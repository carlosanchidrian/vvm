"""Tests de la línea de QEMU y del lanzador (SPEC §5, §7)."""

from __future__ import annotations

import pytest

from vvm.vm import VmConfig, build_cmd, run_vm


def make_config(**overrides) -> VmConfig:
    params = {
        "kernel": "/boot/vmlinuz-virt",
        "initrd": "/boot/initramfs-virt",
    }
    params.update(overrides)
    return VmConfig(**params)


def test_build_cmd_carries_volatility_flags():
    cmd = build_cmd(make_config(mem_mb=1024), disk_fd=7, qemu="qemu-system-x86_64")

    assert cmd[0] == "qemu-system-x86_64"
    assert "memory-backend-memfd,id=mem,size=1024M,share=off" in cmd
    assert "q35,memory-backend=mem,dump-guest-core=off,mem-merge=off" in cmd
    assert "file=/dev/fd/7,format=raw,if=virtio,cache=writethrough" in cmd
    assert cmd[cmd.index("-nic") + 1] == "none"
    assert cmd[cmd.index("-display") + 1] == "none"


def test_build_cmd_matches_m_and_backend_size():
    cmd = build_cmd(make_config(mem_mb=768, cpus=2), disk_fd=3)

    mem_arg = cmd[cmd.index("-m") + 1]
    backend = cmd[cmd.index("-object") + 1]

    assert mem_arg == "768M"
    assert f"size={mem_arg}" in backend
    assert cmd[cmd.index("-smp") + 1] == "2"


def test_build_cmd_interactive_uses_stdio_serial():
    cmd = build_cmd(make_config(), disk_fd=3)

    assert cmd[cmd.index("-serial") + 1] == "mon:stdio"
    assert "-monitor" not in cmd


def test_build_cmd_headless_uses_monitor_none_and_log():
    config = make_config(headless=True, serial_log="/tmp/vvm-serial.log")
    cmd = build_cmd(config, disk_fd=3)

    assert cmd[cmd.index("-monitor") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1] == "file:/tmp/vvm-serial.log"


def test_build_cmd_headless_without_log_raises_value_error():
    with pytest.raises(ValueError, match="headless"):
        build_cmd(make_config(headless=True), disk_fd=3)


def test_run_vm_returns_child_returncode():
    assert run_vm(["/bin/true"]) == 0


def test_build_cmd_kernel_mode_line_is_unchanged():
    cmd = build_cmd(make_config(), disk_fd=7)

    assert cmd == [
        "qemu-system-x86_64",
        "-accel",
        "kvm",
        "-cpu",
        "host",
        "-smp",
        "1",
        "-m",
        "512M",
        "-object",
        "memory-backend-memfd,id=mem,size=512M,share=off",
        "-machine",
        "q35,memory-backend=mem,dump-guest-core=off,mem-merge=off",
        "-kernel",
        "/boot/vmlinuz-virt",
        "-initrd",
        "/boot/initramfs-virt",
        "-append",
        "console=ttyS0",
        "-drive",
        "file=/dev/fd/7,format=raw,if=virtio,cache=writethrough",
        "-nic",
        "none",
        "-display",
        "none",
        "-serial",
        "mon:stdio",
    ]


def test_build_cmd_image_mode_uses_window_and_cdrom_by_fd():
    cmd = build_cmd(
        make_config(kernel=None, initrd=None, image="/tmp/kali.iso", mem_mb=4096),
        disk_fd=7,
        image_fd=9,
    )

    assert "memory-backend-memfd,id=mem,size=4096M,share=off" in cmd
    assert "q35,memory-backend=mem,dump-guest-core=off,mem-merge=off" in cmd
    assert "-nodefaults" in cmd
    assert cmd[cmd.index("-vga") + 1] == "std"
    assert "qemu-xhci" in cmd
    assert "usb-tablet" in cmd
    assert "file=/dev/fd/9,format=raw,media=cdrom,readonly=on" in cmd
    assert cmd[cmd.index("-boot") + 1] == "d"
    assert "file=/dev/fd/7,format=raw,if=virtio,cache=writethrough" in cmd
    assert cmd[cmd.index("-display") + 1] == "gtk"
    assert cmd[cmd.index("-serial") + 1] == "mon:stdio"
    assert "-kernel" not in cmd
    assert "-initrd" not in cmd
    assert "-append" not in cmd
    assert "console=ttyS0" not in cmd


def test_build_cmd_image_does_not_use_headless_or_serial_log():
    cmd = build_cmd(
        make_config(kernel=None, initrd=None, image="/tmp/live.iso"),
        disk_fd=7,
        image_fd=9,
    )

    assert "-monitor" not in cmd
    assert "-serial" in cmd
    assert cmd[cmd.index("-serial") + 1] == "mon:stdio"


def test_build_cmd_image_and_kernel_are_exclusive():
    config = make_config(image="/tmp/live.iso")

    with pytest.raises(ValueError, match="excluyentes"):
        build_cmd(config, disk_fd=7, image_fd=9)


def test_build_cmd_image_with_headless_is_rejected():
    config = make_config(
        kernel=None,
        initrd=None,
        image="/tmp/live.iso",
        headless=True,
        serial_log="/tmp/serial.log",
    )

    with pytest.raises(ValueError, match="headless"):
        build_cmd(config, image_fd=9)


def test_build_cmd_kernel_without_initrd_is_rejected():
    with pytest.raises(ValueError, match="--kernel sin --initrd"):
        build_cmd(VmConfig(kernel="/boot/vmlinuz"), disk_fd=7)


def test_build_cmd_without_boot_source_is_rejected():
    with pytest.raises(ValueError, match="fuente de arranque"):
        build_cmd(VmConfig(), disk_fd=7)


def test_build_cmd_image_without_image_fd_is_rejected():
    with pytest.raises(ValueError, match="descriptor de la imagen"):
        build_cmd(
            make_config(kernel=None, initrd=None, image="/tmp/live.iso"),
            disk_fd=7,
        )


def test_build_cmd_disk_zero_omits_data_drive():
    cmd = build_cmd(
        make_config(kernel=None, initrd=None, image="/tmp/live.iso", disk_mb=0),
        disk_fd=None,
        image_fd=9,
    )

    assert not any("if=virtio" in token for token in cmd)
    assert "file=/dev/fd/9,format=raw,media=cdrom,readonly=on" in cmd


def test_build_cmd_image_disk_256_keeps_data_drive():
    cmd = build_cmd(
        make_config(kernel=None, initrd=None, image="/tmp/live.iso"),
        disk_fd=7,
        image_fd=9,
    )

    assert "file=/dev/fd/7,format=raw,if=virtio,cache=writethrough" in cmd


def test_build_cmd_accepts_placeholder_for_dry_run():
    cmd = build_cmd(
        make_config(kernel=None, initrd=None, image="/tmp/live.iso"),
        disk_fd="<fd>",
        image_fd="<fd>",
    )

    assert "file=/dev/fd/<fd>,format=raw,media=cdrom,readonly=on" in cmd
    assert "file=/dev/fd/<fd>,format=raw,if=virtio,cache=writethrough" in cmd
