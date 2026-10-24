"""Línea de QEMU y ciclo de vida de la VM (SPEC §5).

Invariantes de `build_cmd()`:
- `-m <mem>M` y `size=<mem>M` coinciden exactamente.
- El backend `memfd` lleva `share=off`; `-machine` lleva
  `dump-guest-core=off,mem-merge=off`; hay `-nic none`.
- El disco se pasa como `file=/dev/fd/<n>`, no como ruta.
"""

from __future__ import annotations

import resource
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass

# Marcador que identifica el backend de memoria de una sesión VVM en `cmdline`.
SESSION_MARKER = "memory-backend-memfd,id=mem"


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
    if config.headless and not config.serial_log:
        raise ValueError(
            "El modo headless necesita un log serie (--log): "
            "sin fichero de salida la consola del guest se perdería"
        )

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
        "-kernel",
        config.kernel,
        "-initrd",
        config.initrd,
        "-append",
        "console=ttyS0",
        "-drive",
        f"file=/dev/fd/{disk_fd},format=raw,if=virtio,cache=writethrough",
        "-nic",
        "none",
        "-display",
        "none",
    ]
    if config.headless:
        cmd += ["-monitor", "none", "-serial", f"file:{config.serial_log}"]
    else:
        cmd += ["-serial", "mon:stdio"]
    return cmd


def _limit_core() -> None:
    """Fija `RLIMIT_CORE=0` en el proceso hijo (sin volcados a disco)."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_vm(cmd: Sequence[str], pass_fds: Sequence[int] = ()) -> int:
    """Lanza QEMU heredando los fds indicados y devuelve su código de salida.

    En el hijo fija `RLIMIT_CORE=0`. No captura la salida: la sesión es interactiva.
    """
    completed = subprocess.run(
        list(cmd),
        pass_fds=tuple(pass_fds),
        preexec_fn=_limit_core,
        check=False,
    )
    return completed.returncode
