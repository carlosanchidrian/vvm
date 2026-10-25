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
from vvm.vm import SESSION_MARKER

MEMINFO_PATH = Path("/proc/meminfo")
SHM_PATH = Path("/dev/shm")
PROC_PATH = Path("/proc")

_QEMU_PREFIX = "qemu-system-"
_SESSION_MARKER_BYTES = SESSION_MARKER.encode()


@dataclass(frozen=True)
class Snapshot:
    """Medidas del anfitrión en un instante."""

    shmem_kb: int
    mem_available_kb: int
    shm_entries: tuple[str, ...]
    qemu_pids: tuple[str, ...]
    taken_at: str


@dataclass(frozen=True)
class Session:
    """Sesión guardada: snapshots más el pre-chequeo que la autorizó."""

    before: Snapshot
    after: Snapshot
    checks: tuple[CheckResult, ...] | None = None
    forced: bool = False


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
            kind = "bloqueante" if check.blocking else "no bloqueante"
            lines.append(
                f"   {check.id} {label} ({kind}) {check.vector}: {check.evidence}"
            )
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
) -> str:
    """Genera el informe de la sesión con las secciones y el veredicto de SPEC §6.

    `checks` es el pre-chequeo que autorizó la sesión; `None` significa que la
    sesión guardada no lo incluía (formato antiguo). `forced` indica si se
    arrancó con `--force`.
    """
    new_entries = sorted(set(after.shm_entries) - set(before.shm_entries))
    findings: list[str] = []
    findings.extend(f"entrada nueva en /dev/shm: {name}" for name in new_entries)
    findings.extend(
        f"proceso QEMU vivo al cierre: pid {pid}"
        for pid in sorted(after.qemu_pids, key=_pid_sort_key)
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
        "1. Entradas de /dev/shm",
        f"   Antes:   {_format_entries(before.shm_entries)}",
        f"   Después: {_format_entries(after.shm_entries)}",
        "   Nuevas:  " + (", ".join(new_entries) if new_entries else "ninguna"),
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
        "4. Log serie",
        *_log_lines(log_path),
        "",
        "5. Fuera del alcance de este informe",
        "   - root y ptrace sobre el proceso: no se comprueban.",
        "   - volcado de memoria en vivo (LiME/AVML): no se comprueba.",
        "   - DMA y Thunderbolt: no se comprueban.",
        "   - cold boot: no se comprueba.",
        "   - journald y page cache: pendiente.",
        "   - la propia herramienta (VVM): no se audita a sí misma.",
        "",
        "6. Veredicto",
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


def _delta(before: int, after: int) -> str:
    return f"{after - before:+d}"


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
) -> None:
    """Guarda snapshots y, si se conoce, el pre-chequeo de la sesión en JSON."""
    document: dict[str, object] = {
        "before": _snapshot_to_dict(before),
        "after": _snapshot_to_dict(after),
        "forced": bool(forced),
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
    }


_FIELDS = ("shmem_kb", "mem_available_kb", "shm_entries", "qemu_pids", "taken_at")
_CHECK_FIELDS = ("id", "vector", "status", "blocking", "evidence", "remediation")
_CHECK_STRINGS = ("id", "vector", "status", "evidence", "remediation")


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
    return Snapshot(
        shmem_kb=raw["shmem_kb"],
        mem_available_kb=raw["mem_available_kb"],
        shm_entries=sequences["shm_entries"],
        qemu_pids=sequences["qemu_pids"],
        taken_at=taken_at,
    )
