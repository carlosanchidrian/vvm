"""Ciclo de vida reutilizable de una sesión VVM (SPEC tramo 6 §5).

El CLI y la TUI comparten esta pieza: `start()` crea los medios y lanza QEMU,
`wait()`/`stop()` gobiernan el hijo, `io()` lee sus contadores y `finish()`
cierra todo en el orden ya validado y devuelve el informe.

`build_report()` no se toca: `finish()` solo le pasa los mismos datos que antes
componía `cmd_run`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from vvm import audit, images, supervisor, vm
from vvm.host_check import CheckResult


@dataclass(frozen=True)
class IOStats:
    """Contadores de I/O del QEMU de la sesión."""

    rchar: int
    wchar: int
    read_bytes: int
    write_bytes: int


@dataclass(frozen=True)
class SessionOutcome:
    """Resultado de esperar al hijo, con la última muestra de I/O."""

    returncode: int
    interrupt: int | None = None
    killed_after: float | None = None
    io: IOStats | None = None


def _as_stats(sample: vm.ProcessIO | IOStats | None) -> IOStats | None:
    """Normaliza la muestra del lector de `vm` al tipo público de esta API."""
    if sample is None:
        return None
    return IOStats(
        rchar=sample.rchar,
        wchar=sample.wchar,
        read_bytes=sample.read_bytes,
        write_bytes=sample.write_bytes,
    )


def read_process_io(pid: int, proc_dir: str = "/proc") -> vm.ProcessIO | None:
    """Lector propio de los contadores de I/O, sustituible en los tests."""
    return vm.read_process_io(pid, proc_dir)


def _exit_code(returncode: int) -> int:
    """Código tipo shell: una señal se declara como 128+señal."""
    return 128 + (-returncode) if returncode < 0 else returncode


def _closure_label(interrupt: int | None) -> str:
    """Etiqueta del cierre para el informe, igual que la del CLI."""
    if interrupt is None:
        return "normal"
    if interrupt == signal.SIGINT:
        return "Ctrl-C"
    if interrupt == signal.SIGTERM:
        return "SIGTERM"
    if interrupt == signal.SIGHUP:
        return "SIGHUP"
    return f"señal {interrupt}"


class Session:
    """Una sesión con medios en memoria y un proceso QEMU gobernable.

    El pre-chequeo se hace antes de construir la sesión; aquí solo se guardan
    sus resultados para el informe. La línea base se toma al empezar, antes de
    crear el disco y de copiar el ISO, para que el delta de Shmem no descuente
    los medios de la propia herramienta.
    """

    def __init__(
        self,
        config: vm.VmConfig,
        *,
        qemu: str = vm.DEFAULT_QEMU,
        checks: Sequence[CheckResult] | None = None,
        forced: bool = False,
        log_path: str | None = None,
        session_path: Path | None = None,
        guard: vm.SignalGuard | None = None,
        snapshotter: Callable[[], audit.Snapshot] | None = None,
        session_saver: Callable[..., None] | None = None,
        session_id: str | None = None,
        label: str | None = None,
        install_signals: bool = True,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        self.config = config
        self.qemu = qemu
        self.checks: tuple[CheckResult, ...] | None = (
            tuple(checks) if checks is not None else None
        )
        self.forced = bool(forced)
        self.log_path = log_path
        self.session_path = Path(session_path) if session_path is not None else None
        self.guard = guard if guard is not None else vm.SignalGuard()
        self._snapshotter = snapshotter
        self._session_saver = session_saver
        self.session_id = session_id
        self.label = label or "cli"
        self._install_signals = bool(install_signals)
        self._external_should_stop = should_stop

        self.before: audit.Snapshot | None = None
        self.after: audit.Snapshot | None = None
        self.report: str | None = None
        self.wipe_info: audit.WipeInfo | None = None
        self.outcome: SessionOutcome | None = None
        self.last_io: IOStats | None = None
        self.started_at: float | None = None
        self.other_sessions = 0

        self._proc: subprocess.Popen | None = None
        self._disk_fd: int | None = None
        self._image_fd: int | None = None
        self._finished = False
        self._killed_after: float | None = None
        self._stop_signal: int | None = None
        self._signals_installed = False
        self._live_written = False
        self._finish_lock = threading.Lock()

    # -- estado -----------------------------------------------------------

    @property
    def pid(self) -> int:
        """PID del proceso lanzado (incluido un `--qemu` de prueba)."""
        if self._proc is None:
            raise RuntimeError("La sesión no se ha iniciado todavía")
        return self._proc.pid

    @property
    def started(self) -> bool:
        """True si hay un proceso lanzado (aunque ya haya terminado)."""
        return self._proc is not None

    @property
    def running(self) -> bool:
        """True mientras el proceso siga vivo."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def killed_after(self) -> float | None:
        """Segundos hasta el SIGKILL, si hubo que escalar."""
        return self._killed_after

    @property
    def finished(self) -> bool:
        """True si `finish()` ya emitió el informe."""
        return self._finished

    @property
    def interrupt(self) -> int | None:
        """Primera señal registrada por el guard o por un `stop()` directo."""
        return self.guard.interrupt or self._stop_signal

    # -- lectores ---------------------------------------------------------

    def _snapshot(self) -> audit.Snapshot:
        if self._snapshotter is not None:
            return self._snapshotter()
        return audit.take_snapshot()

    def poll(self) -> int | None:
        """Código de salida si terminó; `None` si sigue vivo o no arrancó."""
        if self._proc is None:
            return None
        return self._proc.poll()

    def io(self) -> IOStats | None:
        """Contadores de I/O con la VM viva; `None` si ya no se pueden leer."""
        if self._proc is None:
            return None
        try:
            sample = read_process_io(self._proc.pid)
        except (OSError, ValueError):
            return None
        stats = _as_stats(sample)
        if stats is not None:
            self.last_io = stats
        return stats

    # -- arranque ---------------------------------------------------------

    def _should_stop(self) -> bool:
        """Señal del guard propio o la del grupo que gobierna la TUI."""
        if self._external_should_stop is not None:
            return self._external_should_stop()
        return self.guard.interrupt is not None

    def _prepare_registration(self) -> None:
        """Id, directorio 0700 y socket de esta sesión antes de lanzar QEMU."""
        if self.session_id is None:
            self.session_id, directory = supervisor.reserve_session(self.label)
        else:
            directory = supervisor.prepare_session_dir(self.session_id)
        if self.config.serial_mode != "socket":
            return
        if not self.config.serial_sock:
            self.config = replace(
                self.config, serial_sock=str(directory / "serie.sock")
            )
            return
        socket = Path(self.config.serial_sock)
        try:
            if socket.is_socket():
                socket.unlink()
        except OSError as exc:
            print(
                f"Aviso: no se pudo borrar el socket anterior {socket}: {exc}",
                file=sys.stderr,
            )

    def _register_live(self) -> None:
        """Escribe `live.json` después de `Popen`; un fallo no tumba la VM."""
        if self._proc is None or self.session_id is None:
            return
        serial_path = None
        if self.config.serial_mode == "socket":
            serial_path = self.config.serial_sock
        elif self.config.serial_mode == "file":
            serial_path = self.config.serial_log
        entry = supervisor.LiveSession(
            id=self.session_id,
            label=self.label,
            pid=self._proc.pid,
            mem_mb=self.config.mem_mb,
            disk_mb=self.config.disk_mb,
            image=bool(self.config.image),
            started_at=supervisor.now_utc(),
            serial=self.config.serial_mode,
            serial_path=serial_path,
        )
        try:
            supervisor.write_live(entry)
            self._live_written = True
        except OSError as exc:
            print(
                f"Aviso: no se pudo registrar la sesión viva: {exc}",
                file=sys.stderr,
            )

    def start(self) -> None:
        """Crea los medios y lanza QEMU. El pre-chequeo ya debe estar hecho."""
        if self._proc is not None or self.before is not None:
            raise RuntimeError("La sesión ya se ha iniciado")
        # Fuera del hilo principal esto lanza ValueError antes de crear nada.
        if self._install_signals:
            self.guard.install()
            self._signals_installed = True
        self.before = self._snapshot()
        try:
            self._prepare_registration()
            self.other_sessions = supervisor.count_others(
                exclude=self.session_id
            )
            if self.config.disk_mb > 0:
                self._disk_fd = images.create_disk_fd(self.config.disk_mb)
            if self.config.image:
                self._image_fd = images.create_image_fd(
                    self.config.image,
                    should_stop=self._should_stop,
                )
        except BaseException:
            self._close_media()
            raise

        # Una señal durante la copia del ISO aborta el arranque sin lanzar
        # QEMU, pero deja el cierre normal para que el llamador informe.
        if self._should_stop():
            self.started_at = time.monotonic()
            return

        try:
            cmd = vm.build_cmd(
                self.config,
                self._disk_fd,
                qemu=self.qemu,
                image_fd=self._image_fd,
            )
        except BaseException:
            self._close_media()
            raise

        if self._should_stop():
            self.started_at = time.monotonic()
            return

        pass_fds = tuple(
            fd for fd in (self._disk_fd, self._image_fd) if fd is not None
        )
        try:
            self._proc = subprocess.Popen(
                cmd,
                pass_fds=pass_fds,
                preexec_fn=vm._limit_core,
            )
        except BaseException:
            self._close_media()
            raise
        self.guard.attach(self._proc)
        self.started_at = time.monotonic()
        self._register_live()

    # -- gobierno ---------------------------------------------------------

    def wait(
        self,
        *,
        term_wait: float | None = None,
        io_poll: float = vm.IO_POLL_SECONDS,
    ) -> SessionOutcome:
        """Espera al hijo con el mismo comportamiento que `vm.run_session`."""
        wait_seconds = vm.TERM_WAIT_SECONDS if term_wait is None else term_wait
        if self._proc is None:
            interrupt = self.guard.interrupt
            result = SessionOutcome(
                returncode=(128 + interrupt) if interrupt else 0,
                interrupt=interrupt,
                killed_after=self._killed_after,
                io=self.last_io,
            )
            self.outcome = result
            return result

        proc = self._proc
        try:
            while proc.poll() is None and not self.guard.interrupts:
                self.io()
                try:
                    proc.wait(timeout=io_poll)
                except subprocess.TimeoutExpired:
                    continue
        except KeyboardInterrupt:
            if not self.guard.interrupts:
                self.guard.interrupts.append(signal.SIGINT)
                self.guard.first_signal_at = time.monotonic()
                vm._send_signal(proc, signal.SIGTERM)

        if not self.guard.interrupts:
            result = SessionOutcome(
                returncode=proc.returncode,
                io=self.last_io,
            )
            self.outcome = result
            return result

        interrupt = self.guard.interrupts[0]
        self.io()
        if proc.poll() is None:
            try:
                proc.wait(timeout=wait_seconds)
            except subprocess.TimeoutExpired:
                vm._send_signal(proc, signal.SIGKILL)
                vm._reap_bounded(proc)
                # Mismo dato que declaraba `run_session`: la espera agotada.
                self._killed_after = wait_seconds
                self.guard.record_kill(self._killed_after)
            except KeyboardInterrupt:
                self.guard.force_killed = True
                vm._send_signal(proc, signal.SIGKILL)
                vm._reap_bounded(proc)
                self._killed_after = self.guard.elapsed_since_first()
                self.guard.record_kill(self._killed_after)

        result = SessionOutcome(
            returncode=128 + interrupt,
            interrupt=interrupt,
            killed_after=self._killed_after or self.guard.killed_after,
            io=self.last_io,
        )
        self.outcome = result
        return result

    def stop(
        self,
        sig: int = signal.SIGTERM,
        timeout: float = 5.0,
        *,
        mark_signal: bool = True,
    ) -> int:
        """Reenvía una señal, espera y escala a SIGKILL si no atiende.

        Devuelve el código shell del cierre iniciado por la señal (143 para
        SIGTERM). Si el hijo ya había terminado, devuelve su código real.
        `mark_signal=False` se usa en el cierre interno para no atribuir al
        usuario una señal que solo mandó la limpieza de `finish()`.
        """
        if self._proc is None:
            return 0
        if self._proc.poll() is not None:
            return _exit_code(self._proc.returncode)

        if mark_signal:
            self._stop_signal = sig
        self.io()
        started = time.monotonic()
        vm._send_signal(self._proc, sig)
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            vm._send_signal(self._proc, signal.SIGKILL)
            vm._reap_bounded(self._proc)
            self._killed_after = time.monotonic() - started
            self.guard.record_kill(self._killed_after)
        except KeyboardInterrupt:
            vm._send_signal(self._proc, signal.SIGKILL)
            vm._reap_bounded(self._proc)
            self._killed_after = time.monotonic() - started
            self.guard.record_kill(self._killed_after)
        return 128 + sig

    # -- cierre -----------------------------------------------------------

    def _wipe(self, requested: bool) -> audit.WipeInfo | None:
        """Borra el disco en el mismo orden y con las mismas reglas que el CLI."""
        if not requested:
            return None
        if self.config.disk_mb == 0 or self._disk_fd is None:
            return audit.WipeInfo(requested=True, disk_mb=0)
        try:
            result = images.wipe_fd(self._disk_fd)
        except OSError as exc:
            print(f"Aviso: no se pudo borrar el disco: {exc}", file=sys.stderr)
            return audit.WipeInfo(
                requested=True,
                disk_mb=self.config.disk_mb,
                error=str(exc),
            )
        return audit.WipeInfo(
            requested=True,
            disk_mb=result.size_mb,
            seconds=result.seconds,
        )

    def _close_fd(self, name: str) -> None:
        fd = getattr(self, name)
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass
        setattr(self, name, None)

    def _close_media(self) -> None:
        self._close_fd("_image_fd")
        self._close_fd("_disk_fd")

    def _own_snapshots(
        self,
    ) -> tuple[audit.Snapshot, audit.Snapshot]:
        """Quita del informe los procesos y memfds de otras sesiones vivas.

        El delta de Shmem sigue siendo global y el informe lo declara con
        `other_sessions`; la sección 2 y los memfds, en cambio, no pueden
        atribuir a esta sesión procesos que no son suyos.
        """
        assert self.before is not None
        assert self.after is not None
        own_pid = str(self._proc.pid) if self._proc is not None else None
        other_pids = (
            set(self.before.qemu_pids) | set(self.after.qemu_pids)
        )
        if own_pid is not None:
            other_pids.discard(own_pid)
        excluded_memfds: set[str] = set()
        for pid in other_pids:
            if pid.isdigit():
                excluded_memfds.update(
                    audit.memfd_names_of(int(pid))
                )

        def isolate(snapshot: audit.Snapshot) -> audit.Snapshot:
            pids = (
                tuple(pid for pid in snapshot.qemu_pids if pid == own_pid)
                if own_pid is not None
                else ()
            )
            memfds = snapshot.memfds
            if memfds is not None and excluded_memfds:
                memfds = tuple(
                    name for name in memfds if name not in excluded_memfds
                )
            return replace(snapshot, qemu_pids=pids, memfds=memfds)

        return isolate(self.before), isolate(self.after)

    def finish(self, *, wipe: bool = False) -> str:
        """Cierra la sesión una sola vez aunque dos hilos la pidan a la vez."""
        with self._finish_lock:
            return self._finish_locked(wipe)

    def _finish_locked(self, wipe: bool) -> str:
        """Cuerpo de `finish()`, protegido por `_finish_lock`."""
        if self._finished:
            if self.report is None:
                raise RuntimeError("La sesión se cerró sin informe")
            return self.report
        if self.before is None:
            raise RuntimeError("No se puede cerrar una sesión que no se inició")

        if self._proc is not None and self._proc.poll() is None:
            self.stop(mark_signal=False)
        # Última muestra con el hijo todavía cerrable (puede ser None si ya no
        # existe: el fallo del lector nunca tumba la sesión).
        self.io()
        self._close_fd("_image_fd")
        self.wipe_info = self._wipe(wipe)
        self._close_fd("_disk_fd")

        try:
            self.after = self._snapshot()
        except (OSError, RuntimeError) as exc:
            print(
                f"Aviso: no se pudo tomar la instantánea final: {exc}",
                file=sys.stderr,
            )
            self.after = self.before

        self.before, self.after = self._own_snapshots()
        interrupt = self.interrupt
        closure = _closure_label(interrupt)
        killed_after = self._killed_after
        if killed_after is None and self.outcome is not None:
            killed_after = self.outcome.killed_after
        log_size: int | None = None
        if self.log_path:
            try:
                log_size = Path(self.log_path).stat().st_size
            except OSError:
                log_size = None
        extras = audit.SessionExtras(
            closure=closure,
            killed_after=killed_after,
            wipe=self.wipe_info,
            image_mode=bool(self.config.image),
            disk_mb=self.config.disk_mb,
            process_io=self.last_io,
            log_path=self.log_path or None,
            log_size=log_size,
            other_sessions=self.other_sessions,
        )
        self.report = audit.build_report(
            self.before,
            self.after,
            self.log_path,
            checks=self.checks,
            forced=self.forced,
            extras=extras,
        )
        paths: list[Path] = [
            self.session_path or audit.default_session_path()
        ]
        if self.session_id:
            try:
                per_id = audit.session_path_for(self.session_id)
            except ValueError:
                per_id = None
            if per_id is not None and per_id not in paths:
                paths.append(per_id)
        saver = self._session_saver
        for path in paths:
            try:
                if saver is None:
                    audit.save_session(
                        path,
                        self.before,
                        self.after,
                        checks=self.checks,
                        forced=self.forced,
                        extras=extras,
                    )
                else:
                    saver(
                        path,
                        self.before,
                        self.after,
                        checks=self.checks,
                        forced=self.forced,
                        extras=extras,
                    )
            except (OSError, RuntimeError, ValueError) as exc:
                print(f"Aviso: no se pudo guardar la sesión: {exc}", file=sys.stderr)

        if self.session_id is not None:
            supervisor.remove_live(self.session_id)
            self._live_written = False
        self._finished = True
        if self._signals_installed:
            self.guard.restore()
            self._signals_installed = False
        return self.report

    def close(self) -> None:
        """Red de seguridad: cierra medios y restaura señales sin emitir informe."""
        if self._proc is not None and self._proc.poll() is None:
            try:
                self.stop()
            except (OSError, RuntimeError):
                pass
        self._close_media()
        if self.session_id is not None:
            supervisor.remove_live(self.session_id)
            self._live_written = False
        if self._signals_installed:
            self.guard.restore()
            self._signals_installed = False
