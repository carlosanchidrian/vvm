"""Tests de la TUI sin TTY: pantallas, teclas e invariantes del front-end."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")
pytest.importorskip("pyte")

from textual.widgets import DataTable, Input, Select, Tabs  # noqa: E402

import vvm.tui.app as tui_app  # noqa: E402
from vvm import cli, host_check, profiles, vm  # noqa: E402
from vvm.console import ConsoleSnapshot, Span  # noqa: E402
from vvm.tui.app import VvmApp  # noqa: E402
from vvm.tui.screens import (
    ProfilePickerScreen,  # noqa: E402
    CloseAllScreen,
    DeleteProfileScreen,
    MainScreen,
    PreCheckScreen,
    ProfileFormScreen,
    ProfilesScreen,
    ReportScreen,
    VmMenuScreen,
)
from vvm.tui.widgets import ConsoleView, SessionPanel  # noqa: E402

SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host_ok(monkeypatch: pytest.MonkeyPatch):
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


class _FakeConsole:
    """Consola sin socket para que los tests de TUI no abran hilos."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.sent: list[bytes] = []
        self.bytes_received = 1
        self.stopped = False

    def send(self, data: bytes) -> bool:
        self.sent.append(data)
        return True

    def snapshot(self) -> ConsoleSnapshot:
        return ConsoleSnapshot(
            rows=((Span("~ # _"),),), cursor=(5, 0)
        )

    def stop(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def fake_console(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tui_app, "SerialConsole", _FakeConsole)
    # La latencia de la consola se prueba en test_console; aquí no hace falta
    # un repintado a 10 fps y solo añade ruido al bucle de mensajes.
    monkeypatch.setattr(tui_app.VvmApp, "CONSOLE_FPS", 0)


def _boot(tmp_path: Path) -> tuple[Path, Path]:
    kernel = tmp_path / "vmlinuz"
    initrd = tmp_path / "initramfs"
    kernel.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")
    return kernel, initrd


def _profiles_file(tmp_path: Path) -> Path:
    kernel, initrd = _boot(tmp_path)
    path = tmp_path / "profiles.toml"
    path.write_text(
        f"""
default = "min"

[profiles.min]
kernel = "{kernel}"
initrd = "{initrd}"
mem_mb = 256
disk_mb = 8
""",
        encoding="utf-8",
    )
    return path


def _two_profiles_file(tmp_path: Path) -> Path:
    kernel, initrd = _boot(tmp_path)
    path = tmp_path / "profiles.toml"
    path.write_text(
        f"""
default = "uno"

[profiles.uno]
kernel = "{kernel}"
initrd = "{initrd}"
mem_mb = 256
disk_mb = 8

[profiles.dos]
kernel = "{kernel}"
initrd = "{initrd}"
mem_mb = 256
disk_mb = 8
""",
        encoding="utf-8",
    )
    return path


def _fake_qemu(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-qemu"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _looping_qemu(tmp_path: Path, marker: Path) -> Path:
    return _fake_qemu(
        tmp_path,
        (
            f"echo $$ >> {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 1000 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )


def _app(tmp_path: Path, qemu: str = "/bin/true", **kwargs) -> VvmApp:
    kwargs.setdefault("refresh_interval", 1.0)
    return VvmApp(
        profiles_path=_profiles_file(tmp_path),
        qemu=qemu,
        **kwargs,
    )


def _wait_for(path: Path, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"no apareció {path}")
        time.sleep(0.02)


def _wait_lines(path: Path, count: int, timeout: float = 5.0) -> list[int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            lines = [
                int(line)
                for line in path.read_text(encoding="utf-8").split()
                if line.strip()
            ]
        except (OSError, ValueError):
            lines = []
        if len(lines) >= count:
            return lines
        time.sleep(0.02)
    raise AssertionError(f"no aparecieron {count} pids en {path}")


def _kill_marked_children(marker: Path) -> None:
    try:
        pids = [
            int(line)
            for line in marker.read_text(encoding="utf-8").split()
            if line.strip()
        ]
    except (OSError, ValueError):
        return
    for child in pids:
        try:
            os.kill(child, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_sessions(pilot, app: VvmApp, count: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        ready = len(app.sessions) >= count
        if ready and app.main_screen is not None:
            ready = all(
                (tab := app.main_screen.tab_for(runtime_id)) is not None
                and tab.is_mounted
                and bool(tab.query(SessionPanel))
                for runtime_id in app.sessions
            )
        if ready:
            return
        if time.monotonic() > deadline:
            raise AssertionError(
                f"sesiones esperadas {count}, hay {len(app.sessions)}"
            )
        await pilot.pause(0.05)


async def _wait_finished(pilot, app: VvmApp, runtime_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        runtime = app.sessions.get(runtime_id)
        if runtime is not None and runtime.report is not None:
            break
        if time.monotonic() > deadline:
            raise AssertionError(f"la VM {runtime_id} no terminó")
        await pilot.pause(0.05)
    while app.workers:
        if time.monotonic() > deadline:
            raise AssertionError("quedaron workers de cierre")
        await asyncio.sleep(0.02)


async def _wait_tab(pilot, app: VvmApp, runtime_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while True:
        tab = app.main_screen.tab_for(runtime_id) if app.main_screen else None
        if tab is not None and tab.is_mounted:
            return tab
        if time.monotonic() > deadline:
            raise AssertionError(f"no apareció la pestaña de {runtime_id}")
        await pilot.pause(0.05)


async def _quit_profiles(pilot, app: VvmApp) -> None:
    if isinstance(app.screen, ProfilesScreen) and app.return_code is None:
        await pilot.press("q")
        deadline = time.monotonic() + 5
        while app.workers:
            if time.monotonic() > deadline:
                raise AssertionError("quedaron workers al salir")
            await asyncio.sleep(0.02)
        await pilot.pause(0.3)


@asynccontextmanager
async def _running_app(app: VvmApp, size=(100, 35)):
    """run_test que cierra la app aunque una aserción falle dentro."""
    async with app.run_test(size=size) as pilot:
        try:
            yield pilot
        finally:
            if app.return_code is None:
                app.exit(return_code=0)


async def _wait_state(
    pilot,
    predicate,
    message: str,
    timeout: float = 5.0,
) -> None:
    """Espera activa de un efecto de la UI, con deadline y estado en el error."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"{message} (estado actual: {predicate()!r})")
        await pilot.pause(0.05)


async def _close_active_and_wait(pilot, app: VvmApp, timeout: float = 8.0) -> None:
    """Cierra la VM activa desde el menú y deja tiempo al cierre."""
    await pilot.press("ctrl+g")
    await pilot.pause()
    await pilot.press("q")
    await pilot.pause(1.0)
    deadline = time.monotonic() + timeout
    while app.workers:
        if time.monotonic() > deadline:
            raise AssertionError("quedaron workers de cierre")
        await asyncio.sleep(0.02)


async def _start_first(pilot) -> None:
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(pilot.app.screen, PreCheckScreen)
    await pilot.press("enter")
    await pilot.pause(0.3)


async def _start_second(pilot) -> None:
    await pilot.press("n")
    await pilot.pause()
    assert isinstance(pilot.app.screen, ProfilePickerScreen)
    await pilot.press("up")
    await pilot.pause()
    await pilot.press("enter")
    await pilot.pause()
    assert isinstance(pilot.app.screen, PreCheckScreen)
    await pilot.press("enter")
    await pilot.pause(0.3)


def test_carga_perfiles_entra_al_prechequeo_y_sale_con_q(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)
            assert "min" in app.export_text()

            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PreCheckScreen)

            await pilot.press("q")

    asyncio.run(run())
    assert app.return_code == 0


def test_la_linea_de_la_tui_lleva_la_serie_a_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    app = _app(tmp_path)
    loaded = profiles.load_profiles(tmp_path / "profiles.toml")
    profile = loaded.get("min")

    config, results = app.precheck(profile)
    cmd = vm.build_cmd(
        config,
        "<fd>" if config.disk_mb > 0 else None,
        qemu=app.qemu,
    )

    assert config.serial_mode == "socket"
    assert config.serial_sock == "<socket>"
    assert config.serial_log is None
    assert cmd[cmd.index("-monitor") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1] == "chardev:ser0"
    chardev = cmd[cmd.index("-chardev") + 1]
    assert chardev.startswith("socket,id=ser0,path=<socket>")
    assert chardev.endswith("server=on,wait=off")
    assert "mon:stdio" not in cmd
    assert "-qmp" not in cmd
    by_id = {result.id: result for result in results}
    assert by_id["P2"].status == "OK"
    assert by_id["P7"].status == "OK"
    assert by_id["P9"].status == "OK"


def test_qemu_true_termina_y_muestra_el_informe_sin_cerrar_la_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await _start_first(pilot)
            await _wait_sessions(pilot, app, 1)
            runtime_id = next(iter(app.sessions))
            await _wait_finished(pilot, app, runtime_id)
            assert isinstance(app.screen, MainScreen)
            assert app.return_code is None
            runtime = app.sessions[runtime_id]
            assert runtime.report is not None
            assert "Informe de sesión VVM" in runtime.report
            tab = app.main_screen.tab_for(runtime_id)
            assert tab is not None
            assert tab.symbol == "○"
            assert "Informe de sesión VVM" in app.export_text()
            await _close_active_and_wait(pilot, app)
            assert isinstance(app.screen, ProfilesScreen)
            await pilot.press("q")
            await pilot.pause(0.2)

    asyncio.run(run())
    assert app.return_code == 0


def test_s_para_el_fake_y_deja_el_informe_en_la_pestana(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = _app(tmp_path, qemu=str(qemu))
    try:

        async def run() -> None:
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                pids = _wait_lines(marker, 1)
                runtime_id = next(iter(app.sessions))
                await pilot.press("ctrl+g")
                await pilot.pause()
                assert isinstance(app.screen, VmMenuScreen)
                await pilot.press("s")
                await _wait_finished(pilot, app, runtime_id)
                assert isinstance(app.screen, MainScreen)
                assert app.sessions[runtime_id].report is not None
                assert app.main_screen.tab_for(runtime_id).symbol == "○"
                assert not _alive(pids[0])
                await _close_active_and_wait(pilot, app)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_dos_vms_pestanas_y_cierre_granular(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_two_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                await _start_second(pilot)
                await _wait_sessions(pilot, app, 2)
                assert isinstance(app.screen, MainScreen)

                first_id, second_id = app.main_screen.pane_order
                assert first_id != second_id
                pids = _wait_lines(marker, 2)
                first_session = app.sessions[first_id].session
                second_session = app.sessions[second_id].session
                assert first_session.running and second_session.running
                for runtime_id in (first_id, second_id):
                    tab = app.main_screen.tab_for(runtime_id)
                    assert tab is not None and tab.symbol == "●"

                await pilot.press("ctrl+g")
                await pilot.pause()
                await pilot.press("q")
                await pilot.pause(0.6)

                assert isinstance(app.screen, MainScreen)
                assert app.main_screen.active_runtime_id() == first_id
                assert list(app.sessions) == [first_id]
                assert first_session.running
                assert second_session.poll() is not None
                assert not _alive(pids[1])

                await _close_active_and_wait(pilot, app)
                assert isinstance(app.screen, ProfilesScreen)
                assert not app.sessions
                assert not _alive(pids[0])
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_q_sin_captura_cierra_solo_la_vm_activa(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_two_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                await _start_second(pilot)
                await _wait_sessions(pilot, app, 2)
                first_id, second_id = app.main_screen.pane_order
                first_session = app.sessions[first_id].session
                second_session = app.sessions[second_id].session

                await pilot.press("q")
                await pilot.pause(0.6)

                assert isinstance(app.screen, MainScreen)
                assert list(app.sessions) == [first_id]
                assert first_session.running
                assert second_session.poll() is not None

                await _close_active_and_wait(pilot, app)
                assert isinstance(app.screen, ProfilesScreen)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_captura_envia_teclas_al_guest_y_ctrl_q_sigue_disponible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    created: list[object] = []

    class FakeConsole:
        def __init__(self, path: str) -> None:
            self.path = path
            self.sent: list[bytes] = []
            self.bytes_received = 0
            self.stopped = False

        def send(self, data: bytes) -> bool:
            self.sent.append(data)
            return True

        def snapshot(self) -> ConsoleSnapshot:
            return ConsoleSnapshot(
                rows=((Span("~ # "),),), cursor=(4, 0)
            )

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(
        tui_app,
        "SerialConsole",
        lambda path: created.append(FakeConsole(path)) or created[-1],
    )
    app = _app(tmp_path, qemu=str(qemu))
    try:

        async def run() -> None:
            async with _running_app(app) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                runtime_id = next(iter(app.sessions))
                tab = await _wait_tab(pilot, app, runtime_id)
                panel = tab.query_one(SessionPanel)
                panel.focus()
                await _wait_state(
                    pilot, lambda: app.focused is panel, "el panel no recibió el foco"
                )
                await pilot.press("enter")
                view = app.main_screen.tab_for(runtime_id).query_one(ConsoleView)
                await _wait_state(
                    pilot,
                    lambda: view.capture,
                    f"la captura no se activó (foco={app.focused!r})",
                )
                assert view.has_class("capturing")

                await pilot.press("q")
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)
                assert runtime_id in app.sessions

                await pilot.press("ctrl+c")
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)

                await pilot.press("s")
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)

                fake = created[0]
                assert fake.sent == [b"q", b"\x03", b"s"]

                await pilot.press("ctrl+q")
                await pilot.pause()
                assert isinstance(app.screen, CloseAllScreen)
                await pilot.press("escape")
                await pilot.pause()
                assert isinstance(app.screen, MainScreen)
                assert runtime_id in app.sessions

                await _close_active_and_wait(pilot, app)
                assert isinstance(app.screen, ProfilesScreen)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_cierre_total_pide_confirmacion_y_no_cierra_al_abrirla(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_two_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    sessions_seen = []
    try:

        async def run() -> None:
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                await _start_second(pilot)
                await _wait_sessions(pilot, app, 2)
                pids = _wait_lines(marker, 2)
                sessions_seen.extend(
                    app.sessions[key].session for key in app.sessions
                )

                await pilot.click("#close-all")
                await pilot.pause()
                assert isinstance(app.screen, CloseAllScreen)
                assert all(session.running for session in sessions_seen)
                assert all(
                    runtime.report is None for runtime in app.sessions.values()
                )

                await pilot.press("enter")
                await pilot.pause(1.0)
                assert app.return_code == 0

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)

    assert len(sessions_seen) == 2
    assert all("Wipe: 8 MiB" in (report or "") for report in app.last_reports.values())
    assert not any(_alive(pid) for pid in _wait_lines(marker, 2))


def test_escape_no_para_ninguna_vm_en_la_confirmacion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = _app(tmp_path, qemu=str(qemu))
    try:

        async def run() -> None:
            async with _running_app(app) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                runtime_id = next(iter(app.sessions))
                session = app.sessions[runtime_id].session

                await pilot.press("ctrl+q")
                await pilot.pause()
                assert isinstance(app.screen, CloseAllScreen)
                await pilot.press("escape")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, MainScreen),
                    "escape no volvió al centro de control",
                )
                await _wait_state(
                    pilot,
                    lambda: session.running,
                    "escape paró la VM en vez de cancelar el modal",
                )
                assert runtime_id in app.sessions

                await _close_active_and_wait(pilot, app)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_sigterm_cierra_todas_las_vms_y_sale_con_143(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = _app(tmp_path, qemu=str(qemu))
    runtime_id = ""
    try:

        async def run() -> None:
            nonlocal runtime_id
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                runtime_id = next(iter(app.sessions))
                pids = _wait_lines(marker, 1)
                assert app.group.installed is True

                os.kill(os.getpid(), signal.SIGTERM)
                deadline = time.monotonic() + 5
                while app.return_code is None:
                    if time.monotonic() > deadline:
                        raise AssertionError("la app no salió tras SIGTERM")
                    await pilot.pause(0.1)

                assert app.return_code == 143
                report = app.sessions[runtime_id].report
                assert report is not None
                assert "Wipe: 8 MiB" in report
                assert not _alive(pids[0])

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_home_y_cierre_total_en_la_barra_superior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_two_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with _running_app(app, (110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                await _start_second(pilot)
                await _wait_sessions(pilot, app, 2)
                _wait_lines(marker, 2)
                sessions = [runtime.session for runtime in app.sessions.values()]

                texto = app.export_text()
                assert "✕" in texto
                assert "⌂" in texto
                assert "Arrancar una VM" not in texto

                await pilot.click("#home")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, ProfilePickerScreen),
                    "home no abrió el selector de perfiles",
                )
                assert len(app.sessions) == 2
                assert all(session.running for session in sessions)
                for runtime_id in app.sessions:
                    assert app.main_screen.tab_for(runtime_id).symbol == "●"

                await pilot.press("escape")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, MainScreen),
                    "escape no volvió al centro de control",
                )
                assert all(session.running for session in sessions)

                await pilot.click("#close-all")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, CloseAllScreen),
                    "el botón de cierre no abrió la confirmación",
                )
                assert all(session.running for session in sessions)

                await pilot.press("escape")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, MainScreen),
                    "escape no canceló el cierre total",
                )
                assert all(session.running for session in sessions)

                await pilot.press("ctrl+q")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, CloseAllScreen),
                    "Ctrl-Q no abrió la confirmación",
                )
                await pilot.press("enter")
                await _wait_state(
                    pilot,
                    lambda: app.return_code is not None,
                    "el cierre total no terminó",
                )

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)


def test_el_boton_de_cierre_total_esta_anclado_a_la_derecha():
    css = (ROOT / "vvm" / "tui" / "app.tcss").read_text(encoding="utf-8")
    assert "#close-all {" in css
    block = css.split("#close-all {", 1)[1].split("}", 1)[0]
    assert "dock: right" in block
    assert "#home {" in css
    home = css.split("#home {", 1)[1].split("}", 1)[0]
    assert "dock: left" in home
    assert "#3b82f6" in home


def test_tui_sin_tty_sale_2(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    assert cli.main(["tui"]) == 2

    err = capsys.readouterr().err
    assert "terminal interactivo" in err
    assert "vvm run --profile NOMBRE" in err


def test_tui_sin_textual_sale_2(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_textual_available", lambda: False)

    assert cli.main(["tui"]) == 2

    err = capsys.readouterr().err
    assert "falta el extra de la TUI" in err
    assert "pip install -e '.[tui]'" in err


def test_tui_con_perfil_explicito_y_sin_fichero_sale_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "vacio"))

    assert cli.main(["tui", "--profile", "kali"]) == 2

    err = capsys.readouterr().err
    assert "no existe el fichero de perfiles" in err
    assert str(tmp_path / "vacio" / "vvm" / "profiles.toml") in err


def test_tui_sin_toml_abre_lista_vacia_y_crea_el_primer_perfil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = tmp_path / "config" / "vvm" / "profiles.toml"
    kernel, initrd = _boot(tmp_path)
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.5,
    )

    async def run() -> None:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)
            assert "No hay perfiles" in app.export_text()

            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, ProfileFormScreen)

            app.screen.query_one("#profile-name", Input).value = "min"
            app.screen.query_one("#profile-source", Select).value = "kernel"
            app.screen.query_one("#profile-kernel", Input).value = str(kernel)
            app.screen.query_one("#profile-initrd", Input).value = str(initrd)
            app.screen.query_one("#profile-mem", Input).value = "256"
            app.screen.query_one("#profile-disk", Input).value = "0"
            app.screen.query_one("#profile-cpus", Input).value = "1"
            await pilot.click("#save")
            await pilot.pause()

            assert isinstance(app.screen, ProfilesScreen)
            assert "min" in app.export_text()
            await pilot.press("q")

    asyncio.run(run())

    loaded = profiles.load_profiles(target)
    assert loaded.default == "min"
    assert loaded.get("min").kernel == str(kernel)
    assert loaded.get("min").initrd == str(initrd)
    assert loaded.get("min").disk_mb == 0


def test_el_formulario_anade_un_perfil_a_un_toml_existente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = _profiles_file(tmp_path)
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * 1024)
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.5,
    )

    async def run() -> None:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, ProfileFormScreen)

            app.screen.query_one("#profile-name", Input).value = "live"
            app.screen.query_one("#profile-image", Input).value = str(iso)
            app.screen.query_one("#profile-mem", Input).value = "2048"
            app.screen.query_one("#profile-disk", Input).value = "0"
            await pilot.click("#save")
            await pilot.pause()

            assert isinstance(app.screen, ProfilesScreen)
            assert "live" in app.export_text()
            await pilot.press("q")

    asyncio.run(run())

    loaded = profiles.load_profiles(target)
    assert loaded.default == "min"
    assert set(loaded.profiles) == {"live", "min"}
    assert loaded.get("live").image == str(iso)


def test_el_formulario_rechaza_un_nombre_duplicado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = _profiles_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.5,
    )

    async def run() -> None:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            app.screen.query_one("#profile-name", Input).value = "min"
            app.screen.query_one("#profile-kernel", Input).value = "/k"
            app.screen.query_one("#profile-initrd", Input).value = "/i"
            await pilot.click("#save")
            await pilot.pause()

            assert isinstance(app.screen, ProfileFormScreen)
            assert "Ya existe" in app.export_text()
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)
            await pilot.press("q")

    asyncio.run(run())

    assert target.read_text(encoding="utf-8") == original


def test_importar_el_paquete_tui_no_importa_textual():
    code = "import sys, vvm.tui; print('textual' in sys.modules)"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


def test_invariante_sin_qemu_system_en_la_tui():
    for path in (ROOT / "vvm" / "tui").rglob("*"):
        if path.is_file():
            assert "qemu-system" not in path.read_text(
                encoding="utf-8", errors="replace"
            )


def test_invariante_sin_rutas_proc_en_la_tui():
    for path in (ROOT / "vvm" / "tui").rglob("*"):
        if path.is_file():
            assert "/proc/" not in path.read_text(
                encoding="utf-8", errors="replace"
            )


def test_editar_un_perfil_existente_conserva_el_resto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = _profiles_file(tmp_path)
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * 1024)
    profiles.add_profile(
        target,
        profiles.Profile(
            name="live",
            image=str(iso),
            mem_mb=2048,
            disk_mb=0,
        ),
    )
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.5,
    )

    async def run() -> None:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, ProfileFormScreen)
            assert app.screen.editing is not None
            assert app.screen.editing.name == "min"
            assert app.screen.query_one("#profile-name", Input).disabled is True
            app.screen.query_one("#profile-mem", Input).value = "1024"
            app.screen.query_one("#profile-disk", Input).value = "0"
            await pilot.click("#save")
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)
            await pilot.press("q")

    asyncio.run(run())

    loaded = profiles.load_profiles(target)
    assert set(loaded.profiles) == {"live", "min"}
    assert loaded.default == "min"
    assert loaded.get("min").mem_mb == 1024
    assert loaded.get("min").disk_mb == 0
    assert loaded.get("live").mem_mb == 2048


def test_cancelar_el_borrado_no_toca_el_fichero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = _profiles_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.5,
    )

    async def run() -> None:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteProfileScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)
            await pilot.press("q")

    asyncio.run(run())
    assert target.read_text(encoding="utf-8") == original


def test_borrar_el_default_reasigna_el_siguiente(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = _profiles_file(tmp_path)
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * 1024)
    profiles.add_profile(
        target,
        profiles.Profile(
            name="live",
            image=str(iso),
            mem_mb=2048,
            disk_mb=0,
        ),
    )
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.5,
    )

    async def run() -> None:
        async with app.run_test(size=(110, 40)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DeleteProfileScreen)
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, ProfilesScreen)
            assert "min" not in app.export_text()
            await pilot.press("q")

    asyncio.run(run())

    loaded = profiles.load_profiles(target)
    assert set(loaded.profiles) == {"live"}
    assert loaded.default == "live"


def test_la_sesion_respeta_el_wipe_del_perfil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    target = _profiles_file(tmp_path)
    kernel, initrd = _boot(tmp_path)
    profiles.update_profile(
        target,
        profiles.Profile(
            name="min",
            kernel=str(kernel),
            initrd=str(initrd),
            mem_mb=256,
            disk_mb=8,
            wipe=True,
        ),
    )
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=target,
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                runtime_id = next(iter(app.sessions))
                await pilot.press("ctrl+g")
                await pilot.pause()
                await pilot.press("s")
                await _wait_finished(pilot, app, runtime_id)
                assert app.sessions[runtime_id].wipe is True
                assert "Wipe: 8 MiB" in app.last_report
                await _close_active_and_wait(pilot, app)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)

def test_el_selector_arranca_otra_vm_sin_cerrar_las_vivas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_two_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with _running_app(app, (110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                primera = next(iter(app.sessions.values())).session

                await pilot.click("#home")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, ProfilePickerScreen),
                    "home no abrió el selector",
                )
                tabla = app.screen.query_one(DataTable)
                assert tabla.row_count == 2

                # el cursor está en el perfil por defecto: se baja al otro
                await pilot.press("down")
                await pilot.press("enter")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, PreCheckScreen),
                    "enter en el selector no llevó al pre-chequeo",
                )
                assert primera.running
                assert len(app.sessions) == 1

                await pilot.press("escape")
                await _wait_state(
                    pilot,
                    lambda: isinstance(app.screen, MainScreen),
                    "escape en el pre-chequeo no volvió al centro de control",
                )
                assert primera.running
                assert "⌂" in app.export_text()

                await _close_active_and_wait(pilot, app)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)

def test_la_pista_de_teclas_no_repite_el_footer_y_se_oculta_al_capturar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setattr(tui_app.VvmApp, "CONSOLE_FPS", 0)
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with _running_app(app, (100, 32)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                app._refresh_sessions()
                await pilot.pause()

                tab = app.main_screen.active_tab()
                pista = tab.query_one(".console-hint")
                assert pista.display is True
                assert "enter: consola" in str(pista.render())
                # ni el `q` ni el cierre total se repiten: los lleva el footer
                assert "cerrar" not in str(pista.render()).lower()

                view = tab.query_one(ConsoleView)
                view.enter_capture()
                app._refresh_sessions()
                await pilot.pause()
                assert view.capture is True
                assert pista.display is False

                view.leave_capture()
                app._refresh_sessions()
                await pilot.pause()
                assert pista.display is True

                await _close_active_and_wait(pilot, app)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)

def test_cada_pestana_tiene_su_x_y_cierra_solo_esa_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setattr(tui_app.VvmApp, "CONSOLE_FPS", 0)
    marker = tmp_path / "qemu.pid"
    qemu = _looping_qemu(tmp_path, marker)
    app = VvmApp(
        profiles_path=_two_profiles_file(tmp_path),
        qemu=str(qemu),
        refresh_interval=0.2,
    )
    try:

        async def run() -> None:
            async with _running_app(app, (110, 40)) as pilot:
                await pilot.pause()
                await _start_first(pilot)
                await _wait_sessions(pilot, app, 1)
                await _start_second(pilot)
                await _wait_sessions(pilot, app, 2)

                # la tira propia sustituye a la barra de pestañas de Textual
                assert app.main_screen.query_one(Tabs).display is False
                for runtime_id in app.sessions:
                    assert app.main_screen.query_one(f"#tabbtn-{runtime_id}")
                    assert app.main_screen.query_one(f"#tabclose-{runtime_id}")
                # la cabecera ya no repite el símbolo de estado
                cabecera = app.main_screen.active_tab().query_one(
                    ".session-header"
                ).render()
                assert not str(cabecera).lstrip().startswith("●")

                primero, segundo = list(app.sessions)
                assert app.main_screen.active_runtime_id() == segundo

                # la etiqueta activa cambia de pestaña y se marca
                await pilot.click(f"#tabbtn-{primero}")
                await pilot.pause()
                assert app.main_screen.active_runtime_id() == primero
                assert app.main_screen.query_one(
                    f"#tabbtn-{primero}"
                ).has_class("active-tab")

                # la ✕ de la pestaña NO activa se lleva solo esa VM
                await pilot.click(f"#tabclose-{segundo}")
                await _wait_state(
                    pilot,
                    lambda: segundo not in app.sessions,
                    "la ✕ no retiró la pestaña cerrada",
                )
                assert primero in app.sessions
                await _wait_state(
                    pilot,
                    lambda: not app.main_screen.query(f"#tabitem-{segundo}"),
                    "la tira se quedó con la pestaña cerrada",
                )
                assert app.main_screen.active_runtime_id() == primero

                await _close_active_and_wait(pilot, app)
                await _quit_profiles(pilot, app)

        asyncio.run(run())
    finally:
        _kill_marked_children(marker)
