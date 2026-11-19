"""Tests de los artefactos volátiles del guest (SPEC §5, §7)."""

from __future__ import annotations

import os

import pytest

from vvm import images
from vvm.images import (
    create_disk_fd,
    create_image_fd,
    validate_boot_files,
    validate_image,
    wipe_fd,
)


def test_create_disk_fd_is_256_mib():
    fd = create_disk_fd(256)
    try:
        assert os.fstat(fd).st_size == 256 * 1024 * 1024
    finally:
        os.close(fd)


def test_create_disk_fd_rejects_non_positive_size():
    with pytest.raises(ValueError, match="mayor que cero"):
        create_disk_fd(0)


def test_validate_boot_files_accepts_regular_non_empty_files(tmp_path):
    kernel = tmp_path / "vmlinuz-virt"
    initrd = tmp_path / "initramfs-virt"
    kernel.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")

    validate_boot_files(str(kernel), str(initrd))


def test_validate_boot_files_missing_file_names_raw_path(tmp_path):
    kernel = tmp_path / "vmlinuz-virt"
    initrd = tmp_path / "initramfs-virt"
    kernel.write_bytes(b"kernel")

    with pytest.raises(FileNotFoundError) as excinfo:
        validate_boot_files(str(kernel), str(initrd))
    assert str(initrd) in str(excinfo.value)


def test_validate_boot_files_rejects_directory(tmp_path):
    kernel = tmp_path / "vmlinuz-virt"
    initrd = tmp_path / "initramfs-virt"
    kernel.write_bytes(b"kernel")
    initrd.mkdir()

    with pytest.raises(ValueError, match="fichero regular") as excinfo:
        validate_boot_files(str(kernel), str(initrd))
    assert str(initrd) in str(excinfo.value)


def test_validate_boot_files_rejects_empty_file(tmp_path):
    kernel = tmp_path / "vmlinuz-virt"
    initrd = tmp_path / "initramfs-virt"
    kernel.write_bytes(b"")
    initrd.write_bytes(b"initrd")

    with pytest.raises(ValueError, match="vacío") as excinfo:
        validate_boot_files(str(kernel), str(initrd))
    assert str(kernel) in str(excinfo.value)


def test_validate_image_returns_size_without_reading(tmp_path):
    iso = tmp_path / "kali.iso"
    iso.write_bytes(b"x" * 4096)

    assert validate_image(str(iso)) == 4096


def test_validate_image_missing_names_raw_path(tmp_path):
    missing = tmp_path / "no-existe.iso"

    with pytest.raises(FileNotFoundError, match="--image no encontrada") as excinfo:
        validate_image(str(missing))
    assert str(missing) in str(excinfo.value)


def test_validate_image_rejects_directory(tmp_path):
    directory = tmp_path / "carpeta.iso"
    directory.mkdir()

    with pytest.raises(ValueError, match="fichero regular") as excinfo:
        validate_image(str(directory))
    assert str(directory) in str(excinfo.value)


def test_validate_image_rejects_empty_file(tmp_path):
    iso = tmp_path / "vacia.iso"
    iso.write_bytes(b"")

    with pytest.raises(ValueError, match="vacía") as excinfo:
        validate_image(str(iso))
    assert str(iso) in str(excinfo.value)


def test_create_image_fd_copies_content_to_anonymous_memory(tmp_path):
    iso = tmp_path / "live.iso"
    payload = b"ISO" * 1000
    iso.write_bytes(payload)

    fd = create_image_fd(str(iso))
    try:
        assert os.fstat(fd).st_size == len(payload)
        assert os.pread(fd, len(payload), 0) == payload
        # El memfd no tiene ruta: no aparece en /dev/shm ni en el sistema de ficheros.
        assert os.readlink(f"/proc/self/fd/{fd}").startswith("/memfd:vvm-iso")
    finally:
        os.close(fd)


