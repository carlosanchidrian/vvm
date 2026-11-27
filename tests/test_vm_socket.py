"""Línea de QEMU y validación del modo `serial_mode="socket"`."""

from __future__ import annotations

from pathlib import Path

import pytest

from vvm import host_check, profiles
from vvm.vm import VmConfig, build_cmd

SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
SOCKET = "/run/user/1000/vvm/sessions/kali-3f2a/serie.sock"


def _kernel_config(**overrides) -> VmConfig:
    values = {
        "kernel": "/boot/vmlinuz-virt",
        "initrd": "/boot/initramfs-virt",
        "serial_mode": "socket",
        "serial_sock": SOCKET,
    }
    values.update(overrides)
    return VmConfig(**values)


def _image_config(**overrides) -> VmConfig:
    values = {
        "image": "/tmp/live.iso",
        "serial_mode": "socket",
        "serial_sock": SOCKET,
    }
    values.update(overrides)
    return VmConfig(**values)


@pytest.fixture
def host_readers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(host_check, "kvm_accessible", lambda: True)
    monkeypatch.setattr(
        host_check, "read_meminfo", lambda: {"MemAvailable": 16 * 1024 * 1024}
    )
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAPS_HEADER)
    monkeypatch.setattr(host_check, "read_power_state", lambda: "freeze mem\n")
    monkeypatch.setattr(host_check, "read_core_pattern", lambda: "core\n")
    monkeypatch.setattr(host_check, "read_ksm_run", lambda: "0\n")
    monkeypatch.setattr(host_check, "read_shmem_enabled", lambda: "[never] always\n")
    monkeypatch.setattr(host_check, "list_shm_entries", lambda: [])


def test_socket_en_modo_kernel():
    cmd = build_cmd(_kernel_config(), disk_fd=3)

    assert cmd[cmd.index("-monitor") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1] == "chardev:ser0"
    chardev = cmd[cmd.index("-chardev") + 1]
    assert chardev == (
        f"socket,id=ser0,path={SOCKET},server=on,wait=off"
    )
    assert "mon:stdio" not in cmd
    assert "-qmp" not in cmd
    assert "memory-backend-memfd,id=mem,size=512M,share=off" in cmd
    assert "q35,memory-backend=mem,dump-guest-core=off,mem-merge=off" in cmd
    assert cmd[cmd.index("-nic") + 1] == "none"
    assert cmd[cmd.index("-display") + 1] == "none"
    assert "file=/dev/fd/3,format=raw,if=virtio,cache=writethrough" in cmd


def test_socket_en_modo_imagen_mantiene_la_ventana():
    cmd = build_cmd(
        _image_config(disk_mb=0),
        disk_fd=None,
        image_fd=9,
    )

    assert cmd[cmd.index("-display") + 1] == "gtk"
    assert cmd[cmd.index("-monitor") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1] == "chardev:ser0"
    assert "file:/" not in " ".join(cmd)
    assert "file=/dev/fd/9,format=raw,media=cdrom,readonly=on" in cmd
    assert "-qmp" not in cmd


def test_headless_con_socket_es_valido():
    cmd = build_cmd(
        _kernel_config(headless=True),
        disk_fd=3,
    )

    assert cmd[cmd.index("-display") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1] == "chardev:ser0"
    assert "-monitor" in cmd


def test_validacion_de_los_cinco_casos():
    with pytest.raises(ValueError, match="'stdio', 'file', 'socket'"):
        build_cmd(_kernel_config(serial_mode="raro"), disk_fd=3)

    with pytest.raises(
        ValueError, match="necesita un socket serie \\(serial_sock\\)"
    ):
        build_cmd(_kernel_config(serial_sock=None), disk_fd=3)

    with pytest.raises(
        ValueError, match="serial_sock solo vale con serial_mode='socket'"
    ):
        build_cmd(
            _kernel_config(
                serial_mode="stdio", serial_sock="/tmp/serie.sock"
            ),
            disk_fd=3,
        )

    with pytest.raises(
        ValueError, match="serial_log no puede ser una segunda consola"
    ):
        build_cmd(
            _kernel_config(serial_log="/tmp/serie.log"), disk_fd=3
        )

    with pytest.raises(ValueError, match="socket serie en la TUI"):
        build_cmd(
            VmConfig(
                kernel="/boot/vmlinuz-virt",
                initrd="/boot/initramfs-virt",
                headless=True,
            ),
            disk_fd=3,
        )


def test_p7_y_p9_siguen_ok_con_la_linea_del_socket(host_readers):
    cmd = build_cmd(_kernel_config(), disk_fd=3)

    results = host_check.check_host(256, 8, cmd)
    by_id = {result.id: result for result in results}

    assert by_id["P7"].status == "OK"
    assert by_id["P9"].status == "OK"


def test_el_perfil_toml_no_admite_serial_sock(tmp_path: Path):
    path = tmp_path / "profiles.toml"
    path.write_text(
        """
[profiles.min]
kernel = "/boot/vmlinuz"
initrd = "/boot/initramfs"
serial_sock = "/tmp/serie.sock"
""",
        encoding="utf-8",
    )

    with pytest.raises(profiles.ProfilesError, match="serial_sock"):
        profiles.load_profiles(path)
