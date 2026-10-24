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
