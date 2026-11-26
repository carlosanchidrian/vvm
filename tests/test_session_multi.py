"""Registro por id, señales del grupo y arranque interrumpido."""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from vvm import audit, supervisor
from vvm.session import Session
from vvm.vm import VmConfig


def _snapshot(taken_at: str = "2026-11-26T18:40:12Z", **overrides) -> audit.Snapshot:
    values = {
        "shmem_kb": 1000,
        "mem_available_kb": 4_000_000,
        "shm_entries": (),
        "qemu_pids": (),
        "taken_at": taken_at,
        "memfds": (),
    }
    values.update(overrides)
    return audit.Snapshot(**values)


def _boot(tmp_path: Path) -> tuple[Path, Path]:
    kernel = tmp_path / "vmlinuz"
    initrd = tmp_path / "initramfs"
    kernel.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")
    return kernel, initrd


def _looping_qemu(tmp_path: Path, marker: Path) -> Path:
    script = tmp_path / "fake-qemu"
    script.write_text(
        "#!/bin/sh\n"
        f"echo $$ >> {marker}\n"
        "trap 'exit 0' TERM\n"
        "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"no apareció {path}")
        time.sleep(0.02)


def _kill_marked(marker: Path) -> None:
    try:
        pids = [int(line) for line in marker.read_text().split()]
    except (OSError, ValueError):
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    qemu: str = "/bin/true",
    label: str = "uno",
    image: str | None = None,
    install_signals: bool = False,
    should_stop=None,
) -> Session:
    kernel, initrd = _boot(tmp_path)
    monkeypatch.setattr(audit, "take_snapshot", lambda: _snapshot())
    config = VmConfig(
        kernel=None if image else str(kernel),
        initrd=None if image else str(initrd),
        image=image,
        mem_mb=256,
        disk_mb=0 if image else 8,
    )
    return Session(
        config,
        qemu=qemu,
        checks=(),
        label=label,
        install_signals=install_signals,
        should_stop=should_stop,
    )


def test_install_signals_false_no_toca_signal_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    def fail(_signum, _handler):
        raise AssertionError("no se debe instalar un manejador")

    monkeypatch.setattr("vvm.session.vm.SignalGuard.install", fail)
    monkeypatch.setattr("vvm.session.vm.SignalGuard.restore", fail)
    current = _session(tmp_path, monkeypatch, install_signals=False)

    current.start()
    current.wait()
    report = current.finish(wipe=False)

    assert "Cierre: normal" in report
    assert current.finished is True


def test_stop_con_install_signals_false_devuelve_143(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    current = _session(
        tmp_path, monkeypatch, qemu=str(qemu), install_signals=False
    )
    current.start()
    _wait_for(marker)
    try:
        code = current.stop()
        report = current.finish(wipe=False)
    finally:
        _kill_marked(marker)

    assert code == 143
    assert "Cierre: SIGTERM" in report


def test_should_stop_aborta_la_copia_del_iso_sin_lanzar_qemu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * 1024)
    flags = {"stop": False}
    calls: list[str] = []

    def fake_copy(path: str, *, should_stop=None) -> int:
        assert path == str(iso)
        assert should_stop is not None
        flags["stop"] = True
        calls.append("copia")
        if should_stop():
            raise InterruptedError("copia abortada")
        raise AssertionError("el grupo debía marcar la parada")

    monkeypatch.setattr(
        "vvm.session.images.create_image_fd", fake_copy
    )
    current = _session(
        tmp_path,
        monkeypatch,
        image=str(iso),
        should_stop=lambda: flags["stop"],
    )

    with pytest.raises(InterruptedError, match="copia abortada"):
        current.start()

    assert calls == ["copia"]
    assert current.started is False
    assert current.session_id is not None
    live = supervisor.runtime_root() / current.session_id / "live.json"
    assert not live.exists()
    current.close()


def test_finish_concurrente_emite_un_solo_informe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    current = _session(tmp_path, monkeypatch)
    calls: list[Path] = []

    def saver(path, *args, **kwargs) -> None:
        calls.append(Path(path))

    current._session_saver = saver
    current.start()
    current.wait()
    results: list[str] = []
    threads = [
        threading.Thread(target=lambda: results.append(current.finish(wipe=False)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 2
    assert results[0] == results[1]
    assert len(calls) == len(set(calls))
    assert current.finished is True


def test_finish_ignora_procesos_y_memfds_de_otras_sesiones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    kernel, initrd = _boot(tmp_path)
    before = _snapshot()
    after = _snapshot(qemu_pids=("111",))
    before = replace(
        before, qemu_pids=("111",), memfds=("memfd:otro",)
    )
    after = replace(
        after, memfds=("memfd:otro", "memfd:propio")
    )
    snapshots = iter((before, after))
    monkeypatch.setattr(
        audit,
        "memfd_names_of",
        lambda pid, proc_dir=audit.PROC_PATH: (
            ("memfd:otro",) if pid == 111 else ()
        ),
    )
    current = Session(
        VmConfig(
            kernel=str(kernel),
            initrd=str(initrd),
            mem_mb=256,
            disk_mb=0,
        ),
        qemu="/bin/true",
        checks=(),
        label="uno",
        install_signals=False,
        snapshotter=lambda: next(snapshots),
    )
    current.start()
    current.wait()
    report = current.finish(wipe=False)

    assert "111" not in report
    assert "memfd:otro" not in report
    assert "memfd:propio" in report
    assert current.before.qemu_pids == ()
    assert current.after.qemu_pids == ()
    assert "memfd:otro" not in (current.before.memfds or ())
    assert "memfd:otro" not in (current.after.memfds or ())


def test_dos_sesiones_se_registran_y_guardan_por_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(audit, "is_vvm_qemu", lambda _pid: True)
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    first = _session(
        tmp_path, monkeypatch, qemu=str(qemu), label="uno"
    )
    second = _session(
        tmp_path, monkeypatch, qemu=str(qemu), label="dos"
    )
    try:
        first.start()
        second.start()
        _wait_for(marker)
        assert first.session_id != second.session_id
        assert first.other_sessions == 0
        assert second.other_sessions == 1

        first_live = supervisor.runtime_root() / first.session_id / "live.json"
        second_live = supervisor.runtime_root() / second.session_id / "live.json"
        assert first_live.is_file()
        assert second_live.is_file()
        assert oct((first_live.parent.stat().st_mode & 0o777)) == "0o700"
        assert oct((second_live.parent.stat().st_mode & 0o777)) == "0o700"

        assert first.stop() == 143
        report = first.finish(wipe=True)
        assert "Cierre: SIGTERM" in report
        assert "Wipe: 8 MiB" in report
        assert not first_live.exists()
        assert second_live.is_file()

        second.stop()
        second.finish(wipe=False)
        assert not second_live.exists()

        for current in (first, second):
            path = audit.session_path_for(current.session_id)
            assert path.is_file()
            saved = audit.load_session_full(path)
            assert saved.before == saved.after == _snapshot()
        assert audit.default_session_path().is_file()
    finally:
        _kill_marked(marker)
