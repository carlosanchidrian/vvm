"""Informe con sesiones concurrentes, rutas por id y P2 con memoria ocupada."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vvm import audit, cli, host_check
from vvm.host_check import CheckResult
from vvm.vm import VmConfig, build_cmd

SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
SOCKET = "/run/user/1000/vvm/sessions/kali-3f2a/serie.sock"


def _snapshot(**overrides) -> audit.Snapshot:
    values = {
        "shmem_kb": 1000,
        "mem_available_kb": 4_000_000,
        "shm_entries": (),
        "qemu_pids": (),
        "taken_at": "2026-11-26T18:40:12Z",
        "memfds": (),
    }
    values.update(overrides)
    return audit.Snapshot(**values)


def _check() -> CheckResult:
    return CheckResult(
        id="P2",
        vector="Memoria RAM suficiente",
        status="OK",
        blocking=True,
        evidence="MemAvailable=ok",
        remediation="libera memoria",
    )


def _fake_proc(root: Path, pid: str, cmdline: bytes | None) -> None:
    process = root / pid
    process.mkdir(parents=True)
    if cmdline is not None:
        (process / "cmdline").write_bytes(cmdline)


@pytest.fixture
def host_readers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(host_check, "kvm_accessible", lambda: True)
    monkeypatch.setattr(
        host_check, "read_meminfo", lambda: {"MemAvailable": 2 * 1024 * 1024}
    )
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAPS_HEADER)
    monkeypatch.setattr(host_check, "read_power_state", lambda: "freeze mem\n")
    monkeypatch.setattr(host_check, "read_core_pattern", lambda: "core\n")
    monkeypatch.setattr(host_check, "read_ksm_run", lambda: "0\n")
    monkeypatch.setattr(host_check, "read_shmem_enabled", lambda: "[never] always\n")
    monkeypatch.setattr(host_check, "list_shm_entries", lambda: [])


def test_other_sessions_cero_no_cambia_el_informe():
    plain = audit.build_report(_snapshot(), _snapshot())
    with_zero = audit.build_report(
        _snapshot(),
        _snapshot(),
        extras=audit.SessionExtras(other_sessions=0),
    )

    assert with_zero == plain


def test_other_sessions_uno_anade_la_linea_tras_memavailable():
    report = audit.build_report(
        _snapshot(),
        _snapshot(),
        extras=audit.SessionExtras(other_sessions=1),
    )
    lines = report.splitlines()

    expected = "   Otras sesiones vivas: 1 (su actividad entra en el delta de Shmem)"
    assert expected in lines
    index = lines.index(expected)
    assert lines[index - 1].startswith("   MemAvailable:")
    assert "Aviso: Shmem es un contador global" in lines[index + 1]


def test_other_sessions_se_guarda_y_se_relee(tmp_path: Path):
    path = tmp_path / "sesion.json"
    extras = audit.SessionExtras(
        other_sessions=2, log_path="/tmp/serie.log"
    )

    audit.save_session(path, _snapshot(), _snapshot(), extras=extras)

    assert audit.load_session_full(path).extras == extras
    assert json.loads(path.read_text(encoding="utf-8"))["extras"][
        "other_sessions"
    ] == 2
    assert json.loads(path.read_text(encoding="utf-8"))["extras"][
        "log_path"
    ] == "/tmp/serie.log"


def test_json_antiguo_sin_other_sessions_carga_como_cero(tmp_path: Path):
    path = tmp_path / "sesion.json"
    audit.save_session(path, _snapshot(), _snapshot())
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["extras"]["other_sessions"]
    path.write_text(json.dumps(document), encoding="utf-8")

    assert audit.load_session_full(path).extras.other_sessions == 0
    report = audit.build_report(
        _snapshot(),
        _snapshot(),
        extras=audit.load_session_full(path).extras,
    )
    assert "Otras sesiones vivas" not in report


def test_audit_por_id_reproduce_el_informe_con_otras_sesiones(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    session_id = "kali-3f2a"
    path = audit.session_path_for(session_id)
    extras = audit.SessionExtras(other_sessions=1, closure="normal")
    audit.save_session(
        path,
        _snapshot(),
        _snapshot(taken_at="2026-11-26T18:45:12Z"),
        checks=(_check(),),
        extras=extras,
    )
    expected = audit.build_report(
        _snapshot(),
        _snapshot(taken_at="2026-11-26T18:45:12Z"),
        checks=(_check(),),
        extras=extras,
    )

    assert cli.main(["audit", session_id]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected
    assert "Otras sesiones vivas: 1" in captured.out


def test_audit_por_id_reproduce_el_log_guardado(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    session_id = "kali-3f2a"
    log = tmp_path / "serie.log"
    log.write_text("hola\n", encoding="utf-8")
    extras = audit.SessionExtras(
        closure="normal", log_path=str(log), log_size=5
    )
    audit.save_session(
        audit.session_path_for(session_id),
        _snapshot(),
        _snapshot(taken_at="2026-11-26T18:45:12Z"),
        extras=extras,
    )
    log.unlink()
    expected = audit.build_report(
        _snapshot(),
        _snapshot(taken_at="2026-11-26T18:45:12Z"),
        str(log),
        extras=extras,
    )

    assert cli.main(["audit", session_id]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected
    assert f"Log serie: {log} (5 bytes)" in captured.out


def test_audit_por_id_inexistente_sale_2_y_lista_las_guardadas(
    capsys: pytest.CaptureFixture[str],
):
    audit.save_session(
        audit.session_path_for("kali-3f2a"), _snapshot(), _snapshot()
    )

    assert cli.main(["audit", "no-existe"]) == 2
    captured = capsys.readouterr()
    assert "no existe la sesión 'no-existe'" in captured.err
    assert "Sesiones guardadas: kali-3f2a" in captured.err


def test_audit_por_id_y_session_a_la_vez_sale_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert (
        cli.main(
            ["audit", "kali-3f2a", "--session", str(tmp_path / "x.json")]
        )
        == 2
    )
    assert "no los dos a la vez" in capsys.readouterr().err


def test_session_path_for_lista_y_rechaza_rutas(tmp_path: Path):
    audit.save_session(
        audit.session_path_for("kali-3f2a"), _snapshot(), _snapshot()
    )
    audit.save_session(
        audit.session_path_for("alpine-91b0"), _snapshot(), _snapshot()
    )

    assert audit.session_path_for("kali-3f2a") == (
        tmp_path / "state" / "vvm" / "sessions" / "kali-3f2a.json"
    )
    assert audit.list_saved_sessions() == ("alpine-91b0", "kali-3f2a")
    with pytest.raises(ValueError, match="id de sesión no válido"):
        audit.session_path_for("../fuera")


def test_is_vvm_qemu_publico_sin_duplicar_el_criterio(tmp_path: Path):
    _fake_proc(
        tmp_path,
        "101",
        b"qemu-system-x86_64\0-object\0memory-backend-memfd,id=mem,size=512M,share=off\0",
    )
    _fake_proc(
        tmp_path,
        "202",
        b"/usr/bin/python3\0-c\0memory-backend-memfd,id=mem\0",
    )
    _fake_proc(tmp_path, "303", None)

    assert audit.is_vvm_qemu(101, tmp_path) is True
    assert audit.is_vvm_qemu(202, tmp_path) is False
    assert audit.is_vvm_qemu(303, tmp_path) is False


def test_p2_falla_si_otra_vm_ya_consumio_la_memoria(host_readers):
    config = VmConfig(
        kernel="/boot/vmlinuz",
        initrd="/boot/initramfs",
        mem_mb=4096,
        disk_mb=256,
        serial_mode="socket",
        serial_sock=SOCKET,
    )
    cmd = build_cmd(config, disk_fd=3)

    results = host_check.check_host(4096, 256, cmd)
    p2 = next(result for result in results if result.id == "P2")

    assert p2.status == "FAIL"
    assert "MemAvailable=2097152 kB" in p2.evidence
    assert p2.blocking is True
