"""Línea base, cierre e informe determinista (SPEC §6).

Sin IA: el informe se compone de medidas antes/después y de un veredicto
calculado. La sesión se guarda en JSON para poder auditarla después.

Los lectores del sistema (`read_meminfo`, `list_shm_entries`, `live_qemu_pids`)
son funciones de módulo con la ruta como argumento para poder sustituirlos en
los tests. Este módulo solo lee: nunca crea ni borra entradas de `/dev/shm`.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from vvm.host_check import CheckResult
from vvm.vm import SESSION_MARKER, ProcessIO

MEMINFO_PATH = Path("/proc/meminfo")
SHM_PATH = Path("/dev/shm")
PROC_PATH = Path("/proc")

_QEMU_PREFIX = "qemu-system-"
_MEMFD_PREFIX = "/memfd:"
_SESSION_MARKER_BYTES = SESSION_MARKER.encode()

# Memfds que crea el escritorio (pantalla, cursor y audio). Se comparan con la
# línea base y se declaran como pila del anfitrión, nunca como fuga del guest.
HOST_STACK_MEMFDS = frozenset(
    {
        "memfd:displaysurface",
        "memfd:wayland-cursor",
        "memfd:pulseaudio",
    }
)


@dataclass(frozen=True)
class Snapshot:
    """Medidas del anfitrión en un instante."""

    shmem_kb: int
    mem_available_kb: int
    shm_entries: tuple[str, ...]
    qemu_pids: tuple[str, ...]
    taken_at: str
    # `None` = sesión antigua que no midió memfds; `()` = se midieron y no había.
    memfds: tuple[str, ...] | None = None


@dataclass(frozen=True)
class WipeInfo:
    """Resultado declarado del borrado del disco de datos."""

    requested: bool
    disk_mb: int
    seconds: float | None = None
    error: str | None = None


@dataclass(frozen=True)
class SessionExtras:
    """Datos del cierre que acompañan a los snapshots.

    `closure=None` significa que el CLI no midió el cierre (sesión antigua o
    fallo antes de lanzar QEMU): el informe lo declara como desconocido.
    """

    closure: str | None = None
    killed_after: float | None = None
    wipe: WipeInfo | None = None
    image_mode: bool = False
    disk_mb: int = 0
    process_io: ProcessIO | None = None


@dataclass(frozen=True)
class Session:
    """Sesión guardada: snapshots más el pre-chequeo que la autorizó."""

    before: Snapshot
    after: Snapshot
    checks: tuple[CheckResult, ...] | None = None
    forced: bool = False
    extras: SessionExtras = SessionExtras()


def read_meminfo(path: Path = MEMINFO_PATH) -> dict[str, int]:
    """Lee `/proc/meminfo` y devuelve los valores en kB indexados por nombre."""
    values: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                name, sep, rest = line.partition(":")
                if not sep:
                    continue
                parts = rest.split()
                if not parts:
                    continue
                try:
                    values[name.strip()] = int(parts[0])
                except ValueError:
                    continue
    except OSError as exc:
        raise RuntimeError(f"No se pudo leer {path}: {exc}") from exc
    return values


def list_shm_entries(shm_dir: Path = SHM_PATH) -> tuple[str, ...]:
    """Lista los nombres de `/dev/shm` sin abrir ni modificar ninguna entrada."""
    try:
        with os.scandir(shm_dir) as entries:
            return tuple(sorted(entry.name for entry in entries))
    except OSError as exc:
        raise RuntimeError(f"No se pudo listar {shm_dir}: {exc}") from exc


def list_memfd_names(proc_dir: Path = PROC_PATH) -> tuple[str, ...]:
    """Nombres `memfd:*` visibles en los descriptores abiertos de `/proc`.

    Se leen enlaces de `/proc/<pid>/fd` (no se abre ni se toca ningún memfd) y
    se normaliza el sufijo ` (deleted)` que añade el kernel. Solo cuentan los
    destinos que **empiezan** por `/memfd:`: un fichero regular llamado
    `memfd:algo` no es un memfd anónimo.
    """
    names: set[str] = set()
    try:
        with os.scandir(proc_dir) as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                try:
                    with os.scandir(Path(entry.path) / "fd") as fds:
                        for fd_entry in fds:
                            try:
                                target = os.readlink(fd_entry.path)
                            except OSError:
                                continue
                            if not target.startswith(_MEMFD_PREFIX):
                                continue
                            raw = target[len(_MEMFD_PREFIX):]
                            names.add("memfd:" + raw.split(" (deleted)", 1)[0])
                except OSError:
                    continue
    except OSError as exc:
        raise RuntimeError(f"No se pudo listar {proc_dir}: {exc}") from exc
    return tuple(sorted(names))


def live_qemu_pids(proc_dir: Path = PROC_PATH) -> tuple[str, ...]:
    """PIDs de procesos QEMU de esta sesión (cmdline con el backend memfd)."""
    pids: list[str] = []
    try:
        with os.scandir(proc_dir) as entries:
            for entry in entries:
                if not entry.name.isdigit():
                    continue
                if _is_session_qemu(Path(entry.path)):
                    pids.append(entry.name)
    except OSError as exc:
        raise RuntimeError(f"No se pudo listar {proc_dir}: {exc}") from exc
    return tuple(sorted(pids, key=_pid_sort_key))


def _is_session_qemu(proc_pid: Path) -> bool:
    """True si es un `qemu-system-*` con el marcador de esta sesión en `cmdline`.

    Hacen falta las dos condiciones: el marcador aparece también en el `argv`
    del propio CLI o de un shell, y esos procesos no son una VM. Sin `cmdline`
    legible no se puede atribuir la sesión, así que no cuenta.
    """
    try:
        raw = (proc_pid / "cmdline").read_bytes()
    except OSError:
        return False
    argv0 = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    if not Path(argv0).name.startswith(_QEMU_PREFIX):
        return False
    return _SESSION_MARKER_BYTES in raw


def _pid_sort_key(pid: str) -> tuple[int, int | str]:
    return (0, int(pid)) if pid.isdigit() else (1, pid)


def take_snapshot() -> Snapshot:
    """Toma una foto de Shmem, MemAvailable, /dev/shm y procesos QEMU vivos."""
    meminfo = read_meminfo(MEMINFO_PATH)
    missing = [key for key in ("Shmem", "MemAvailable") if key not in meminfo]
    if missing:
        raise RuntimeError(
            f"Faltan las claves {', '.join(missing)} en {MEMINFO_PATH}: "
            "¿kernel sin MemAvailable o /proc/meminfo recortado?"
        )
    return Snapshot(
        shmem_kb=meminfo["Shmem"],
        mem_available_kb=meminfo["MemAvailable"],
        shm_entries=list_shm_entries(SHM_PATH),
        qemu_pids=live_qemu_pids(PROC_PATH),
        taken_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        memfds=list_memfd_names(PROC_PATH),
    )


_STATUS_LABELS = {"OK": "OK", "WARN": "AVISO", "FAIL": "FALLA"}


def _blocking_failures(
    checks: tuple[CheckResult, ...] | None,
) -> tuple[CheckResult, ...]:
    """Comprobaciones en FALLA que bloquean el arranque."""
    if not checks:
        return ()
    return tuple(check for check in checks if check.status == "FAIL" and check.blocking)


def _precheck_lines(
    checks: tuple[CheckResult, ...] | None,
    forced: bool,
) -> list[str]:
    """Sección 0: qué dijo el pre-chequeo y si se arrancó con `--force`."""
    lines = ["0 · Pre-chequeo de la sesión"]
    if checks is None:
        lines.append("   Desconocido: la sesión no guardó el pre-chequeo.")
        return lines

    failed = [check for check in checks if check.status != "OK"]
    if failed:
        for check in failed:
            label = _STATUS_LABELS.get(check.status, check.status)
            # Un aviso nunca bloquea: P2 avisa por `--mem < 2048` sin impedir el
            # arranque, así que el informe no puede llamarlo «bloqueante».
            blocking_check = check.status == "FAIL" and check.blocking
            kind = "bloqueante" if blocking_check else "no bloqueante"
            lines.append(
                f"   {check.id} {label} ({kind}) {check.vector}: {check.evidence}"
            )
        ok_count = len(checks) - len(failed)
        lines.append(f"   Resto: {ok_count} comprobaciones OK.")
    else:
        lines.append(f"   Apto: las {len(checks)} comprobaciones salieron OK.")

    blocking = len(_blocking_failures(checks))
    if forced and blocking:
        lines.append(f"   Forzada: sí ({blocking} fallo(s) bloqueante(s) ignorados).")
    else:
        lines.append(f"   Forzada: {'sí' if forced else 'no'}.")
    return lines


def build_report(
    before: Snapshot,
    after: Snapshot,
    log_path: str | None = None,
    *,
    checks: tuple[CheckResult, ...] | None = None,
    forced: bool = False,
    extras: SessionExtras | None = None,
) -> str:
    """Genera el informe de la sesión con las secciones y el veredicto de SPEC §6.

    `checks` es el pre-chequeo que autorizó la sesión; `None` significa que la
    sesión guardada no lo incluía (formato antiguo). `forced` indica si se
    arrancó con `--force`. `extras` reúne el cierre, el I/O del proceso y el
    resultado de `--wipe`.
    """
    details = extras if extras is not None else SessionExtras()
    new_entries = sorted(set(after.shm_entries) - set(before.shm_entries))
    # Sin línea base de memfds (sesión antigua) no se puede atribuir nada: ni
    # «ninguno» (sería un falso negativo) ni un hallazgo.
    memfds_measured = before.memfds is not None and after.memfds is not None
    if memfds_measured:
        new_memfds = sorted(set(after.memfds) - set(before.memfds))
    else:
        new_memfds = []
    host_memfds = [name for name in new_memfds if name in HOST_STACK_MEMFDS]
    other_memfds = [name for name in new_memfds if name not in HOST_STACK_MEMFDS]
    findings: list[str] = []
    findings.extend(f"entrada nueva en /dev/shm: {name}" for name in new_entries)
    findings.extend(
        f"memfd nuevo fuera de la pila del anfitrión: {name}"
        for name in other_memfds
    )
    findings.extend(
        f"proceso QEMU vivo al cierre: pid {pid}"
        for pid in sorted(after.qemu_pids, key=_pid_sort_key)
    )
    if details.killed_after is not None:
        findings.append(
            "QEMU no atendió el SIGTERM: "
            f"SIGKILL a los {details.killed_after:g} s"
        )
    if details.wipe is not None and details.wipe.error is not None:
        findings.append(f"borrado del disco falló: {details.wipe.error}")
    if details.process_io is not None:
        if details.image_mode and details.process_io.write_bytes > 0:
            findings.append(_guest_io_note(details))
        elif not details.image_mode and details.process_io.write_bytes > 0:
            findings.append(
                "I/O de disco inesperada en modo kernel: "
                f"write_bytes={details.process_io.write_bytes} B"
            )
    blocking = _blocking_failures(checks)
    forced_note = ""
    if blocking:
        forced_note = (
            f" · sesión forzada con {len(blocking)} fallo(s) bloqueante(s)"
        )
    elif forced and checks is None:
        forced_note = " · sesión forzada"

    lines = [
        "Informe de sesión VVM (determinista, sin IA)",
        f"Línea base: {before.taken_at}",
        f"Cierre:     {after.taken_at}",
        "",
        *_precheck_lines(checks, forced),
        "",
        "1. Entradas de /dev/shm y memfds",
        f"   Antes:   {_format_entries(before.shm_entries)}",
        f"   Después: {_format_entries(after.shm_entries)}",
        "   Nuevas:  " + (", ".join(new_entries) if new_entries else "ninguna"),
        "   Memfds nuevos de la pila del anfitrión (no del guest): "
        + _format_memfds(host_memfds, memfds_measured),
        "   Memfds nuevos fuera de esa pila: "
        + _format_memfds(other_memfds, memfds_measured),
        "",
        "2. Procesos QEMU de esta sesión vivos al cierre",
        f"   Antes:   {_format_entries(before.qemu_pids)}",
        f"   Después: {_format_entries(after.qemu_pids)}",
        "",
        "3. Memoria (Shmem y MemAvailable)",
        "   Shmem:        antes {} kB, después {} kB, delta {} kB".format(
            before.shmem_kb, after.shmem_kb, _delta(before.shmem_kb, after.shmem_kb)
        ),
        "   MemAvailable: antes {} kB, después {} kB, delta {} kB".format(
            before.mem_available_kb,
            after.mem_available_kb,
            _delta(before.mem_available_kb, after.mem_available_kb),
        ),
        "   Aviso: Shmem es un contador global del anfitrión (todo tmpfs, /dev/shm incluido)",
        "   y con un escritorio abierto tiene cientos de MB de ruido. Sin una línea base",
        "   limpia no se le puede atribuir al guest ninguna variación.",
        "",
        "4. I/O del proceso QEMU",
        *_process_io_lines(details.process_io, details.image_mode, details.disk_mb),
        "",
        "5. Log serie",
        *_log_lines(log_path),
        "",
        "6. Cierre y borrado",
        f"   Cierre: {details.closure or 'desconocido'}",
        *_teardown_lines(details),
        "",
        "7. Fuera del alcance de este informe",
        "   - root y ptrace sobre el proceso: no se comprueban.",
        "   - volcado de memoria en vivo (LiME/AVML): no se comprueba.",
        "   - DMA y Thunderbolt: no se comprueban.",
        "   - cold boot: no se comprueba.",
        "   - journald y page cache: pendiente.",
        "   - la propia herramienta (VVM): no se audita a sí misma.",
        "",
        "8. Veredicto",
        (
            "SIN HALLAZGOS EN LO COMPROBADO"
            if not findings
            else "HALLAZGOS: " + "; ".join(findings)
        )
        + forced_note,
    ]
    return "\n".join(lines) + "\n"


def _format_entries(entries: tuple[str, ...]) -> str:
    return ", ".join(sorted(entries)) if entries else "(ninguna)"


def _format_memfds(names: list[str], measured: bool) -> str:
    """«no medido» cuando falta la línea base; «ninguno» solo si se midió."""
    if not measured:
        return "no medido en esta sesión"
    return ", ".join(names) if names else "ninguno"


def _delta(before: int, after: int) -> str:
    return f"{after - before:+d}"


def _guest_io_note(extras: SessionExtras) -> str:
    """Lectura de las escrituras de una sesión con ventana, según haya disco.

    Con `--disk 0` no hay ningún disco de datos escribible y se mantiene la
    frase de la especificación; con disco virtio no se puede afirmar que QEMU
    no tenga dispositivo de bloque, así que se declara sin atribuir.
    """
    assert extras.process_io is not None
    size = extras.process_io.write_bytes
    if extras.disk_mb > 0:
        return (
            f"I/O con GUI y disco de datos: {size} B; "
            "no se puede atribuir solo al anfitrión"
        )
    return (
        f"I/O del anfitrión con GUI: {size} B; "
        "QEMU no tiene ningún dispositivo de bloque abierto"
    )


def _process_io_lines(
    io: ProcessIO | None, image_mode: bool, disk_mb: int
) -> list[str]:
    """Sección de I/O: contadores crudos y la lectura que autoriza el veredicto."""
    if io is None:
        return [
            "   No disponible: no se pudo leer /proc/<pid>/io con la VM viva."
        ]
    lines = [
        f"   rchar:       {io.rchar} B",
        f"   wchar:       {io.wchar} B",
        f"   read_bytes:  {io.read_bytes} B",
        f"   write_bytes: {io.write_bytes} B",
    ]
    if image_mode and io.write_bytes > 0:
        extras = SessionExtras(
            image_mode=True, disk_mb=disk_mb, process_io=io
        )
        lines.append("   " + _guest_io_note(extras))
    elif image_mode:
        lines.append(
            "   Sin escrituras a bloque detectadas en esta sesión con ventana."
        )
    elif io.write_bytes == 0:
        lines.append(
            "   write_bytes=0: no llegó ningún byte a un dispositivo de bloque."
        )
    else:
        lines.append(
            f"   write_bytes={io.write_bytes} B: hay I/O a bloque; "
            "el testigo del modo kernel no se cumple."
        )
    return lines


def _teardown_lines(extras: SessionExtras) -> list[str]:
    """Líneas de cierre: SIGKILL, borrado del disco y declaración del ISO."""
    lines: list[str] = []
    if extras.killed_after is not None:
        lines.append(
            "   QEMU no atendió el SIGTERM: "
            f"SIGKILL a los {extras.killed_after:g} s"
        )
    wipe = extras.wipe
    if wipe is None:
        lines.append("   Wipe: no solicitado")
    elif wipe.error is not None:
        lines.append(f"   Wipe: error: {wipe.error}")
    elif wipe.disk_mb == 0:
        lines.append("   Wipe: sin disco de datos")
    else:
        seconds = wipe.seconds if wipe.seconds is not None else 0.0
        lines.append(
            f"   Wipe: {wipe.disk_mb} MiB a cero en {seconds:.3f} s"
        )
    if extras.image_mode:
        lines.append(
            "   ISO: copia anónima liberada al cerrar; no se sobrescribe"
        )
    return lines


def _log_lines(log_path: str | None) -> list[str]:
    if not log_path:
        return ["   Log serie: ninguno"]
    path = Path(log_path)
    try:
        size = path.stat().st_size
    except OSError:
        return [f"   Log serie: ninguno (no existe {path})"]
    return [
        f"   Log serie: {path} ({size} bytes)",
        "   es un rastro en disco: bórralo o guárdalo aparte",
    ]


def default_session_path() -> Path:
    """Ruta por defecto de la última sesión: `$XDG_STATE_HOME/vvm/last-session.json`."""
    state_home = os.environ.get("XDG_STATE_HOME", "")
    base = Path(state_home) if state_home else Path.home() / ".local" / "state"
    return base / "vvm" / "last-session.json"


def save_session(
    path: Path,
    before: Snapshot,
    after: Snapshot,
    checks: tuple[CheckResult, ...] | None = None,
    forced: bool = False,
    extras: SessionExtras | None = None,
) -> None:
    """Guarda snapshots y, si se conoce, el pre-chequeo de la sesión en JSON."""
    document: dict[str, object] = {
        "before": _snapshot_to_dict(before),
        "after": _snapshot_to_dict(after),
        "forced": bool(forced),
        "extras": _extras_to_dict(extras if extras is not None else SessionExtras()),
    }
    if checks is not None:
        document["checks"] = [asdict(check) for check in checks]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"No se pudo guardar la sesión en {path}: {exc}") from exc


def load_session_full(path: Path) -> Session:
    """Carga una sesión y devuelve también su pre-chequeo.

    Las sesiones antiguas sin `checks` se cargan con `checks=None`; las que no
    tienen `forced` se consideran no forzadas.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"No se pudo leer la sesión {path}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"La sesión {path} no es un JSON válido: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(
            f"La sesión {path} debe ser un objeto JSON con las claves 'before' y 'after'."
        )
    missing = [key for key in ("before", "after") if key not in document]
    if missing:
        raise ValueError(f"Faltan las claves {', '.join(missing)} en la sesión {path}.")

    checks_raw = document.get("checks")
    if checks_raw is None:
        checks: tuple[CheckResult, ...] | None = None
    else:
        if not isinstance(checks_raw, list):
            raise ValueError(
                f"El campo 'checks' de la sesión {path} debe ser una lista."
            )
        checks = tuple(_check_from_dict(item, path) for item in checks_raw)

    forced = document.get("forced", False)
    if not isinstance(forced, bool):
        raise ValueError(
            f"El campo 'forced' de la sesión {path} debe ser verdadero o falso."
        )

    return Session(
        before=_snapshot_from_dict(document["before"], "before", path),
        after=_snapshot_from_dict(document["after"], "after", path),
        checks=checks,
        forced=forced,
        extras=_extras_from_dict(document.get("extras"), path),
    )


