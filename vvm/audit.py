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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MEMINFO_PATH = Path("/proc/meminfo")
SHM_PATH = Path("/dev/shm")
PROC_PATH = Path("/proc")

_SESSION_MARKER = b"memory-backend-memfd,id=mem"


@dataclass(frozen=True)
class Snapshot:
    """Medidas del anfitrión en un instante."""

    shmem_kb: int
    mem_available_kb: int
    shm_entries: tuple[str, ...]
    qemu_pids: tuple[str, ...]
    taken_at: str


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
    """True si el `cmdline` del proceso contiene el marcador de esta sesión.

    Sin `cmdline` legible no se puede atribuir la sesión, así que no cuenta.
    """
    try:
        raw = (proc_pid / "cmdline").read_bytes()
    except OSError:
        return False
    return _SESSION_MARKER in raw


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


def build_report(
    before: Snapshot,
    after: Snapshot,
    log_path: str | None = None,
) -> str:
    """Genera el informe de la sesión con las secciones y el veredicto de SPEC §6."""
    new_entries = sorted(set(after.shm_entries) - set(before.shm_entries))
    findings: list[str] = []
    findings.extend(f"entrada nueva en /dev/shm: {name}" for name in new_entries)
    findings.extend(
        f"proceso QEMU vivo al cierre: pid {pid}"
        for pid in sorted(after.qemu_pids, key=_pid_sort_key)
    )

    lines = [
        "Informe de sesión VVM (determinista, sin IA)",
        f"Línea base: {before.taken_at}",
        f"Cierre:     {after.taken_at}",
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
        "SIN HALLAZGOS EN LO COMPROBADO"
        if not findings
        else "HALLAZGOS: " + "; ".join(findings),
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


def save_session(path: Path, before: Snapshot, after: Snapshot) -> None:
    """Guarda la línea base y el cierre en JSON."""
    document = {"before": _snapshot_to_dict(before), "after": _snapshot_to_dict(after)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise RuntimeError(f"No se pudo guardar la sesión en {path}: {exc}") from exc


def load_session(path: Path) -> tuple[Snapshot, Snapshot]:
    """Carga una sesión guardada y devuelve (antes, después)."""
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
    return (
        _snapshot_from_dict(document["before"], "before", path),
        _snapshot_from_dict(document["after"], "after", path),
    )


def _snapshot_to_dict(snapshot: Snapshot) -> dict[str, object]:
    return {
        "shmem_kb": snapshot.shmem_kb,
        "mem_available_kb": snapshot.mem_available_kb,
        "shm_entries": list(snapshot.shm_entries),
        "qemu_pids": list(snapshot.qemu_pids),
        "taken_at": snapshot.taken_at,
    }


_FIELDS = ("shmem_kb", "mem_available_kb", "shm_entries", "qemu_pids", "taken_at")


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
