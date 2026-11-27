"""`vvm ps` y `vvm audit <id>` sin KVM ni QEMU."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from vvm import audit, cli, supervisor
from vvm.supervisor import LiveSession


def _live(session_id: str, pid: int, **overrides) -> LiveSession:
    values = {
        "id": session_id,
        "label": "kali",
        "pid": pid,
        "mem_mb": 4096,
        "disk_mb": 256,
        "image": True,
        "started_at": "2026-11-26T18:40:12Z",
        "serial": "socket",
        "serial_path": f"/run/user/1000/vvm/sessions/{session_id}/serie.sock",
    }
    values.update(overrides)
    return LiveSession(**values)


def test_ps_pinta_la_tabla_y_ordena_por_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(audit, "is_vvm_qemu", lambda _pid: True)
    supervisor.write_live(_live("kali-3f2a", os.getpid()))
    supervisor.write_live(
        _live("alpine-91b0", os.getpid(), label="alpine-min", disk_mb=0)
    )

    assert cli.main(["ps"]) == 0
    captured = capsys.readouterr()

    lines = captured.out.strip().splitlines()
    assert lines[0].split() == [
        "ID", "PERFIL", "PID", "RAM", "DISCO", "ESTADO", "CONSOLA"
    ]
    assert lines[1].split()[:2] == ["alpine-91b0", "alpine-min"]
    assert lines[2].split()[:2] == ["kali-3f2a", "kali"]
    assert "viva" in lines[1]
    assert "socket" in lines[1]
    assert captured.err == ""


def test_ps_json_es_estable_y_sin_uptime(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(audit, "is_vvm_qemu", lambda _pid: True)
    supervisor.write_live(_live("kali-3f2a", os.getpid()))
    supervisor.write_live(_live("alpine-91b0", os.getpid(), label="alpine-min"))

    assert cli.main(["ps", "--json"]) == 0
    entries = json.loads(capsys.readouterr().out)

    assert [entry["id"] for entry in entries] == [
        "alpine-91b0",
        "kali-3f2a",
    ]
    assert set(entries[0]) == {
        "id",
        "label",
        "pid",
        "mem_mb",
        "disk_mb",
        "image",
        "started_at",
        "serial",
        "serial_path",
    }
    assert "uptime" not in entries[0]
    assert json.dumps(entries, sort_keys=True) == json.dumps(
        sorted(entries, key=lambda entry: entry["id"]), sort_keys=True
    )


def test_ps_vacio_mensaje_y_salida_cero(capsys: pytest.CaptureFixture[str]):
    assert cli.main(["ps"]) == 0
    assert capsys.readouterr().out.strip() == "no hay sesiones vivas"

    assert cli.main(["ps", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_ps_poda_un_pid_muerto(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.setattr(audit, "is_vvm_qemu", lambda _pid: True)
    child = subprocess.Popen(["/bin/true"])
    child.wait()
    path = supervisor.write_live(_live("kali-3f2a", child.pid))

    assert cli.main(["ps"]) == 0
    assert capsys.readouterr().out.strip() == "no hay sesiones vivas"
    assert not path.exists()


def test_audit_sin_id_sigue_leyendo_last_session(
    capsys: pytest.CaptureFixture[str],
):
    audit.save_session(
        audit.default_session_path(),
        audit.Snapshot(
            shmem_kb=1,
            mem_available_kb=2,
            shm_entries=(),
            qemu_pids=(),
            taken_at="2026-11-26T18:40:12Z",
            memfds=(),
        ),
        audit.Snapshot(
            shmem_kb=1,
            mem_available_kb=2,
            shm_entries=(),
            qemu_pids=(),
            taken_at="2026-11-26T18:45:12Z",
            memfds=(),
        ),
    )

    assert cli.main(["audit"]) == 0
    assert "Informe de sesión VVM" in capsys.readouterr().out