def load_session(path: Path) -> tuple[Snapshot, Snapshot]:
    """Carga una sesión guardada y devuelve (antes, después)."""
    session = load_session_full(path)
    return session.before, session.after


def _snapshot_to_dict(snapshot: Snapshot) -> dict[str, object]:
    return {
        "shmem_kb": snapshot.shmem_kb,
        "mem_available_kb": snapshot.mem_available_kb,
        "shm_entries": list(snapshot.shm_entries),
        "qemu_pids": list(snapshot.qemu_pids),
        "taken_at": snapshot.taken_at,
        "memfds": None if snapshot.memfds is None else list(snapshot.memfds),
    }


_FIELDS = ("shmem_kb", "mem_available_kb", "shm_entries", "qemu_pids", "taken_at")
_CHECK_FIELDS = ("id", "vector", "status", "blocking", "evidence", "remediation")
_CHECK_STRINGS = ("id", "vector", "status", "evidence", "remediation")
_PROCESS_IO_FIELDS = ("rchar", "wchar", "read_bytes", "write_bytes")


def _extras_to_dict(extras: SessionExtras) -> dict[str, object]:
    return {
        "closure": extras.closure,
        "killed_after": extras.killed_after,
        "image_mode": extras.image_mode,
        "disk_mb": extras.disk_mb,
        "process_io": (
            asdict(extras.process_io) if extras.process_io is not None else None
        ),
        "wipe": asdict(extras.wipe) if extras.wipe is not None else None,
    }


