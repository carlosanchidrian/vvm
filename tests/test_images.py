"""Tests de los artefactos volátiles del guest (SPEC §5, §7)."""

from __future__ import annotations

import os

import pytest

from vvm.images import create_disk_fd, validate_boot_files


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
