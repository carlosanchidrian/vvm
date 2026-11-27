"""Tests del registro de sesiones vivas y del guardia de grupo."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from vvm import supervisor
from vvm.supervisor import LiveSession, SessionGroup


@pytest.fixture
def short_root():
    """Runtime corto: tmp_path puede acercarse al límite de AF_UNIX."""
    path = Path(tempfile.mkdtemp(prefix="vvm-", dir="/tmp"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


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


def test_new_session_id_es_kebab_y_regenera_colisiones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "runtime"
    (root / "kali-linux-aaaa").mkdir(parents=True)
    tokens = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(supervisor.secrets, "token_hex", lambda _: next(tokens))

    session_id = supervisor.new_session_id(" Kali Linux ", root=root)

    assert session_id == "kali-linux-bbbb"
    assert supervisor.kebab_case("Kali Linux") == "kali-linux"
    assert supervisor.kebab_case("???") == "vm"


def test_prepare_session_dir_es_0700_y_borra_el_socket_anterior(short_root: Path):
    root = short_root
    directory = supervisor.prepare_session_dir("kali-3f2a", root=root)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700

    sock = directory / "serie.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock))
        assert sock.exists()
    finally:
        server.close()

    supervisor.prepare_session_dir("kali-3f2a", root=root)
    assert not sock.exists()

    with pytest.raises(FileExistsError):
        supervisor.prepare_session_dir(
            "kali-3f2a", root=root, exist_ok=False
        )


def test_reserve_session_no_comparte_directorio(short_root: Path):
    root = short_root
    first_id, first_dir = supervisor.reserve_session(
        "kali", root=root
    )
    second_id, second_dir = supervisor.reserve_session(
        "kali", root=root
    )

    assert first_id != second_id
    assert first_dir != second_dir
    assert first_dir.is_dir() and second_dir.is_dir()


def test_runtime_root_cae_a_una_raiz_corta_si_xdg_no_cabe(
    tmp_path: Path, short_root: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv(
        "XDG_RUNTIME_DIR", str(tmp_path / ("x" * 200))
    )
    monkeypatch.setattr(
        supervisor, "_fallback_runtime_roots", lambda: (short_root,)
    )

    root = supervisor.runtime_root()
    session_id, directory = supervisor.reserve_session("kali")

    assert root == short_root
    assert directory == short_root / session_id
    assert len(
        os.fsencode(str(directory / "serie.sock"))
    ) <= supervisor._UNIX_SOCKET_MAX


def test_prepare_session_dir_rechaza_una_ruta_de_socket_demasiado_larga(
    short_root: Path,
):
    root = short_root / ("y" * 100)

    with pytest.raises(ValueError, match="demasiado larga para AF_UNIX"):
        supervisor.prepare_session_dir("kali-3f2a", root=root)


def test_write_y_remove_live(tmp_path: Path):
    root = tmp_path / "runtime"
    entry = _live("kali-3f2a", os.getpid())

    path = supervisor.write_live(entry, root=root)
    assert path.is_file()
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document == entry.to_dict()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    supervisor.remove_live("kali-3f2a", root=root)
    assert not path.exists()
    assert not path.parent.exists()


def test_list_live_filtra_pid_muerto_y_poda(tmp_path: Path):
    root = tmp_path / "runtime"
    child = subprocess.Popen(["/bin/true"])
    child.wait()
    entry = _live("kali-3f2a", child.pid)
    path = supervisor.write_live(entry, root=root)

    assert supervisor.list_live(root=root, validator=lambda _: True) == ()
    assert not path.exists()


def test_list_live_filtra_pid_que_no_es_qemu_vvm(tmp_path: Path):
    root = tmp_path / "runtime"
    entry = _live("kali-3f2a", os.getpid())
    path = supervisor.write_live(entry, root=root)

    assert supervisor.list_live(root=root, validator=lambda _: False) == ()
    assert not path.exists()


def test_list_live_avisa_de_directorio_ilegible_y_no_poda(tmp_path: Path):
    if os.geteuid() == 0:
        pytest.skip("root ignora los permisos del directorio")
    root = tmp_path / "runtime"
    directory = supervisor.prepare_session_dir("kali-3f2a", root=root)
    path = supervisor.write_live(_live("kali-3f2a", os.getpid()), root=root)
    warnings: list[str] = []
    os.chmod(directory, 0)
    try:
        entries = supervisor.list_live(
            root=root,
            validator=lambda _: True,
            on_warning=warnings.append,
        )
    finally:
        os.chmod(directory, 0o700)

    assert entries == ()
    assert warnings
    assert path.exists()


def test_list_live_poda_un_pid_fuera_de_rango(tmp_path: Path):
    root = tmp_path / "runtime"
    path = supervisor.write_live(
        _live("kali-3f2a", 2**31), root=root
    )

    assert supervisor.list_live(root=root, validator=lambda _: True) == ()
    assert not path.exists()


def test_list_live_ordena_y_conserva_las_vivas(tmp_path: Path):
    root = tmp_path / "runtime"
    first = _live("kali-3f2a", os.getpid())
    second = _live("alpine-91b0", os.getpid(), label="alpine-min")
    supervisor.write_live(first, root=root)
    supervisor.write_live(second, root=root)

    entries = supervisor.list_live(root=root, validator=lambda _: True)

    assert [entry.id for entry in entries] == ["alpine-91b0", "kali-3f2a"]


def test_list_live_poda_un_json_roto(tmp_path: Path):
    root = tmp_path / "runtime"
    directory = supervisor.prepare_session_dir("rota-0001", root=root)
    path = directory / "live.json"
    path.write_text("{no es json", encoding="utf-8")
    warnings: list[str] = []

    entries = supervisor.list_live(
        root=root,
        validator=lambda _: True,
        on_warning=warnings.append,
    )

    assert entries == ()
    assert not path.exists()
    assert warnings == []


def test_count_others_excluye_la_propia(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("vvm.audit.is_vvm_qemu", lambda _pid: True)
    root = supervisor.runtime_root()
    supervisor.write_live(_live("kali-3f2a", os.getpid()), root=root)
    supervisor.write_live(_live("alpine-91b0", os.getpid()), root=root)

    assert supervisor.count_others("kali-3f2a") == 1
    assert supervisor.count_others() == 2


def test_session_group_instala_marca_y_restaura(
    monkeypatch: pytest.MonkeyPatch,
):
    installed: dict[int, object] = {}
    calls: list[tuple[int, object]] = []
    previous = {signum: object() for signum in SessionGroup.SIGNALS}

    def fake_getsignal(signum: int) -> object:
        return previous[signum]

    def fake_signal(signum: int, handler: object) -> None:
        installed[signum] = handler
        calls.append((signum, handler))

    monkeypatch.setattr(supervisor.signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(supervisor.signal, "signal", fake_signal)
    group = SessionGroup()
    group.install()
    group.install()

    assert group.installed is True
    assert set(installed) == set(SessionGroup.SIGNALS)
    assert len(calls) == len(SessionGroup.SIGNALS)

    group._handle(signal.SIGTERM, None)
    group._handle(signal.SIGINT, None)
    assert group.stop_signal == signal.SIGTERM
    assert group.should_stop() is True

    group.restore()
    group.restore()
    assert group.installed is False


class _FakeSession:
    def __init__(self) -> None:
        self.running = True
        self.finished = False
        self.report: str | None = None
        self.stops = 0
        self.finishes = 0
        self.wipes: list[bool] = []

    def stop(self) -> int:
        self.stops += 1
        self.running = False
        return 143

    def finish(self, *, wipe: bool = False) -> str:
        self.finishes += 1
        self.wipes.append(wipe)
        self.finished = True
        self.report = f"informe {self.finishes}"
        return self.report


def test_stop_all_es_idempotente_y_hace_wipe():
    first = _FakeSession()
    second = _FakeSession()
    group = SessionGroup([first, second])

    reports = group.stop_all(wipe=True)
    again = group.stop_all(wipe=True)

    assert reports == ["informe 1", "informe 1"]
    assert again == reports
    assert first.stops == 1 and second.stops == 1
    assert first.finishes == 1 and second.finishes == 1
    assert first.wipes == [True] and second.wipes == [True]
    assert not first.running and not second.running


def test_stop_all_no_deja_un_hijo_si_stop_falla(monkeypatch: pytest.MonkeyPatch):
    class Broken(_FakeSession):
        def stop(self) -> int:
            raise OSError("sin permiso")

    broken = Broken()
    reports = SessionGroup().stop_all([broken], wipe=True)
    assert reports == ["informe 1"]
    assert broken.finishes == 1
    assert broken.finished is True
