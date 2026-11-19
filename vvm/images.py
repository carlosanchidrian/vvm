"""Artefactos volátiles del guest (SPEC §3, §5).

- Disco en `memfd`: sin ruta en disco y sin entrada en `/dev/shm`.
- Imagen ISO en `memfd`: se copia en bloques y nunca se pasa por ruta.
- Validación de los ficheros de arranque que recibe el CLI.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

_MIB = 1024 * 1024
_BLOCK_SIZE = _MIB


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


def validate_image(path: str) -> int:
    """Comprueba la imagen y devuelve su tamaño en bytes.

    No lee el contenido: solo `stat`, para que `--dry-run` y `check` no copien
    gigabytes de ISO.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"--image no encontrada: {path}")
    if not os.path.isfile(path):
        raise ValueError(f"--image no es un fichero regular: {path}")
    size = os.path.getsize(path)
    if size <= 0:
        raise ValueError(f"--image está vacía: {path}")
    return size


def create_image_fd(
    path: str, *, should_stop: Callable[[], bool] | None = None
) -> int:
    """Copia la imagen a un `memfd` anónimo y devuelve su fd.

    El medio se copia en bloques de 1 MiB; no se crea ninguna ruta ni entrada
    en `/dev/shm`. El llamador es responsable de cerrar el fd. Si la copia falla
    o queda corta (el fichero se acorta entre el `stat` y la lectura), se cierra
    el fd y se lanza `OSError`: nunca se devuelve un ISO con cola de ceros.

    `should_stop` se consulta antes de cada bloque de 1 MiB; si devuelve `True`
    se aborta con `InterruptedError` y el fd se cierra, para que una señal no
    obligue a copiar gigabytes antes de atenderla.
    """
    size = validate_image(path)
    fd = os.memfd_create("vvm-iso", flags=0)
    try:
        os.ftruncate(fd, size)
        _copy_into_memfd(path, fd, size, should_stop)
    except BaseException:
        # No solo OSError: también KeyboardInterrupt o MemoryError cierran el fd.
        os.close(fd)
        raise
    return fd


def _copy_into_memfd(
    path: str,
    fd: int,
    size: int,
    should_stop: Callable[[], bool] | None = None,
) -> None:
    """Copia `size` bytes de `path` a `fd` y falla si la copia queda corta."""
    offset = 0
    remaining = size
    try:
        with open(path, "rb") as source:
            while remaining > 0:
                if should_stop is not None and should_stop():
                    raise InterruptedError(
                        f"Se interrumpió la copia de la imagen a memoria ({path}) "
                        f"tras {offset} de {size} bytes"
                    )
                chunk = source.read(min(_BLOCK_SIZE, remaining))
                if not chunk:
                    break
                _pwrite_all(fd, chunk, offset)
                offset += len(chunk)
                remaining -= len(chunk)
    except InterruptedError:
        # Es un OSError, pero no se reescribe: el mensaje ya dice que se abortó.
        raise
    except OSError as exc:
        raise OSError(
            f"No se pudo copiar la imagen a memoria ({path}) tras "
            f"{offset} de {size} bytes: {exc}"
        ) from exc
    if offset != size:
        raise OSError(
            f"La imagen se acortó durante la copia ({path}): "
            f"{offset} de {size} bytes copiados"
        )


def _pwrite_all(fd: int, data: bytes, offset: int) -> None:
    """Escribe `data` completo en `fd` desde `offset`, tolerando escrituras parciales."""
    written = 0
    while written < len(data):
        chunk = os.pwrite(fd, data[written:], offset + written)
        if chunk <= 0:
            raise OSError(
                "escritura incompleta en el memfd: "
                f"{written} de {len(data)} bytes en el desplazamiento {offset}"
            )
        written += chunk


@dataclass(frozen=True)
class WipeResult:
    """Resultado de poner a cero un medio: tamaño y tiempo empleado."""

    size_mb: int
    seconds: float


def wipe_fd(fd: int) -> WipeResult:
    """Sobrescribe con ceros todo el contenido del `memfd`, en bloques de 1 MiB.

    No cambia el tamaño del medio. El llamador decide cuándo cerrar el fd.
    """
    size = os.fstat(fd).st_size
    if size <= 0:
        return WipeResult(size_mb=0, seconds=0.0)

    block = b"\0" * _MIB
    started = time.monotonic()
    offset = 0
    while offset < size:
        chunk_len = min(_MIB, size - offset)
        _pwrite_all(fd, block[:chunk_len], offset)
        offset += chunk_len
    seconds = time.monotonic() - started
    return WipeResult(
        size_mb=(size + _MIB - 1) // _MIB,
        seconds=seconds,
    )


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