def test_create_image_fd_copies_large_file_in_blocks(tmp_path):
    iso = tmp_path / "live.iso"
    payload = bytes(range(256)) * (3 * 1024 * 1024 // 256)
    iso.write_bytes(payload)

    fd = create_image_fd(str(iso))
    try:
        assert os.fstat(fd).st_size == len(payload)
        assert os.pread(fd, len(payload), 0) == payload
    finally:
        os.close(fd)


def test_wipe_fd_zeroes_the_whole_medium_and_keeps_its_size():
    size = 2 * 1024 * 1024
    fd = os.memfd_create("vvm-wipe")
    try:
        os.ftruncate(fd, size)
        os.pwrite(fd, b"PATRON" * 1000, 0)
        os.pwrite(fd, b"OTRO" * 1000, size // 2)

        result = wipe_fd(fd)

        assert os.fstat(fd).st_size == size
        assert result.size_mb == 2
        assert result.seconds >= 0
        assert os.pread(fd, size, 0) == b"\0" * size
    finally:
        os.close(fd)


def test_wipe_fd_rounds_up_the_reported_size():
    size = 2 * 1024 * 1024 + 17
    fd = os.memfd_create("vvm-wipe")
    try:
        os.ftruncate(fd, size)

        result = wipe_fd(fd)

        assert os.fstat(fd).st_size == size
        assert result.size_mb == 3
        assert os.pread(fd, size, 0) == b"\0" * size
    finally:
        os.close(fd)


class _ShortSource:
    """Lector que anuncia un tamaño y devuelve la mitad: copia corta."""

    def __init__(self, handle, limit: int):
        self._handle = handle
        self._left = limit

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self._handle.close()
        return False

    def read(self, size: int) -> bytes:
        if self._left <= 0:
            return b""
        chunk = self._handle.read(min(size, self._left))
        self._left -= len(chunk)
        return chunk


def test_create_image_fd_fails_on_short_copy_and_closes_the_fd(tmp_path, monkeypatch):
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"A" * (2 * 1024 * 1024))
    created: list[int] = []
    real_memfd = os.memfd_create
    real_open = open

    def capture_memfd(name: str, flags: int = 0) -> int:
        fd = real_memfd(name, flags)
        created.append(fd)
        return fd

    def short_open(path, mode="r", *args, **kwargs):
        return _ShortSource(real_open(path, mode, *args, **kwargs), 1024 * 1024)

    monkeypatch.setattr(images.os, "memfd_create", capture_memfd)
    monkeypatch.setattr(images, "open", short_open, raising=False)

    with pytest.raises(OSError) as excinfo:
        create_image_fd(str(iso))

    mensaje = str(excinfo.value)
    assert "acortó" in mensaje
    assert str(iso) in mensaje
    assert "1048576 de 2097152" in mensaje
    assert created and not _is_open(created[0])


def test_create_image_fd_closes_the_fd_on_unexpected_error(tmp_path, monkeypatch):
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"x" * 4096)
    created: list[int] = []
    real_memfd = os.memfd_create

    def capture_memfd(name: str, flags: int = 0) -> int:
        fd = real_memfd(name, flags)
        created.append(fd)
        return fd

    def interrupted(
        path: str, fd: int, size: int, should_stop=None
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(images.os, "memfd_create", capture_memfd)
    monkeypatch.setattr(images, "_copy_into_memfd", interrupted)

    with pytest.raises(KeyboardInterrupt):
        create_image_fd(str(iso))

    assert created and not _is_open(created[0])


def test_create_image_fd_stops_between_blocks_and_closes_the_fd(
    tmp_path, monkeypatch
):
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"A" * (3 * 1024 * 1024))
    created: list[int] = []
    real_memfd = os.memfd_create

    def capture_memfd(name: str, flags: int = 0) -> int:
        fd = real_memfd(name, flags)
        created.append(fd)
        return fd

    monkeypatch.setattr(images.os, "memfd_create", capture_memfd)
    calls: list[int] = []

    def should_stop() -> bool:
        calls.append(1)
        return len(calls) > 1

    with pytest.raises(InterruptedError) as excinfo:
        create_image_fd(str(iso), should_stop=should_stop)

    mensaje = str(excinfo.value)
    assert "interrumpió la copia" in mensaje
    assert "1048576 de 3145728" in mensaje
    assert len(calls) == 2
    assert created and not _is_open(created[0])


def test_pwrite_all_no_gira_para_siempre_si_la_escritura_es_cero(monkeypatch):
    monkeypatch.setattr(images.os, "pwrite", lambda fd, data, offset: 0)

    with pytest.raises(OSError, match="escritura incompleta"):
        images._pwrite_all(1, b"x" * 10, 0)


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True
