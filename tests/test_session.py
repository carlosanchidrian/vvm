"""Tests de `vvm.session`: ciclo de vida sin KVM ni QEMU real."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from vvm import audit
from vvm.session import IOStats, Session
from vvm.vm import ProcessIO, VmConfig


def _snapshot(**overrides):
    data = {
        "shmem_kb": 1000,
        "mem_available_kb": 4_000_000,
        "shm_entries": (),
        "qemu_pids": (),
        "taken_at": "2026-10-24T10:00:00Z",
        "memfds": (),
    }
    data.update(overrides)
    return audit.Snapshot(**data)


def _boot_files(tmp_path: Path) -> tuple[Path, Path]:
    kernel = tmp_path / "vmlinuz"
    initrd = tmp_path / "initramfs"
    kernel.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")
    return kernel, initrd


def _fake_qemu(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-qemu"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"no apareció {path}")
        time.sleep(0.02)


def _kill_marked_child(marker: Path) -> None:
    try:
        child = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    qemu: str = "/bin/true",
    disk_mb: int = 8,
) -> Session:
    kernel, initrd = _boot_files(tmp_path)
    session_file = tmp_path / "sesion.json"
    monkeypatch.setattr(audit, "take_snapshot", lambda: _snapshot())
    monkeypatch.setattr(audit, "default_session_path", lambda: session_file)
    return Session(
        VmConfig(
            kernel=str(kernel),
            initrd=str(initrd),
            mem_mb=256,
            disk_mb=disk_mb,
        ),
        qemu=qemu,
        checks=(),
    )


def test_start_wait_finish_guarda_informe_y_sesion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = _session(tmp_path, monkeypatch)

    current.start()
    outcome = current.wait()

    assert outcome.returncode == 0
    assert current.poll() == 0
    assert current.pid > 0
    report = current.finish(wipe=False)

    assert "Informe de sesión VVM" in report
    assert "Cierre: normal" in report
    saved = audit.load_session_full(tmp_path / "sesion.json")
    assert saved.before == saved.after == _snapshot()


def test_io_sobre_un_pid_muerto_devuelve_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = _session(tmp_path, monkeypatch)
    current.start()
    current.wait()

    assert current.io() is None


def test_io_convierte_los_cuatro_contadores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = tmp_path / "qemu.pid"
    qemu = _fake_qemu(
        tmp_path,
        f"echo $$ > {marker}\nsleep 30\n",
    )
    current = _session(tmp_path, monkeypatch, qemu=str(qemu))
    current.start()
    monkeypatch.setattr(
        "vvm.session.vm.read_process_io",
        lambda pid, proc_dir="/proc": ProcessIO(
            rchar=1, wchar=2, read_bytes=3, write_bytes=4
        ),
    )

    stats = current.io()

    assert stats == IOStats(rchar=1, wchar=2, read_bytes=3, write_bytes=4)
    current.stop()
    current.finish(wipe=False)


def test_io_sin_fichero_devuelve_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = _session(tmp_path, monkeypatch)
    current._proc = SimpleNamespace(pid=999_999)
    monkeypatch.setattr(
        "vvm.session.vm.read_process_io",
        lambda pid, proc_dir="/proc": None,
    )

    assert current.io() is None


def test_stop_envia_sigterm_y_deja_el_hijo_muerto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = tmp_path / "qemu.pid"
    qemu = _fake_qemu(
        tmp_path,
        (
            f"echo $$ > {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    current = _session(tmp_path, monkeypatch, qemu=str(qemu))
    current.start()
    _wait_for(marker)
    try:
        code = current.stop()
    finally:
        _kill_marked_child(marker)

    assert code == 143
    assert current.poll() is not None
    assert "Cierre: SIGTERM" in current.finish(wipe=False)


def test_stop_escala_a_sigkill_y_lo_declara(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = tmp_path / "qemu.pid"
    qemu = _fake_qemu(
        tmp_path,
        (
            f"echo $$ > {marker}\n"
            "trap '' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    current = _session(tmp_path, monkeypatch, qemu=str(qemu))
    current.start()
    _wait_for(marker)
    try:
        code = current.stop(timeout=0.2)
        report = current.finish(wipe=False)
    finally:
        _kill_marked_child(marker)

    assert code == 143
    assert current.poll() is not None
    assert "QEMU no atendió el SIGTERM: SIGKILL" in report
    assert "Cierre: SIGTERM" in report


def test_finish_es_idempotente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = _session(tmp_path, monkeypatch)
    current.start()
    current.wait()

    first = current.finish(wipe=False)
    second = current.finish(wipe=False)

    assert first == second
    assert current.finished is True


def test_finish_sin_arrancar_falla(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    current = _session(tmp_path, monkeypatch)

    with pytest.raises(RuntimeError, match="no se inició"):
        current.finish(wipe=False)
