"""Tests de la línea base y del informe de sesión (SPEC §6, §7).

Los snapshots se fabrican a mano: ningún test toca el `/dev/shm` real.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from vvm import audit
from vvm.host_check import CheckResult

CLEAN_VERDICT = "SIN HALLAZGOS EN LO COMPROBADO"


def _snapshot(
    shmem_kb: int = 100_000,
    mem_available_kb: int = 4_000_000,
    shm_entries: tuple[str, ...] = (),
    qemu_pids: tuple[str, ...] = (),
    taken_at: str = "2026-10-24T10:00:00Z",
) -> audit.Snapshot:
    return audit.Snapshot(
        shmem_kb=shmem_kb,
        mem_available_kb=mem_available_kb,
        shm_entries=shm_entries,
        qemu_pids=qemu_pids,
        taken_at=taken_at,
    )


def _last_line(report: str) -> str:
    return report.rstrip("\n").splitlines()[-1]


def _fake_proc(tmp_path: Path, pid: str, cmdline: bytes | None) -> None:
    """Crea un `/proc/<pid>` de mentira con el cmdline indicado (None = ilegible)."""
    proc_pid = tmp_path / pid
    proc_pid.mkdir()
    if cmdline is not None:
        (proc_pid / "cmdline").write_bytes(cmdline)


def _check(
    check_id: str = "P3",
    vector: str = "swap",
    status: str = "FAIL",
    blocking: bool = True,
    evidence: str = "swap activa: /swap.img",
    remediation: str = "sudo swapoff -a",
) -> CheckResult:
    return CheckResult(
        id=check_id,
        vector=vector,
        status=status,
        blocking=blocking,
        evidence=evidence,
        remediation=remediation,
    )


def test_prechequeo_forzado_aparece_en_el_informe():
    p3 = _check()
    report = audit.build_report(_snapshot(), _snapshot(), checks=(p3,), forced=True)

    assert "0 · Pre-chequeo de la sesión" in report
    assert "P3 FALLA (bloqueante) swap: swap activa: /swap.img" in report
    assert "Forzada: sí (1 fallo(s) bloqueante(s) ignorados)." in report
    assert _last_line(report) == (
        "SIN HALLAZGOS EN LO COMPROBADO · sesión forzada con 1 fallo(s) bloqueante(s)"
    )


def test_prechequeo_apto_deja_veredicto_limpio():
    ok = _check("P1", "Aceleración", "OK", True, "/dev/kvm accesible", "instala qemu-kvm")

    report = audit.build_report(_snapshot(), _snapshot(), checks=(ok,), forced=False)

    assert "Apto: las 1 comprobaciones salieron OK." in report
    assert "FALLA" not in report
    assert "Forzada: no." in report
    assert _last_line(report) == CLEAN_VERDICT


def test_prechequeo_con_avisos_indica_el_resto_ok():
    ok = _check(
        "P1", "Aceleración KVM disponible", "OK", True, "/dev/kvm accesible", "instala qemu-kvm"
    )
    warn = _check(
        "P5", "Core dumps bajo control", "WARN", False, "|/usr/share/apport/apport", "revisa el handler"
    )

    report = audit.build_report(_snapshot(), _snapshot(), checks=(ok, warn), forced=False)

    assert "P5 AVISO (no bloqueante) Core dumps bajo control: |/usr/share/apport/apport" in report
    assert "Resto: 1 comprobaciones OK." in report
    assert "Apto: las" not in report
    assert _last_line(report) == CLEAN_VERDICT


def test_sesion_sin_prechequeo_carga_como_desconocida(tmp_path: Path):
    path = tmp_path / "antigua.json"
    path.write_text(
        json.dumps({"before": asdict(_snapshot()), "after": asdict(_snapshot())}),
        encoding="utf-8",
    )

    session = audit.load_session_full(path)

    assert session.checks is None
    assert session.forced is False
    report = audit.build_report(
        session.before, session.after, checks=session.checks, forced=session.forced
    )
    assert "Desconocido: la sesión no guardó el pre-chequeo." in report
    assert _last_line(report) == CLEAN_VERDICT


def test_save_load_conserva_prechequeo(tmp_path: Path):
    path = tmp_path / "sesion.json"
    p3 = _check()

    audit.save_session(path, _snapshot(), _snapshot(), checks=(p3,), forced=True)

    session = audit.load_session_full(path)
    assert session.checks == (p3,)
    assert session.forced is True


def test_nueva_entrada_en_shm_es_hallazgo():
    before = _snapshot(shm_entries=("resto-viejo",))
    after = _snapshot(shm_entries=("resto-viejo", "vvm-nueva"), taken_at="2026-10-24T10:05:00Z")

    report = audit.build_report(before, after)

    assert _last_line(report) == "HALLAZGOS: entrada nueva en /dev/shm: vvm-nueva"
    assert "Nuevas:  vvm-nueva" in report


def test_sin_cambios_veredicto_limpio_como_ultima_linea():
    before = _snapshot(shm_entries=("uno", "dos"))
    after = _snapshot(shm_entries=("dos", "uno"), taken_at="2026-10-24T10:05:00Z")

    report = audit.build_report(before, after)

    assert _last_line(report) == CLEAN_VERDICT
    assert "Nuevas:  ninguna" in report


def test_aviso_de_shmem_contador_global():
    report = audit.build_report(_snapshot(), _snapshot())

    assert "Shmem" in report
    assert "contador global" in report
    assert "cientos de MB" in report
    assert "ruido" in report


def test_log_serie_con_tamano_es_artefacto_no_hallazgo(tmp_path: Path):
    log = tmp_path / "vvm-serial.log"
    log.write_text("hola\n", encoding="utf-8")

    report = audit.build_report(_snapshot(), _snapshot(), log_path=str(log))

    assert f"Log serie: {log} (5 bytes)" in report
    assert "es un rastro en disco: bórralo o guárdalo aparte" in report
    assert _last_line(report) == CLEAN_VERDICT


def test_log_serie_inexistente_se_declara_ninguno(tmp_path: Path):
    missing = tmp_path / "no-existe.log"

    report = audit.build_report(_snapshot(), _snapshot())

    assert "Log serie: ninguno" in report
    assert "rastro en disco" not in report

    report_missing = audit.build_report(_snapshot(), _snapshot(), log_path=str(missing))
    assert f"Log serie: ninguno (no existe {missing})" in report_missing


def test_qemu_vivo_al_cierre_es_hallazgo():
    before = _snapshot()
    after = _snapshot(qemu_pids=("4242",), taken_at="2026-10-24T10:05:00Z")

    report = audit.build_report(before, after)

    assert _last_line(report) == "HALLAZGOS: proceso QEMU vivo al cierre: pid 4242"


def test_live_qemu_pids_detecta_qemu_con_marcador(tmp_path: Path):
    _fake_proc(
        tmp_path,
        "101",
        b"qemu-system-x86_64\0-object\0memory-backend-memfd,id=mem,size=512M,share=off\0",
    )

    assert audit.live_qemu_pids(tmp_path) == ("101",)


def test_live_qemu_pids_ignora_python_con_marcador(tmp_path: Path):
    # El propio CLI o un `python -c` pueden llevar el marcador en argv.
    _fake_proc(
        tmp_path,
        "300",
        b"/usr/bin/python3\0-c\0import os; os.execv('qemu', ['memory-backend-memfd,id=mem'])\0",
    )

    assert audit.live_qemu_pids(tmp_path) == ()


def test_live_qemu_pids_ignora_qemu_ajeno_y_cmdline_ilegible(tmp_path: Path):
    _fake_proc(tmp_path, "202", b"qemu-system-x86_64\0-m\01024M\0")
    _fake_proc(tmp_path, "303", None)

    assert audit.live_qemu_pids(tmp_path) == ()


def test_sin_qemu_de_la_sesion_el_veredicto_es_limpio(tmp_path: Path):
    _fake_proc(tmp_path, "202", b"qemu-system-x86_64\0-m\01024M\0")

    vivos = audit.live_qemu_pids(tmp_path)
    report = audit.build_report(
        _snapshot(),
        _snapshot(qemu_pids=vivos, taken_at="2026-10-24T10:05:00Z"),
    )

    assert vivos == ()
    assert _last_line(report) == CLEAN_VERDICT


def test_deltas_de_memoria_con_signo():
    before = _snapshot(shmem_kb=100_000, mem_available_kb=4_000_000)
    after = _snapshot(shmem_kb=100_512, mem_available_kb=3_999_000)

    report = audit.build_report(before, after)

    assert "Shmem:        antes 100000 kB, después 100512 kB, delta +512 kB" in report
    assert "MemAvailable: antes 4000000 kB, después 3999000 kB, delta -1000 kB" in report


def test_informe_es_determinista():
    before = _snapshot(shm_entries=("b", "a"))
    after = _snapshot(shm_entries=("a", "b", "c"), qemu_pids=("99", "100"))

    first = audit.build_report(before, after)
    second = audit.build_report(before, after)

    assert first == second


def test_save_load_roundtrip(tmp_path: Path):
    before = _snapshot(shm_entries=("a",), qemu_pids=("10",))
    after = _snapshot(
        shmem_kb=101_000,
        shm_entries=("a", "b"),
        qemu_pids=("11", "12"),
        taken_at="2026-10-24T10:05:00Z",
    )
    path = tmp_path / "estado" / "vvm" / "last-session.json"

    audit.save_session(path, before, after)
    loaded_before, loaded_after = audit.load_session(path)

    assert path.is_file()
    assert loaded_before == before
    assert loaded_after == after
    assert isinstance(loaded_before.shm_entries, tuple)
    assert isinstance(loaded_after.qemu_pids, tuple)


def test_load_session_rechaza_estructura_invalida(tmp_path: Path):
    path = tmp_path / "roto.json"
    path.write_text(json.dumps({"before": {"shmem_kb": 1}}), encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        audit.load_session(path)

    assert "Faltan las claves after" in str(excinfo.value)


def test_load_session_rechaza_json_invalido(tmp_path: Path):
    path = tmp_path / "roto.json"
    path.write_text("{no es json", encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        audit.load_session(path)

    assert "no es un JSON válido" in str(excinfo.value)


def test_default_session_path_con_xdg_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "estado"))

    assert audit.default_session_path() == tmp_path / "estado" / "vvm" / "last-session.json"


def test_default_session_path_sin_xdg_usa_home(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", "/home/ejemplo")

    assert (
        audit.default_session_path()
        == Path("/home/ejemplo/.local/state/vvm/last-session.json")
    )


def test_take_snapshot_compone_lectores(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        audit, "read_meminfo", lambda path=audit.MEMINFO_PATH: {"Shmem": 1234, "MemAvailable": 5678}
    )
    monkeypatch.setattr(audit, "list_shm_entries", lambda shm_dir=audit.SHM_PATH: ("x",))
    monkeypatch.setattr(audit, "live_qemu_pids", lambda proc_dir=audit.PROC_PATH: ("777",))

    snapshot = audit.take_snapshot()

    assert snapshot.shmem_kb == 1234
    assert snapshot.mem_available_kb == 5678
    assert snapshot.shm_entries == ("x",)
    assert snapshot.qemu_pids == ("777",)
    assert snapshot.taken_at.endswith("Z")


def test_list_shm_entries_solo_lee(tmp_path: Path):
    (tmp_path / "b").write_text("", encoding="utf-8")
    (tmp_path / "a").write_text("", encoding="utf-8")

    assert audit.list_shm_entries(tmp_path) == ("a", "b")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a", "b"]
