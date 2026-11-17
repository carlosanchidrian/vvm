"""Tests de la línea de QEMU y del lanzador (SPEC §5, §7)."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest

from vvm.vm import (
    ProcessIO,
    SignalGuard,
    VmConfig,
    build_cmd,
    read_process_io,
    run_session,
    run_vm,
)


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


# Bucle acotado (~20 s) para que un fake que nunca recibe la señal no cuelgue
# la suite indefinidamente.
_FAKE_LOOP = "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"


def _fake_qemu(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-qemu"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _signal_when_ready(marker: Path, signum: int = signal.SIGTERM) -> threading.Thread:
    def waiter() -> None:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signum)

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    return thread


def test_read_process_io_reads_the_four_counters(tmp_path: Path):
    proc = tmp_path / "123"
    proc.mkdir()
    (proc / "io").write_text(
        "rchar: 10\nwchar: 20\nsyscr: 1\nsyscw: 2\n"
        "read_bytes: 30\nwrite_bytes: 40\ncancelled_write_bytes: 0\n",
        encoding="utf-8",
    )

    assert read_process_io(123, str(tmp_path)) == ProcessIO(
        rchar=10, wchar=20, read_bytes=30, write_bytes=40
    )


def test_read_process_io_missing_or_incomplete_returns_none(tmp_path: Path):
    assert read_process_io(999, str(tmp_path)) is None

    proc = tmp_path / "123"
    proc.mkdir()
    (proc / "io").write_text("rchar: 10\n", encoding="utf-8")

    assert read_process_io(123, str(tmp_path)) is None


def test_run_session_normal_exit_returns_child_code():
    outcome = run_session(["/bin/true"], io_poll=0.01)

    assert outcome.returncode == 0
    assert outcome.interrupt is None
    assert outcome.killed_after is None


def test_run_session_forwards_sigterm_and_returns_143(tmp_path: Path):
    marker = tmp_path / "started"
    script = _fake_qemu(
        tmp_path,
        (
            "sleep 0.2\n"
            f"echo $$ > {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    sender = _signal_when_ready(marker)

    outcome = run_session([str(script)], term_wait=1.0, io_poll=0.01)
    sender.join()

    assert outcome.returncode == 143
    assert outcome.interrupt == signal.SIGTERM
    assert outcome.killed_after is None
    child = int(marker.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_run_session_forwards_sighup_and_returns_129(tmp_path: Path):
    marker = tmp_path / "started"
    script = _fake_qemu(
        tmp_path,
        (
            "sleep 0.2\n"
            f"echo $$ > {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    sender = _signal_when_ready(marker, signal.SIGHUP)

    outcome = run_session([str(script)], term_wait=1.0, io_poll=0.01)
    sender.join()

    assert outcome.returncode == 129
    assert outcome.interrupt == signal.SIGHUP


def test_run_session_kills_child_that_ignores_sigterm(tmp_path: Path):
    marker = tmp_path / "started"
    script = _fake_qemu(
        tmp_path,
        (
            "sleep 0.2\n"
            f"echo $$ > {marker}\n"
            "trap '' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    sender = _signal_when_ready(marker)

    outcome = run_session([str(script)], term_wait=0.2, io_poll=0.01)
    sender.join()

    assert outcome.returncode == 143
    assert outcome.interrupt == signal.SIGTERM
    assert outcome.killed_after == 0.2
    child = int(marker.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_run_session_reports_process_io_of_a_live_child(tmp_path: Path):
    script = _fake_qemu(
        tmp_path,
        (
            "dd if=/dev/zero of=/dev/null bs=64k count=8 2>/dev/null\n"
            "sleep 0.4\n"
        ),
    )

    outcome = run_session([str(script)], io_poll=0.01)

    assert outcome.returncode == 0
    assert outcome.process_io is not None
    assert outcome.process_io.rchar > 0


def test_run_session_sigint_returns_130_and_leaves_no_child(tmp_path: Path):
    marker = tmp_path / "started"
    script = _fake_qemu(
        tmp_path,
        (
            "sleep 0.2\n"
            f"echo $$ > {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    sender = _signal_when_ready(marker, signal.SIGINT)

    outcome = run_session([str(script)], term_wait=1.0, io_poll=0.01)
    sender.join()

    assert outcome.returncode == 130
    assert outcome.interrupt == signal.SIGINT
    child = int(marker.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_run_session_second_signal_measures_time_from_the_first(tmp_path: Path):
    marker = tmp_path / "started"
    script = _fake_qemu(
        tmp_path,
        (
            "sleep 0.2\n"
            f"echo $$ > {marker}\n"
            "trap '' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )

    def sender() -> None:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=sender, daemon=True)
    thread.start()
    outcome = run_session([str(script)], term_wait=5.0, io_poll=0.01)
    thread.join(timeout=5)

    assert outcome.returncode == 143
    assert outcome.interrupt == signal.SIGTERM
    assert outcome.killed_after is not None
    # El informe no debe decir «0 s»: se mide desde la primera señal.
    assert 0.2 <= outcome.killed_after < 4.0


def test_run_session_from_non_main_thread_raises_without_spawning(tmp_path: Path):
    marker = tmp_path / "started"
    script = _fake_qemu(
        tmp_path,
        f"echo $$ > {marker}\n" + _FAKE_LOOP,
    )
    errors: list[BaseException] = []

    def target() -> None:
        try:
            run_session([str(script)], io_poll=0.01)
        except BaseException as exc:  # noqa: BLE001 - se comprueba el tipo abajo
            errors.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout=10)

    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert not marker.exists()


def test_run_session_with_external_guard_does_not_restore_handlers():
    guard = SignalGuard()
    guard.install()
    try:
        handler = signal.getsignal(signal.SIGTERM)

        outcome = run_session(["/bin/true"], io_poll=0.01, guard=guard)

        assert outcome.returncode == 0
        assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        guard.restore()


def test_signal_guard_forwards_signal_recorded_before_attach(tmp_path: Path):
    marker = tmp_path / "started"
    guard = SignalGuard()
    guard.install()
    proc = subprocess.Popen(
        [
            "/bin/sh",
            "-c",
            f"trap 'exit 0' TERM; echo $$ > {marker}; "
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done",
        ]
    )
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        # Simula la señal llegada entre `install()` y `Popen`.
        guard.interrupts.append(signal.SIGTERM)
        guard.first_signal_at = time.monotonic()

        guard.attach(proc)
        proc.wait(timeout=5)

        assert proc.returncode == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        guard.restore()
