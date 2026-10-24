"""Artefactos volátiles del guest (SPEC §3, §5).

- Disco en `memfd`: sin ruta en disco y sin entrada en `/dev/shm`.
- Validación de los ficheros de arranque que recibe el CLI.
"""

from __future__ import annotations


def create_disk_fd(size_mb: int) -> int:
    """Crea el disco del guest en un `memfd` del tamaño pedido y devuelve su fd.

    El llamador es responsable de cerrarlo (`os.close`).
    """
    raise NotImplementedError("contrato sin implementar: tramo 2")


def validate_boot_files(kernel: str, initrd: str) -> None:
    """Comprueba que kernel e initrd existen y son ficheros regulares no vacíos.

    Lanza `FileNotFoundError`/`ValueError` con mensaje en español y la ruta cruda.
    """
    raise NotImplementedError("contrato sin implementar: tramo 2")
