"""Línea de QEMU y ciclo de vida de la VM (SPEC §5).

Invariantes de `build_cmd()`:
- `-m <mem>M` y `size=<mem>M` coinciden exactamente.
- El backend `memfd` lleva `share=off`; `-machine` lleva
  `dump-guest-core=off,mem-merge=off`; hay `-nic none`.
- El disco se pasa como `file=/dev/fd/<n>`, no como ruta.
"""

from __future__ import annotations

import resource
import signal
import subprocess
import time
from collections.abc import Sequence
from dataclasses import dataclass

# Marcador que identifica el backend de memoria de una sesión VVM en `cmdline`.
SESSION_MARKER = "memory-backend-memfd,id=mem"

# Espera máxima a que QEMU atienda el SIGTERM antes de matarlo (inyectable).
TERM_WAIT_SECONDS = 5.0

# Espera máxima a que el kernel reape el hijo tras el SIGKILL. Un proceso en
# estado D (I/O bloqueada) no debe colgar el CLI para siempre.
KILL_WAIT_SECONDS = 1.0

# Cada cuánto se muestrea `/proc/<pid>/io` mientras la VM sigue viva.
IO_POLL_SECONDS = 0.1


@dataclass(frozen=True)
class VmConfig:
    """Parámetros de una sesión."""

    kernel: str | None = None
    initrd: str | None = None
    image: str | None = None
    mem_mb: int = 512
    disk_mb: int = 256
    cpus: int = 1
    headless: bool = False
    serial_log: str | None = None


def _validate_config(config: VmConfig) -> None:
    """Valida la fuente de arranque y las combinaciones que no se soportan."""
    has_image = bool(config.image)
    has_kernel = bool(config.kernel)
    has_initrd = bool(config.initrd)

    if has_image and (has_kernel or has_initrd):
        raise ValueError("--image y --kernel/--initrd son excluyentes")
    if has_image and config.headless:
        raise ValueError(
            "--image con --headless no está soportado: el cargador del ISO "
            "pinta su menú en la consola VGA y sin pantalla la sesión se queda "
            "parada ahí"
        )
    if not has_image:
        if has_kernel and not has_initrd:
            raise ValueError("--kernel sin --initrd")
        if has_initrd and not has_kernel:
            raise ValueError("--initrd sin --kernel")
        if not has_kernel and not has_initrd:
            raise ValueError(
                "Falta la fuente de arranque: usa --image o --kernel/--initrd"
            )

    if config.headless and not config.serial_log:
        raise ValueError(
            "El modo headless necesita un log serie (--log): "
            "sin fichero de salida la consola del guest se perdería"
        )
    if config.mem_mb <= 0:
        raise ValueError(f"--mem debe ser mayor que cero: {config.mem_mb} MB")
    if config.disk_mb < 0:
        raise ValueError(f"--disk no puede ser negativo: {config.disk_mb} MB")
    if config.cpus <= 0:
        raise ValueError(f"--cpus debe ser mayor que cero: {config.cpus}")


def build_cmd(
    config: VmConfig,
    disk_fd: int | str | None = None,
    qemu: str = "qemu-system-x86_64",
    *,
    image_fd: int | str | None = None,
) -> list[str]:
    """Construye la línea de QEMU. No lanza ningún proceso.

    `disk_fd` e `image_fd` pueden ser un descriptor real o el texto `<fd>` que
    usa `--dry-run` para no leer la imagen.
    """
    _validate_config(config)
    if config.disk_mb > 0 and disk_fd is None:
        raise ValueError("Falta el descriptor del disco de datos")
    if config.image and image_fd is None:
        raise ValueError("Falta el descriptor de la imagen")

    mem = f"{config.mem_mb}M"
    cmd = [
        qemu,
        "-accel",
        "kvm",
        "-cpu",
        "host",
        "-smp",
        str(config.cpus),
        "-m",
        mem,
        "-object",
        f"{SESSION_MARKER},size={mem},share=off",
        "-machine",
        "q35,memory-backend=mem,dump-guest-core=off,mem-merge=off",
    ]
    if config.image:
        cmd += [
            "-nodefaults",
            "-vga",
            "std",
            "-device",
            "qemu-xhci",
            "-device",
            "usb-tablet",
            "-drive",
            (
                f"file=/dev/fd/{image_fd},format=raw,media=cdrom,"
                "readonly=on"
            ),
            "-boot",
            "d",
        ]
        if config.disk_mb > 0:
            cmd += [
                "-drive",
                f"file=/dev/fd/{disk_fd},format=raw,if=virtio,cache=writethrough",
            ]
        cmd += [
            "-nic",
            "none",
            "-display",
            "gtk",
            "-serial",
            "mon:stdio",
        ]
        return cmd

    cmd += [
        "-kernel",
        config.kernel,
        "-initrd",
        config.initrd,
        "-append",
        "console=ttyS0",
    ]
    if config.disk_mb > 0:
        cmd += [
            "-drive",
            f"file=/dev/fd/{disk_fd},format=raw,if=virtio,cache=writethrough",
        ]
    cmd += ["-nic", "none", "-display", "none"]
    if config.headless:
        cmd += ["-monitor", "none", "-serial", f"file:{config.serial_log}"]
    else:
        cmd += ["-serial", "mon:stdio"]
    return cmd


