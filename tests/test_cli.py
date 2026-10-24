"""Tests del CLI: `check`, `run` y `audit` sin KVM ni QEMU real."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vvm import audit, cli, host_check, images

SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
SWAP_ACTIVE = SWAPS_HEADER + "/swap.img file 8388604 0 -2\n"


@pytest.fixture
def host_ok(monkeypatch: pytest.MonkeyPatch):
    """Anfitrión apto: ningún test del CLI depende de /proc, /sys ni /dev/shm reales."""
    monkeypatch.setattr(host_check, "kvm_accessible", lambda: True)
    monkeypatch.setattr(host_check, "read_meminfo", lambda: {"MemAvailable": 8 * 1024 * 1024})
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAPS_HEADER)
    monkeypatch.setattr(host_check, "read_power_state", lambda: "freeze mem\n")
    monkeypatch.setattr(host_check, "read_core_pattern", lambda: "core\n")
    monkeypatch.setattr(host_check, "read_ksm_run", lambda: "0\n")
    monkeypatch.setattr(host_check, "read_shmem_enabled", lambda: "[never] always\n")
    monkeypatch.setattr(host_check, "list_shm_entries", lambda: [])


def _boot_files(tmp_path: Path) -> tuple[Path, Path]:
    kernel = tmp_path / "vmlinuz"
    initrd = tmp_path / "initramfs"
    kernel.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")
    return kernel, initrd


def _run_args(kernel: Path, initrd: Path, *extra: str) -> list[str]:
    return ["run", "--kernel", str(kernel), "--initrd", str(initrd), *extra]


def _snapshot(**overrides) -> audit.Snapshot:
    data = {
        "shmem_kb": 1000,
        "mem_available_kb": 4_000_000,
        "shm_entries": (),
        "qemu_pids": (),
        "taken_at": "2026-10-24T10:00:00Z",
    }
    data.update(overrides)
    return audit.Snapshot(**data)


def _capture_disk_fd(monkeypatch: pytest.MonkeyPatch, fds: list[int]) -> None:
    real = images.create_disk_fd

    def capture(size_mb: int) -> int:
        fd = real(size_mb)
        fds.append(fd)
        return fd

    monkeypatch.setattr(cli.images, "create_disk_fd", capture)


def _stub_session(monkeypatch: pytest.MonkeyPatch, session: Path) -> None:
    monkeypatch.setattr(cli.audit, "default_session_path", lambda: session)
    monkeypatch.setattr(cli.audit, "take_snapshot", lambda: _snapshot())


# --- check ---------------------------------------------------------------


def test_check_apto_sale_0(host_ok, capsys):
    assert cli.main(["check"]) == 0

    out = capsys.readouterr().out
    assert "P1 OK" in out
    assert "P8 OK" in out
    assert "apto para arrancar" in out


def test_check_con_swap_sale_1(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)

    assert cli.main(["check"]) == 1

    out = capsys.readouterr().out
    assert "P3 FALLA" in out
    assert "/swap.img" in out
    assert "fallo(s) bloqueante(s)" in out


def test_check_sin_kvm_sale_1(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(host_check, "kvm_accessible", lambda: False)

    assert cli.main(["check"]) == 1

    assert "P1 FALLA" in capsys.readouterr().out


def test_check_memoria_baja_sale_1(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(host_check, "read_meminfo", lambda: {"MemAvailable": 600 * 1024})

    assert cli.main(["check"]) == 1

    out = capsys.readouterr().out
    assert "P2 FALLA" in out
    assert "necesario=1280 MB" in out


def test_check_json_estructura(host_ok, capsys):
    assert cli.main(["check", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert len(data) == 10
    assert [item["id"] for item in data] == [f"P{n}" for n in range(1, 11)]
    assert data[0]["status"] == "OK"
    assert {"id", "vector", "status", "blocking", "evidence", "remediation"} <= set(data[0])


def test_main_sin_subcomando_sale_2(capsys):
    assert cli.main([]) == 2

    assert "usage:" in capsys.readouterr().out


# --- run -----------------------------------------------------------------


def test_run_dry_run_imprime_linea_y_no_lanza(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    fds: list[int] = []
    _capture_disk_fd(monkeypatch, fds)

    def boom(*args, **kwargs):
        raise AssertionError("no debe ejecutarse en --dry-run")

    monkeypatch.setattr(cli.host_check, "check_host", boom)
    monkeypatch.setattr(cli.vm, "run_vm", boom)

    assert cli.main(_run_args(kernel, initrd, "--dry-run")) == 0

    out = capsys.readouterr().out
    assert "memory-backend-memfd,id=mem,size=512M,share=off" in out
    assert "file=/dev/fd/" in out
    assert "-nic none" in out
    assert fds, "el disco debe crearse en memoria"
    with pytest.raises(OSError):
        os.fstat(fds[0])


def test_run_con_bloqueo_aborta_y_cierra_fd(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)
    fds: list[int] = []
    _capture_disk_fd(monkeypatch, fds)

    def boom(*args, **kwargs):
        raise AssertionError("no debe lanzarse QEMU si hay bloqueos")

    monkeypatch.setattr(cli.vm, "run_vm", boom)

    assert cli.main(_run_args(kernel, initrd)) == 1

    captured = capsys.readouterr()
    assert "P3 FALLA" in captured.out
    assert "--force" in captured.err
    with pytest.raises(OSError):
        os.fstat(fds[0])


def test_run_con_qemu_true_emite_informe_y_guarda_sesion(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    assert cli.main(_run_args(kernel, initrd, "--qemu", "/bin/true")) == 0

    out = capsys.readouterr().out
    assert "Informe de sesión VVM" in out
    assert "SIN HALLAZGOS EN LO COMPROBADO" in out
    before, after = audit.load_session(session)
    assert before == after == _snapshot()


def test_run_escribe_informe_en_fichero(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    report = tmp_path / "informe.txt"
    _stub_session(monkeypatch, session)

    args = _run_args(kernel, initrd, "--qemu", "/bin/true", "--report", str(report))
    assert cli.main(args) == 0

    assert report.read_text(encoding="utf-8").startswith("Informe de sesión VVM")
    assert f"Informe escrito en {report}" in capsys.readouterr().out


def test_run_emite_informe_aunque_qemu_falle(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    assert cli.main(_run_args(kernel, initrd, "--qemu", "/bin/false")) == 1

    out = capsys.readouterr().out
    assert "SIN HALLAZGOS EN LO COMPROBADO" in out


def test_run_emite_informe_si_se_interrumpe(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    def interrupted(cmd, pass_fds=()):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.vm, "run_vm", interrupted)

    assert cli.main(_run_args(kernel, initrd)) == 130

    captured = capsys.readouterr()
    assert "SIN HALLAZGOS EN LO COMPROBADO" in captured.out
    assert "interrumpida" in captured.err


def test_run_force_ignora_bloqueos(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)
    monkeypatch.setattr(cli.vm, "run_vm", lambda cmd, pass_fds=(): 0)

    assert cli.main(_run_args(kernel, initrd, "--force")) == 0

    out = capsys.readouterr().out
    assert "P3 FALLA" in out
    assert "SIN HALLAZGOS EN LO COMPROBADO" in out


def test_run_headless_sin_log_sale_1(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)

    assert cli.main(_run_args(kernel, initrd, "--headless")) == 1

    assert "log serie" in capsys.readouterr().err


# --- audit ---------------------------------------------------------------


def test_audit_sin_sesion_sale_1(tmp_path, capsys):
    missing = tmp_path / "no-existe.json"

    assert cli.main(["audit", "--session", str(missing)]) == 1

    assert "No hay sesión que auditar" in capsys.readouterr().err


def test_audit_imprime_sesion_guardada(tmp_path, capsys):
    session = tmp_path / "sesion.json"
    audit.save_session(
        session,
        _snapshot(shm_entries=("bus",)),
        _snapshot(shm_entries=("bus", "vvm-nueva"), taken_at="2026-10-24T10:05:00Z"),
    )

    assert cli.main(["audit", "--session", str(session)]) == 0

    assert "HALLAZGOS: entrada nueva en /dev/shm: vvm-nueva" in capsys.readouterr().out


def test_audit_usa_la_sesion_por_defecto(monkeypatch, tmp_path, capsys):
    session = tmp_path / "ultima.json"
    audit.save_session(session, _snapshot(), _snapshot(taken_at="2026-10-24T10:05:00Z"))
    monkeypatch.setattr(cli.audit, "default_session_path", lambda: session)

    assert cli.main(["audit"]) == 0

    assert "SIN HALLAZGOS EN LO COMPROBADO" in capsys.readouterr().out
