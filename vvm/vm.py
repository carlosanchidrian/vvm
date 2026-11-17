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