@dataclass(frozen=True)
class ProcessIO:
    """Contadores de I/O del QEMU de la sesión (`/proc/<pid>/io`)."""

    rchar: int
    wchar: int
    read_bytes: int
    write_bytes: int


@dataclass(frozen=True)
class RunOutcome:
    """Resultado del ciclo de vida de una sesión.

    `interrupt` es la señal que inició el cierre (None si fue normal);
    `killed_after` solo se rellena si hubo que mandar SIGKILL; `process_io` es
    la última muestra leída con la VM todavía viva.
    """

    returncode: int
    interrupt: int | None = None
    killed_after: float | None = None
    process_io: ProcessIO | None = None


def _limit_core() -> None:
    """Fija `RLIMIT_CORE=0` en el proceso hijo (sin volcados a disco)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def read_process_io(pid: int, proc_dir: str = "/proc") -> ProcessIO | None:
    """Lee los contadores de `/proc/<pid>/io`; None si no se pueden leer."""
    values: dict[str, int] = {}
    try:
        with open(
            f"{proc_dir}/{pid}/io", encoding="ascii", errors="replace"
        ) as handle:
            for line in handle:
                key, sep, rest = line.partition(":")
                if not sep:
                    continue
                try:
                    values[key.strip()] = int(rest.strip())
                except ValueError:
                    continue
    except OSError:
        return None
    try:
        return ProcessIO(
            rchar=values["rchar"],
            wchar=values["wchar"],
            read_bytes=values["read_bytes"],
            write_bytes=values["write_bytes"],
        )
    except KeyError:
        return None


def _send_signal(proc: subprocess.Popen | None, signum: int) -> None:
    """Envía una señal al hijo sin fallar si ya terminó."""
    if proc is None:
        return
    try:
        proc.send_signal(signum)
    except ProcessLookupError:
        pass


class SignalGuard:
    """Manejadores de SIGTERM, SIGHUP y SIGINT activos durante todo el ciclo.

    El manejador solo registra la señal y reenvía al hijo asociado: no hace
    trabajo pesado, así que también vale para el teardown (wipe, instantánea,
    informe y sesión). La primera señal se reenvía como SIGTERM; la segunda
    escala a SIGKILL y anota el tiempo transcurrido desde la primera.

    `install()` debe llamarse antes de `Popen` para cerrar la carrera de
    arranque: si llega una señal antes de `attach()`, se reenvía en cuanto el
    hijo está asociado. `restore()` deshace los manejadores; es idempotente.
    """

    SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def __init__(self) -> None:
        self.interrupts: list[int] = []
        self.force_killed = False
        self.first_signal_at: float | None = None
        self.killed_after: float | None = None
        self._proc: subprocess.Popen | None = None
        self._previous: dict[int, object] = {}
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def interrupt(self) -> int | None:
        """Primera señal registrada (la que inició el cierre), si hubo alguna."""
        return self.interrupts[0] if self.interrupts else None

    def install(self) -> None:
        """Instala los manejadores. Idempotente; falla fuera del hilo principal."""
        if self._installed:
            return
        previous: dict[int, object] = {}
        try:
            for signum in self.SIGNALS:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
        except BaseException:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            raise
        self._previous = previous
        self._installed = True

    def attach(self, proc: subprocess.Popen) -> None:
        """Asocia el hijo y materializa una señal que llegara antes de `Popen`.

        Con una sola señal se reenvía SIGTERM; si ya habían llegado dos, el
        SIGKILL pendiente se envía aquí y se anota el tiempo desde la primera.
        """
        self._proc = proc
        if not self.interrupts:
            return
        if self.force_killed:
            if self.killed_after is None:
                self.killed_after = self.elapsed_since_first()
            _send_signal(proc, signal.SIGKILL)
        else:
            _send_signal(proc, signal.SIGTERM)

    def record_kill(self, seconds: float | None) -> None:
        """Anota el SIGKILL si el manejador no lo había hecho ya."""
        if self.killed_after is None:
            self.killed_after = seconds

    def elapsed_since_first(self) -> float | None:
        """Segundos desde la primera señal; `None` si no hubo ninguna."""
        if self.first_signal_at is None:
            return None
        return time.monotonic() - self.first_signal_at

    def restore(self) -> None:
        """Restaura los manejadores previos. Idempotente."""
        if not self._installed:
            return
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous = {}
        self._installed = False

    def _handle(self, signum: int, _frame: object) -> None:
        if not self.interrupts:
            self.interrupts.append(signum)
            self.first_signal_at = time.monotonic()
            _send_signal(self._proc, signal.SIGTERM)
            return
        self.force_killed = True
        if self._proc is None:
            # Sin hijo asociado no se ha enviado ningún SIGKILL todavía: lo
            # materializa `attach()`, que además anota el tiempo real.
            return
        if self.killed_after is None:
            self.killed_after = self.elapsed_since_first()
        _send_signal(self._proc, signal.SIGKILL)


def _reap_bounded(proc: subprocess.Popen, timeout: float = KILL_WAIT_SECONDS) -> None:
    """Reapea al hijo sin esperar para siempre si el kernel no puede matarlo."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        pass