def _extras_from_dict(raw: object, path: Path) -> SessionExtras:
    """Carga los datos del cierre; una sesión antigua no los tiene y usa defectos."""
    if raw is None:
        return SessionExtras()
    if not isinstance(raw, dict):
        raise ValueError(f"El campo 'extras' de la sesión {path} debe ser un objeto.")

    closure = raw.get("closure")
    if closure is not None and (not isinstance(closure, str) or not closure):
        raise ValueError(
            f"El campo 'closure' de la sesión {path} debe ser una cadena no "
            "vacía o null."
        )
    killed_after = raw.get("killed_after")
    if killed_after is not None and (
        not isinstance(killed_after, (int, float)) or isinstance(killed_after, bool)
    ):
        raise ValueError(
            f"El campo 'killed_after' de la sesión {path} debe ser un número o null."
        )
    image_mode = raw.get("image_mode", False)
    if not isinstance(image_mode, bool):
        raise ValueError(
            f"El campo 'image_mode' de la sesión {path} debe ser verdadero o falso."
        )
    disk_mb = raw.get("disk_mb", 0)
    if not isinstance(disk_mb, int) or isinstance(disk_mb, bool) or disk_mb < 0:
        raise ValueError(
            f"El campo 'disk_mb' de la sesión {path} debe ser un entero no negativo."
        )

    process_io = _process_io_from_dict(raw.get("process_io"), path)
    wipe = _wipe_from_dict(raw.get("wipe"), path)
    return SessionExtras(
        closure=closure,
        killed_after=float(killed_after) if killed_after is not None else None,
        wipe=wipe,
        image_mode=image_mode,
        disk_mb=disk_mb,
        process_io=process_io,
    )


