"""Línea de QEMU y ciclo de vida de la VM (SPEC §5).

Invariantes de `build_cmd()`:
- `-m <mem>M` y `size=<mem>M` coinciden exactamente.
- El backend `memfd` lleva `share=off`; `-machine` lleva
  `dump-guest-core=off,mem-merge=off`; hay `-nic none`.
- El disco se pasa como `file=/dev/fd/<n>`, no como ruta.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class VmConfig:
    """Parámetros de una sesión."""

    kernel: str
    initrd: str
    mem_mb: int = 512
    disk_mb: int = 256
    cpus: int = 1
    headless: bool = False
    serial_log: str | None = None


def build_cmd(
    config: VmConfig,
    disk_fd: int,
    qemu: str = "qemu-system-x86_64",
) -> list[str]:
    """Construye la línea de QEMU. No lanza ningún proceso."""
    raise NotImplementedError("contrato sin implementar: tramo 2")


def run_vm(cmd: Sequence[str], pass_fds: Sequence[int] = ()) -> int:
    """Lanza QEMU heredando los fds indicados y devuelve su código de salida.

    En el hijo fija `RLIMIT_CORE=0`. No captura la salida: la sesión es interactiva.
    """
    raise NotImplementedError("contrato sin implementar: tramo 2")
