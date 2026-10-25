"""Artefactos volátiles del guest (SPEC §3, §5).

- Disco en `memfd`: sin ruta en disco y sin entrada en `/dev/shm`.
- Validación de los ficheros de arranque que recibe el CLI.
"""

from __future__ import annotations

import os


def create_disk_fd(size_mb: int) -> int:
    """Crea el disco del guest en un `memfd` del tamaño pedido y devuelve su fd.

    El llamador es responsable de cerrarlo (`os.close`).
    """
    if size_mb <= 0:
        raise ValueError(f"El tamaño del disco debe ser mayor que cero: {size_mb} MiB")

    fd = os.memfd_create("vvm-disk", flags=0)
    try:
        os.ftruncate(fd, size_mb * 1024 * 1024)
    except OSError as exc:
        os.close(fd)
        raise OSError(
            f"No se pudo dimensionar el disco del guest a {size_mb} MiB "
            f"(memfd): {exc}"
        ) from exc
    return fd


def validate_boot_files(kernel: str, initrd: str) -> None:
    """Comprueba que kernel e initrd existen y son ficheros regulares no vacíos.

    Lanza `FileNotFoundError`/`ValueError` con mensaje en español y la ruta cruda.
    """
    for label, path in (("kernel", kernel), ("initrd", initrd)):
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"No existe el fichero de arranque ({label}): {path}"
            )
        if not os.path.isfile(path):
            raise ValueError(
                f"El fichero de arranque ({label}) no es un fichero regular: {path}"
            )
        if os.path.getsize(path) <= 0:
            raise ValueError(
                f"El fichero de arranque ({label}) está vacío: {path}"
            )