def run_session(
    cmd: Sequence[str],
    pass_fds: Sequence[int] = (),
    *,
    term_wait: float | None = None,
    io_poll: float = IO_POLL_SECONDS,
    guard: SignalGuard | None = None,
) -> RunOutcome:
    """Lanza QEMU, atiende SIGTERM/SIGHUP/SIGINT y devuelve el cierre.

    Los manejadores se instalan **antes** de `Popen` (así no queda un hijo vivo
    si la instalación falla o si llega una señal en el arranque) y se restauran
    solo si el guard es propio. Si se recibe `guard`, el llamador es el dueño de
    su instalación y de su restauración, para poder cubrir también el teardown.

    SIGTERM, SIGHUP y SIGINT se reenvían al hijo como SIGTERM; si no sale en
    `term_wait` segundos, se le manda SIGKILL. Una segunda señal lo mata de
    inmediato y anota el tiempo desde la primera. En el hijo se fija
    `RLIMIT_CORE=0`.
    """
    wait_seconds = TERM_WAIT_SECONDS if term_wait is None else term_wait
    own_guard = guard is None
    if guard is None:
        guard = SignalGuard()
    # Antes de `Popen`: fuera del hilo principal esto lanza ValueError y no se
    # llega a crear ningún hijo. Es idempotente si el llamador ya lo instaló.
    guard.install()

    try:
        proc = subprocess.Popen(
            list(cmd),
            pass_fds=tuple(pass_fds),
            preexec_fn=_limit_core,
        )
    except BaseException:
        if own_guard:
            guard.restore()
        raise
    guard.attach(proc)

    io_sample: ProcessIO | None = None
    try:
        try:
            while proc.poll() is None and not guard.interrupts:
                sample = read_process_io(proc.pid)
                if sample is not None:
                    io_sample = sample
                try:
                    proc.wait(timeout=io_poll)
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            # Red de seguridad si el guard no estaba instalado (SIGINT no pasa
            # por el manejador): se trata como una interrupción más.
            if not guard.interrupts:
                guard.interrupts.append(signal.SIGINT)
                guard.first_signal_at = time.monotonic()
                _send_signal(proc, signal.SIGTERM)

        if not guard.interrupts:
            return RunOutcome(
                returncode=proc.returncode,
                process_io=io_sample,
            )

        interrupt = guard.interrupts[0]
        sample = read_process_io(proc.pid)
        if sample is not None:
            io_sample = sample

        if proc.poll() is None:
            try:
                proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                _send_signal(proc, signal.SIGKILL)
                _reap_bounded(proc)
                guard.record_kill(wait_seconds)
            except KeyboardInterrupt:
                guard.force_killed = True
                _send_signal(proc, signal.SIGKILL)
                _reap_bounded(proc)
                guard.record_kill(guard.elapsed_since_first())

        return RunOutcome(
            returncode=128 + interrupt,
            interrupt=interrupt,
            killed_after=guard.killed_after,
            process_io=io_sample,
        )
    finally:
        # Red de seguridad: si algo inesperado interrumpe el bucle, no dejar
        # QEMU vivo. En los caminos normales el hijo ya terminó.
        if proc.poll() is None:
            _send_signal(proc, signal.SIGKILL)
            _reap_bounded(proc)
        if own_guard:
            guard.restore()


def run_vm(cmd: Sequence[str], pass_fds: Sequence[int] = ()) -> int:
    """Compatibilidad: lanza una sesión y devuelve solo el código de salida."""
    return run_session(cmd, pass_fds).returncode
