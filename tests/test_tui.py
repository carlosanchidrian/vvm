"""Tests de la TUI sin TTY: pantallas, teclas e invariantes del front-end."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("textual")

from vvm import cli, host_check, profiles, vm  # noqa: E402
from vvm.tui.app import VvmApp  # noqa: E402
from vvm.tui.screens import (  # noqa: E402
    DeleteProfileScreen,
    PreCheckScreen,
    ProfileFormScreen,
    ProfilesScreen,
    ReportScreen,
    SessionScreen,
)
from textual.widgets import Input, Select  # noqa: E402

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


def _fake_qemu(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-qemu"
    script.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def _app(tmp_path: Path, qemu: str = "/bin/true", **kwargs) -> VvmApp:
    kwargs.setdefault("auto_exit_on_finish", False)
    kwargs.setdefault("refresh_interval", 0.05)
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


def _kill_marked_child(marker: Path) -> None:
    try:
        child = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass


def test_carga_perfiles_entra_al_prechequeo_y_sale_con_q(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
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


def test_la_linea_de_la_tui_lleva_la_serie_a_fichero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    app = _app(tmp_path)
    loaded = profiles.load_profiles(tmp_path / "profiles.toml")
    profile = loaded.get("min")

    config, results = app.precheck(profile)
    cmd = vm.build_cmd(
        config,
        "<fd>" if config.disk_mb > 0 else None,
        qemu=app.qemu,
    )

    assert config.serial_mode == "file"
    assert config.serial_log == str(tmp_path / "runtime" / "vvm" / "serie.log")
    assert cmd[cmd.index("-monitor") + 1] == "none"
    assert cmd[cmd.index("-serial") + 1].startswith("file:")
    assert "mon:stdio" not in cmd
    by_id = {result.id: result for result in results}
    assert by_id["P2"].status == "OK"
    assert by_id["P7"].status == "OK"
    assert by_id["P9"].status == "OK"


def test_qemu_true_termina_y_muestra_el_informe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    app = _app(tmp_path)

    async def run() -> None:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.4)
            assert isinstance(app.screen, ReportScreen)
            assert app.last_report is not None
            assert "Informe de sesión VVM" in app.last_report
            assert "Informe de sesión VVM" in app.export_text()
            await pilot.press("q")

    asyncio.run(run())
    assert app.return_code == 0


def test_la_app_se_cierra_sola_al_terminar_el_hijo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    app = _app(tmp_path, auto_exit_on_finish=True, exit_delay=0.1)

    async def run() -> None:
        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.5)

    asyncio.run(run())
    assert app.return_code == 0


def test_s_para_el_fake_y_sale_sin_hijos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host_ok
):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    marker = tmp_path / "qemu.pid"
    qemu = _fake_qemu(
        tmp_path,
        (
            f"echo $$ > {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    app = _app(tmp_path, qemu=str(qemu))
    try:

        async def run() -> None:
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.2)
                assert isinstance(app.screen, SessionScreen)
                _wait_for(marker)
                await pilot.press("s")

        asyncio.run(run())
        assert app.return_code == 0
        child = int(marker.read_text(encoding="utf-8").strip())
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
    finally:
        _kill_marked_child(marker)


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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    target = tmp_path / "config" / "vvm" / "profiles.toml"
    kernel, initrd = _boot(tmp_path)
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.05,
        auto_exit_on_finish=False,
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    target = _profiles_file(tmp_path)
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * 1024)
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.05,
        auto_exit_on_finish=False,
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    target = _profiles_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.05,
        auto_exit_on_finish=False,
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
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
        refresh_interval=0.05,
        auto_exit_on_finish=False,
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    target = _profiles_file(tmp_path)
    original = target.read_text(encoding="utf-8")
    app = VvmApp(
        profiles_path=target,
        qemu="/bin/true",
        refresh_interval=0.05,
        auto_exit_on_finish=False,
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
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
        refresh_interval=0.05,
        auto_exit_on_finish=False,
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
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
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
    qemu = _fake_qemu(
        tmp_path,
        (
            "sleep 0.3\n"
            f"echo $$ > {marker}\n"
            "trap 'exit 0' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    app = VvmApp(
        profiles_path=target,
        qemu=str(qemu),
        refresh_interval=0.05,
        auto_exit_on_finish=False,
    )
    try:

        async def run() -> None:
            async with app.run_test(size=(110, 40)) as pilot:
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause(0.1)
                _wait_for(marker)
                await pilot.press("s")

        asyncio.run(run())
        assert app.last_report is not None
        assert "Wipe: 8 MiB" in app.last_report
    finally:
        _kill_marked_child(marker)
