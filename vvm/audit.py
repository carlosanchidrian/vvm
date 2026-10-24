"""Línea base, cierre e informe determinista (SPEC §6).

Sin IA: el informe se compone de medidas antes/después y de un veredicto
calculado. La sesión se guarda en JSON para poder auditarla después.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Snapshot:
    """Medidas del anfitrión en un instante."""

    shmem_kb: int
    mem_available_kb: int
    shm_entries: tuple[str, ...]
    qemu_pids: tuple[str, ...]
    taken_at: str


def take_snapshot() -> Snapshot:
    """Toma una foto de Shmem, MemAvailable, /dev/shm y procesos QEMU vivos."""
    raise NotImplementedError("contrato sin implementar: tramo 3")


def build_report(
    before: Snapshot,
    after: Snapshot,
    log_path: str | None = None,
) -> str:
    """Genera el informe de la sesión con las secciones y el veredicto de SPEC §6."""
    raise NotImplementedError("contrato sin implementar: tramo 3")


def default_session_path() -> Path:
    """Ruta por defecto de la última sesión: `$XDG_STATE_HOME/vvm/last-session.json`."""
    raise NotImplementedError("contrato sin implementar: tramo 3")


def save_session(path: Path, before: Snapshot, after: Snapshot) -> None:
    """Guarda la línea base y el cierre en JSON."""
    raise NotImplementedError("contrato sin implementar: tramo 3")


def load_session(path: Path) -> tuple[Snapshot, Snapshot]:
    """Carga una sesión guardada y devuelve (antes, después)."""
    raise NotImplementedError("contrato sin implementar: tramo 3")
