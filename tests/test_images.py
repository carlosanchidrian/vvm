"""Tests de los artefactos volátiles del guest (SPEC §5, §7)."""

from __future__ import annotations

import os

import pytest

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