def _process_io_from_dict(raw: object, path: Path) -> ProcessIO | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"El campo 'process_io' de la sesión {path} debe ser un objeto o null."
        )
    values: dict[str, int] = {}
    for field in _PROCESS_IO_FIELDS:
        value = raw.get(field)
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"El campo '{field}' de process_io en la sesión {path} "
                "debe ser un entero."
            )
        values[field] = value
    return ProcessIO(**values)


def _wipe_from_dict(raw: object, path: Path) -> WipeInfo | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(
            f"El campo 'wipe' de la sesión {path} debe ser un objeto o null."
        )
    requested = raw.get("requested")
    if not isinstance(requested, bool):
        raise ValueError(
            f"El campo 'requested' de wipe en la sesión {path} "
            "debe ser verdadero o falso."
        )
    disk_mb = raw.get("disk_mb")
    if not isinstance(disk_mb, int) or isinstance(disk_mb, bool):
        raise ValueError(
            f"El campo 'disk_mb' de wipe en la sesión {path} debe ser un entero."
        )
    seconds = raw.get("seconds")
    if seconds is not None and (
        not isinstance(seconds, (int, float)) or isinstance(seconds, bool)
    ):
        raise ValueError(
            f"El campo 'seconds' de wipe en la sesión {path} "
            "debe ser un número o null."
        )
    error = raw.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError(
            f"El campo 'error' de wipe en la sesión {path} debe ser texto o null."
        )
    return WipeInfo(
        requested=requested,
        disk_mb=disk_mb,
        seconds=float(seconds) if seconds is not None else None,
        error=error,
    )


def _check_from_dict(raw: object, path: Path) -> CheckResult:
    if not isinstance(raw, dict):
        raise ValueError(
            f"Cada comprobación de la sesión {path} debe ser un objeto JSON."
        )
    missing = [field for field in _CHECK_FIELDS if field not in raw]
    if missing:
        raise ValueError(
            f"Faltan los campos {', '.join(missing)} en una comprobación "
            f"de la sesión {path}."
        )
    for field in _CHECK_STRINGS:
        if not isinstance(raw[field], str):
            raise ValueError(
                f"El campo '{field}' de una comprobación de la sesión {path} "
                "debe ser una cadena."
            )
    if raw["status"] not in _STATUS_LABELS:
        raise ValueError(
            f"Estado desconocido en una comprobación de la sesión {path}: "
            f"{raw['status']!r}."
        )
    if not isinstance(raw["blocking"], bool):
        raise ValueError(
            f"El campo 'blocking' de una comprobación de la sesión {path} "
            "debe ser verdadero o falso."
        )
    return CheckResult(
        id=raw["id"],
        vector=raw["vector"],
        status=raw["status"],
        blocking=raw["blocking"],
        evidence=raw["evidence"],
        remediation=raw["remediation"],
    )


def _snapshot_from_dict(raw: object, block: str, path: Path) -> Snapshot:
    if not isinstance(raw, dict):
        raise ValueError(f"El bloque '{block}' de la sesión {path} debe ser un objeto JSON.")
    missing = [field for field in _FIELDS if field not in raw]
    if missing:
        raise ValueError(
            f"Faltan los campos {', '.join(missing)} en el bloque '{block}' "
            f"de la sesión {path}."
        )
    for field in ("shmem_kb", "mem_available_kb"):
        value = raw[field]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"El campo '{field}' del bloque '{block}' de la sesión {path} "
                "debe ser un entero."
            )
    sequences: dict[str, tuple[str, ...]] = {}
    for field in ("shm_entries", "qemu_pids"):
        value = raw[field]
        if not isinstance(value, (list, tuple)) or not all(
            isinstance(item, str) for item in value
        ):
            raise ValueError(
                f"El campo '{field}' del bloque '{block}' de la sesión {path} "
                "debe ser una lista de cadenas."
            )
        sequences[field] = tuple(value)
    taken_at = raw["taken_at"]
    if not isinstance(taken_at, str) or not taken_at:
        raise ValueError(
            f"El campo 'taken_at' del bloque '{block}' de la sesión {path} "
            "debe ser una cadena no vacía."
        )
    memfds_raw = raw.get("memfds")
    if memfds_raw is None:
        # Sesión anterior a la medición de memfds: se distingue de una lista
        # vacía para no afirmar «ninguno» sobre algo que no se midió.
        memfds: tuple[str, ...] | None = None
    else:
        if not isinstance(memfds_raw, (list, tuple)) or not all(
            isinstance(item, str) for item in memfds_raw
        ):
            raise ValueError(
                f"El campo 'memfds' del bloque '{block}' de la sesión {path} "
                "debe ser una lista de cadenas."
            )
        memfds = tuple(memfds_raw)
    return Snapshot(
        shmem_kb=raw["shmem_kb"],
        mem_available_kb=raw["mem_available_kb"],
        shm_entries=sequences["shm_entries"],
        qemu_pids=sequences["qemu_pids"],
        taken_at=taken_at,
        memfds=memfds,
    )
