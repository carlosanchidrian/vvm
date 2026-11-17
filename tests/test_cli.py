"""Tests del CLI: `check`, `run` y `audit` sin KVM ni QEMU real."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from vvm import audit, cli, host_check, images
from vvm.vm import ProcessIO

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


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


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


# Bucle acotado (~20 s) para que un fake que no atiende la señal no cuelgue la
# suite indefinidamente.
_FAKE_LOOP = "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"


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
    """Mata el QEMU falso del marcador si el CLI falló antes de reapearlo."""
    try:
        child = int(marker.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(child, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _cli_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    return env


def _cli_run_command(
    tmp_path: Path,
    kernel: Path,
    initrd: Path,
    qemu: Path,
    report: Path,
    *extra: str,
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "vvm",
        "run",
        "--kernel",
        str(kernel),
        "--initrd",
        str(initrd),
        "--qemu",
        str(qemu),
        "--force",
        "--report",
        str(report),
        *extra,
    ]


# --- check ---------------------------------------------------------------


def test_check_apto_sale_0(host_ok, capsys):
    assert cli.main(["check"]) == 0

    out = capsys.readouterr().out
    assert "Aceleración KVM disponible:" in out
    assert "Sin restos vvm-* en /dev/shm:" in out
    assert "(P1)" not in out and "(P8)" not in out
    assert "apto para arrancar" in out
    assert "\033[" not in out


def test_check_con_swap_sale_1(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)

    assert cli.main(["check"]) == 1

    out = capsys.readouterr().out
    assert "[FALLA]" in out
    assert "Sin swap activa:" in out
    assert "(P3)" not in out
    assert "/swap.img" in out
    assert "fallo(s) bloqueante(s)" in out


def test_check_sin_kvm_sale_1(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(host_check, "kvm_accessible", lambda: False)

    assert cli.main(["check"]) == 1

    out = capsys.readouterr().out
    assert "[FALLA]" in out
    assert "Aceleración KVM disponible:" in out
    assert "(P1)" not in out


def test_check_memoria_baja_sale_1(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(host_check, "read_meminfo", lambda: {"MemAvailable": 600 * 1024})

    assert cli.main(["check"]) == 1

    out = capsys.readouterr().out
    assert "[FALLA]" in out
    assert "Memoria RAM suficiente:" in out
    assert "(P2)" not in out
    assert "necesario=1280 MB" in out


def test_check_colorea_las_entradas_con_tty(host_ok, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_stdout_supports_color", lambda: True)
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)

    assert cli.main(["check"]) == 1

    out = capsys.readouterr().out
    assert "\033[31m" in out
    assert "\033[32m" in out
    assert "\033[0m" in out


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


def test_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == "vvm 0.1.0"


# --- run -----------------------------------------------------------------


def test_run_dry_run_imprime_linea_y_no_lanza(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("no debe ejecutarse en --dry-run")

    monkeypatch.setattr(cli.host_check, "check_host", boom)
    monkeypatch.setattr(cli.vm, "run_session", boom)
    monkeypatch.setattr(cli.images, "create_disk_fd", boom)
    monkeypatch.setattr(cli.images, "create_image_fd", boom)

    assert cli.main(_run_args(kernel, initrd, "--dry-run")) == 0

    out = capsys.readouterr().out
    assert "memory-backend-memfd,id=mem,size=512M,share=off" in out
    assert "file=/dev/fd/<fd>,format=raw,if=virtio,cache=writethrough" in out
    assert "-nic none" in out


def test_run_con_bloqueo_aborta_sin_crear_fds(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)

    def boom(*args, **kwargs):
        raise AssertionError("no debe lanzarse QEMU si hay bloqueos")

    monkeypatch.setattr(cli.vm, "run_session", boom)
    monkeypatch.setattr(cli.images, "create_disk_fd", boom)

    assert cli.main(_run_args(kernel, initrd)) == 1

    captured = capsys.readouterr()
    assert "[FALLA]" in captured.out
    assert "Sin swap activa:" in captured.out
    assert "--force" in captured.err


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
    saved = audit.load_session_full(session)
    assert len(saved.checks or ()) == 10
    assert saved.forced is False
    assert saved.extras.closure == "normal"


def test_run_cierra_el_disco_antes_de_la_instantanea_final(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    monkeypatch.setattr(cli.audit, "default_session_path", lambda: session)
    fds: list[int] = []
    _capture_disk_fd(monkeypatch, fds)

    calls = {"n": 0}

    def fake_snapshot():
        calls["n"] += 1
        if calls["n"] == 1:
            return _snapshot(shmem_kb=1_000)
        try:
            os.fstat(fds[0])
            abierto = True
        except OSError:
            abierto = False
        # Si el disco siguiera abierto, Shmem incluiría 209 MB escritos por el guest.
        return _snapshot(shmem_kb=1_000 + (209 * 1024 if abierto else 0))

    monkeypatch.setattr(cli.audit, "take_snapshot", fake_snapshot)

    assert cli.main(_run_args(kernel, initrd, "--qemu", "/bin/true")) == 0

    assert calls["n"] == 2
    out = capsys.readouterr().out
    assert "delta +0 kB" in out
    assert "+214016" not in out


def test_run_escribe_informe_en_fichero(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    report = tmp_path / "informe.txt"
    _stub_session(monkeypatch, session)

    args = _run_args(kernel, initrd, "--qemu", "/bin/true", "--report", str(report))
    assert cli.main(args) == 0

    assert report.read_text(encoding="utf-8").startswith("Informe de sesión VVM")
    assert f"Informe escrito en {report}" in capsys.readouterr().out


def test_run_crea_el_directorio_del_informe(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)
    report = tmp_path / "no" / "existe" / "informe.txt"

    args = _run_args(kernel, initrd, "--qemu", "/bin/true", "--report", str(report))
    assert cli.main(args) == 0

    assert report.is_file()
    assert report.read_text(encoding="utf-8").startswith("Informe de sesión VVM")


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

    def interrupted(cmd, pass_fds=(), **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.vm, "run_session", interrupted)

    assert cli.main(_run_args(kernel, initrd)) == 130

    captured = capsys.readouterr()
    assert "SIN HALLAZGOS EN LO COMPROBADO" in captured.out
    assert "interrumpida" in captured.err


def test_run_force_ignora_bloqueos(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)
    monkeypatch.setattr(
        cli.vm,
        "run_session",
        lambda cmd, pass_fds=(), **kwargs: cli.vm.RunOutcome(returncode=0),
    )

    assert cli.main(_run_args(kernel, initrd, "--force")) == 0

    captured = capsys.readouterr()
    assert "[FALLA]" in captured.out
    assert "Sin swap activa:" in captured.out
    assert "SIN HALLAZGOS EN LO COMPROBADO" in captured.out
    assert (
        "Arrancando con 1 fallo(s) bloqueante(s) ignorados por --force"
        in captured.err
    )
    saved = audit.load_session_full(session)
    assert saved.forced is True
    assert any(
        check.id == "P3" and check.status == "FAIL"
        for check in (saved.checks or ())
    )


def test_run_headless_sin_log_sale_1(host_ok, monkeypatch, tmp_path, capsys):
    kernel, initrd = _boot_files(tmp_path)

    assert cli.main(_run_args(kernel, initrd, "--headless")) == 1

    assert "log serie" in capsys.readouterr().err


# --- run --image ---------------------------------------------------------


def _image(tmp_path: Path, size: int = 3 * 1024 * 1024) -> Path:
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * (size // 3))
    return iso


def _forbid_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("no debe lanzarse nada con argumentos inválidos")

    monkeypatch.setattr(cli.vm, "run_session", boom)


def test_run_image_con_kernel_sale_2_y_no_lanza(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    iso = _image(tmp_path)
    _forbid_launch(monkeypatch)

    code = cli.main(
        [
            "run",
            "--image",
            str(iso),
            "--kernel",
            str(kernel),
            "--initrd",
            str(initrd),
        ]
    )

    assert code == 2
    assert "--image y --kernel/--initrd son excluyentes" in capsys.readouterr().err


def test_run_image_con_headless_sale_2_y_no_lanza(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    _forbid_launch(monkeypatch)

    assert cli.main(["run", "--image", str(iso), "--headless"]) == 2

    err = capsys.readouterr().err
    assert "--image con --headless no está soportado" in err
    assert "medido: 1 MB leído, 0 B de log" in err
    assert "solo para --kernel/--initrd" in err


def test_run_image_con_log_sale_2_y_no_lanza(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    log = tmp_path / "serie.log"
    _forbid_launch(monkeypatch)

    def boom(*args, **kwargs):
        raise AssertionError("no debe copiarse el ISO si --log no aplica")

    monkeypatch.setattr(cli.images, "create_image_fd", boom)

    assert cli.main(["run", "--image", str(iso), "--log", str(log)]) == 2

    err = capsys.readouterr().err
    assert "--image y --log son excluyentes" in err
    assert not log.exists()


@pytest.mark.parametrize("modo", ["image", "kernel"])
def test_run_cpus_cero_sale_2_sin_copiar(
    host_ok, monkeypatch, tmp_path, capsys, modo
):
    iso = _image(tmp_path)
    kernel, initrd = _boot_files(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("--cpus 0 no debe llegar a crear medios ni lanzar")

    monkeypatch.setattr(cli.images, "create_image_fd", boom)
    monkeypatch.setattr(cli.images, "create_disk_fd", boom)
    _forbid_launch(monkeypatch)

    if modo == "image":
        args = ["run", "--image", str(iso), "--cpus", "0"]
    else:
        args = _run_args(kernel, initrd, "--cpus", "0")

    assert cli.main(args) == 2

    assert "--cpus debe ser mayor que cero" in capsys.readouterr().err


def test_run_kernel_sin_initrd_sale_2_y_no_lanza(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, _ = _boot_files(tmp_path)
    _forbid_launch(monkeypatch)

    assert cli.main(["run", "--kernel", str(kernel)]) == 2

    assert "--kernel sin --initrd" in capsys.readouterr().err


def test_run_image_inexistente_sale_2(host_ok, monkeypatch, tmp_path, capsys):
    missing = tmp_path / "no-existe.iso"
    _forbid_launch(monkeypatch)

    assert cli.main(["run", "--image", str(missing)]) == 2

    err = capsys.readouterr().err
    assert "--image no encontrada" in err
    assert str(missing) in err


def test_run_image_dry_run_imprime_fd_y_no_lee_el_iso(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("--dry-run no debe copiar el ISO ni crear el disco")

    monkeypatch.setattr(cli.images, "create_image_fd", boom)
    monkeypatch.setattr(cli.images, "create_disk_fd", boom)
    _forbid_launch(monkeypatch)

    assert cli.main(["run", "--image", str(iso), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "file=/dev/fd/<fd>,format=raw,media=cdrom,readonly=on" in out
    assert "file=/dev/fd/<fd>,format=raw,if=virtio,cache=writethrough" in out
    assert "-boot d" in out
    assert "-display gtk" in out
    assert "-kernel" not in out


def test_run_image_con_bloqueo_no_copia_el_iso(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    monkeypatch.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)

    def boom(*args, **kwargs):
        raise AssertionError("no debe copiarse el ISO si el pre-chequeo bloquea")

    monkeypatch.setattr(cli.images, "create_image_fd", boom)
    _forbid_launch(monkeypatch)

    assert cli.main(["run", "--image", str(iso)]) == 1

    captured = capsys.readouterr()
    assert "[FALLA]" in captured.out
    assert "--force" in captured.err


def test_run_image_prechequeo_y_linea_base_van_antes_de_copiar(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    session = tmp_path / "sesion.json"
    monkeypatch.setattr(cli.audit, "default_session_path", lambda: session)
    real_check = host_check.check_host
    real_copy = images.create_image_fd
    events: list[str] = []

    def check(*args, **kwargs):
        events.append("precheck")
        return real_check(*args, **kwargs)

    monkeypatch.setattr(cli.host_check, "check_host", check)

    def snapshot():
        events.append("snapshot")
        return _snapshot()

    monkeypatch.setattr(cli.audit, "take_snapshot", snapshot)

    def copy(path, **kwargs):
        events.append("copy")
        return real_copy(path)

    monkeypatch.setattr(cli.images, "create_image_fd", copy)
    monkeypatch.setattr(
        cli.vm,
        "run_session",
        lambda cmd, pass_fds=(), **kwargs: cli.vm.RunOutcome(0),
    )

    assert cli.main(["run", "--image", str(iso), "--disk", "0"]) == 0

    assert events == ["precheck", "snapshot", "copy", "snapshot"]


def test_run_image_shmem_no_descuenta_el_iso(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    session = tmp_path / "sesion.json"
    monkeypatch.setattr(cli.audit, "default_session_path", lambda: session)
    iso_kb = 3 * 1024
    image_fds: list[int] = []
    real_copy = images.create_image_fd

    def capture_copy(path, **kwargs):
        fd = real_copy(path)
        image_fds.append(fd)
        return fd

    monkeypatch.setattr(cli.images, "create_image_fd", capture_copy)

    def snapshot():
        # El memfd del ISO suma a Shmem mientras está abierto: si la línea base
        # se tomara después de copiarlo, su delta saldría negativo.
        abierto = bool(image_fds) and _is_open(image_fds[-1])
        return _snapshot(shmem_kb=100_000 + (iso_kb if abierto else 0))

    monkeypatch.setattr(cli.audit, "take_snapshot", snapshot)
    monkeypatch.setattr(
        cli.vm,
        "run_session",
        lambda cmd, pass_fds=(), **kwargs: cli.vm.RunOutcome(0),
    )

    assert cli.main(["run", "--image", str(iso), "--disk", "0"]) == 0

    before, after = audit.load_session(session)
    assert before.shmem_kb == 100_000
    assert after.shmem_kb == 100_000
    out = capsys.readouterr().out
    assert "delta +0 kB" in out
    assert "-3072" not in out


@pytest.mark.parametrize(
    "extra",
    [["--mem", "0"], ["--disk", "-1"]],
)
def test_run_image_con_argumento_invalido_no_copia_el_iso(
    host_ok, monkeypatch, tmp_path, capsys, extra
):
    iso = _image(tmp_path)

    def boom(*args, **kwargs):
        raise AssertionError("no debe copiarse el ISO con argumentos inválidos")

    monkeypatch.setattr(cli.images, "create_image_fd", boom)
    _forbid_launch(monkeypatch)

    assert cli.main(["run", "--image", str(iso), *extra]) == 2

    assert "Error:" in capsys.readouterr().err


def test_run_image_disk_zero_no_crea_disco_de_datos(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    def boom(size_mb):
        raise AssertionError("--disk 0 no debe crear el disco de datos")

    monkeypatch.setattr(cli.images, "create_disk_fd", boom)

    assert cli.main(["run", "--image", str(iso), "--disk", "0", "--qemu", "/bin/true"]) == 0

    out = capsys.readouterr().out
    assert "SIN HALLAZGOS EN LO COMPROBADO" in out
    saved = audit.load_session_full(session)
    assert len(saved.checks or ()) == 10


def test_run_image_con_qemu_true_emite_informe(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    assert cli.main(["run", "--image", str(iso), "--qemu", "/bin/true"]) == 0

    out = capsys.readouterr().out
    assert "Informe de sesión VVM" in out
    assert "SIN HALLAZGOS EN LO COMPROBADO" in out


def test_check_image_cuenta_el_tamano_y_avisa_de_mem_baja(host_ok, tmp_path, capsys):
    iso = _image(tmp_path)

    assert cli.main(["check", "--image", str(iso)]) == 0

    out = capsys.readouterr().out
    assert "necesario=1283 MB" in out
    assert "3 imagen" in out
    assert "aviso" in out


def test_check_image_inexistente_sale_2(tmp_path, capsys):
    missing = tmp_path / "no-existe.iso"

    assert cli.main(["check", "--image", str(missing)]) == 2

    assert "--image no encontrada" in capsys.readouterr().err


# --- teardown ------------------------------------------------------------


def test_run_wipe_con_disk_zero_declara_sin_disco(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    code = cli.main(
        [
            "run",
            "--image",
            str(iso),
            "--disk",
            "0",
            "--wipe",
            "--qemu",
            "/bin/true",
        ]
    )

    assert code == 0
    assert "Wipe: sin disco de datos" in capsys.readouterr().out
    assert audit.load_session_full(session).extras.wipe == audit.WipeInfo(True, 0)


def test_run_sigterm_sale_143_y_no_deja_hijo(tmp_path):
    kernel, initrd = _boot_files(tmp_path)
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
    report = tmp_path / "informe.txt"
    proc = subprocess.Popen(
        _cli_run_command(
            tmp_path, kernel, initrd, qemu, report, "--disk", "8"
        ),
        cwd=Path(__file__).resolve().parents[1],
        env=_cli_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(marker)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        _kill_marked_child(marker)

    assert proc.returncode == 143
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "Cierre: SIGTERM" in text
    child = int(marker.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_run_sigterm_ignorado_mata_con_sigkill_y_lo_declara(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)
    marker = tmp_path / "qemu.pid"
    qemu = _fake_qemu(
        tmp_path,
        (
            "sleep 0.3\n"
            f"echo $$ > {marker}\n"
            "trap '' TERM\n"
            "i=0; while [ $i -lt 400 ]; do sleep 0.05; i=$((i+1)); done\n"
        ),
    )
    monkeypatch.setattr(cli.vm, "TERM_WAIT_SECONDS", 0.2)
    delivered: list[bool] = []

    def waiter() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if marker.exists():
                os.kill(os.getpid(), signal.SIGTERM)
                delivered.append(True)
                return
            time.sleep(0.02)

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    try:
        code = cli.main(_run_args(kernel, initrd, "--qemu", str(qemu), "--disk", "8"))
    finally:
        thread.join(timeout=1)

    assert delivered == [True]
    assert code == 143
    out = capsys.readouterr().out
    assert "Cierre: SIGTERM" in out
    assert "QEMU no atendió el SIGTERM: SIGKILL a los 0.2 s" in out
    child = int(marker.read_text(encoding="utf-8").strip())
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)


def test_run_wipe_se_ejecuta_al_interrumpir(tmp_path):
    kernel, initrd = _boot_files(tmp_path)
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
    report = tmp_path / "informe.txt"
    proc = subprocess.Popen(
        _cli_run_command(
            tmp_path,
            kernel,
            initrd,
            qemu,
            report,
            "--disk",
            "8",
            "--wipe",
        ),
        cwd=Path(__file__).resolve().parents[1],
        env=_cli_env(tmp_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(marker)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        _kill_marked_child(marker)

    assert proc.returncode == 143
    text = report.read_text(encoding="utf-8")
    assert "Cierre: SIGTERM" in text
    assert "Wipe: 8 MiB a cero en" in text


def test_run_sigterm_durante_el_wipe_conserva_informe_y_sesion(tmp_path):
    """Una señal en el teardown no aborta el wipe, el informe ni la sesión."""
    kernel, initrd = _boot_files(tmp_path)
    marker = tmp_path / "wipe.started"
    report = tmp_path / "informe.txt"
    wrapper = tmp_path / "cli_wipe_lento.py"
    # El CLI corre en un subproceso: el wipe lento se inyecta en un envoltorio.
    wrapper.write_text(
        "import sys, time\n"
        "from vvm import cli, images\n"
        "real = images.wipe_fd\n"
        "def slow(fd):\n"
        f"    open({str(marker)!r}, 'w').close()\n"
        "    time.sleep(0.5)\n"
        "    return real(fd)\n"
        "images.wipe_fd = slow\n"
        "raise SystemExit(cli.main(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    env = _cli_env(tmp_path)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    proc = subprocess.Popen(
        [
            sys.executable,
            str(wrapper),
            "run",
            "--kernel",
            str(kernel),
            "--initrd",
            str(initrd),
            "--qemu",
            "/bin/true",
            "--force",
            "--disk",
            "8",
            "--wipe",
            "--report",
            str(report),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for(marker)
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        _kill_marked_child(marker)

    assert proc.returncode == 143
    assert report.is_file()
    text = report.read_text(encoding="utf-8")
    assert "Cierre: SIGTERM" in text
    assert "Wipe: 8 MiB a cero en" in text
    session = audit.load_session_full(tmp_path / "state" / "vvm" / "last-session.json")
    assert session.extras.closure == "SIGTERM"
    assert session.extras.wipe is not None
    assert session.extras.wipe.disk_mb == 8


def test_run_wipe_declara_el_tamano_medido_del_medio(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)
    monkeypatch.setattr(
        cli.images,
        "wipe_fd",
        lambda fd: images.WipeResult(size_mb=7, seconds=0.01),
    )

    code = cli.main(
        _run_args(kernel, initrd, "--qemu", "/bin/true", "--disk", "8", "--wipe")
    )

    assert code == 0
    assert "Wipe: 7 MiB a cero en 0.010 s" in capsys.readouterr().out
    saved = audit.load_session_full(session)
    assert saved.extras.wipe == audit.WipeInfo(True, 7, 0.01)


def test_run_sigterm_durante_la_copia_no_lanza_qemu(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)

    def interrupted(path, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        raise InterruptedError(f"Se interrumpió la copia de la imagen ({path})")

    def boom(*args, **kwargs):
        raise AssertionError("no debe lanzarse QEMU tras una copia interrumpida")

    monkeypatch.setattr(cli.images, "create_image_fd", interrupted)
    monkeypatch.setattr(cli.vm, "run_session", boom)

    assert cli.main(["run", "--image", str(iso), "--disk", "8"]) == 143

    assert "Interrumpido:" in capsys.readouterr().err


def test_run_sigterm_tras_copiar_el_iso_cierra_sin_lanzar_qemu(
    host_ok, monkeypatch, tmp_path, capsys
):
    iso = _image(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)
    real_copy = images.create_image_fd

    def copy_then_interrupt(path, **kwargs):
        fd = real_copy(path)
        os.kill(os.getpid(), signal.SIGTERM)
        return fd

    def boom(*args, **kwargs):
        raise AssertionError("no debe lanzarse QEMU si ya llegó la señal")

    monkeypatch.setattr(cli.images, "create_image_fd", copy_then_interrupt)
    monkeypatch.setattr(cli.vm, "run_session", boom)

    assert cli.main(["run", "--image", str(iso), "--disk", "8"]) == 143

    out = capsys.readouterr().out
    assert "Cierre: SIGTERM" in out
    assert "Wipe: no solicitado" in out
    saved = audit.load_session_full(session)
    assert saved.extras.closure == "SIGTERM"
    assert saved.extras.image_mode is True


def test_run_wipe_fallido_sale_1_y_es_hallazgo(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    def failing(fd):
        raise OSError("EIO simulado")

    monkeypatch.setattr(cli.images, "wipe_fd", failing)

    code = cli.main(
        _run_args(kernel, initrd, "--qemu", "/bin/true", "--disk", "8", "--wipe")
    )

    assert code == 1
    captured = capsys.readouterr()
    assert "Wipe: error: EIO simulado" in captured.out
    assert "HALLAZGOS: borrado del disco falló: EIO simulado" in captured.out
    assert "cierre quedó incompleto" in captured.err
    saved = audit.load_session_full(session)
    assert saved.extras.wipe is not None
    assert saved.extras.wipe.error == "EIO simulado"


def test_run_wipe_fallido_conserva_el_codigo_de_senal(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    _stub_session(monkeypatch, session)

    def failing(fd):
        raise OSError("EIO simulado")

    monkeypatch.setattr(cli.images, "wipe_fd", failing)
    monkeypatch.setattr(
        cli.vm,
        "run_session",
        lambda cmd, pass_fds=(), **kwargs: cli.vm.RunOutcome(
            returncode=143, interrupt=signal.SIGTERM
        ),
    )

    code = cli.main(
        _run_args(kernel, initrd, "--qemu", "/bin/true", "--disk", "8", "--wipe")
    )

    assert code == 143
    captured = capsys.readouterr()
    assert "HALLAZGOS: borrado del disco falló: EIO simulado" in captured.out
    assert "cierre quedó incompleto" not in captured.err


# --- audit ---------------------------------------------------------------


def test_audit_sin_sesion_sale_1(tmp_path, capsys):
    missing = tmp_path / "no-existe.json"

    assert cli.main(["audit", "--session", str(missing)]) == 1

    err = capsys.readouterr().err
    assert "No hay sesión que auditar" in err
    assert err.count(str(missing)) == 1


def test_audit_imprime_sesion_guardada(tmp_path, capsys):
    session = tmp_path / "sesion.json"
    audit.save_session(
        session,
        _snapshot(shm_entries=("bus",)),
        _snapshot(shm_entries=("bus", "vvm-nueva"), taken_at="2026-10-24T10:05:00Z"),
    )

    assert cli.main(["audit", "--session", str(session)]) == 0

    assert "HALLAZGOS: entrada nueva en /dev/shm: vvm-nueva" in capsys.readouterr().out


def test_audit_reconstruye_prechequeo_forzado(tmp_path, capsys):
    session = tmp_path / "sesion.json"
    p3 = host_check.CheckResult(
        id="P3",
        vector="swap",
        status="FAIL",
        blocking=True,
        evidence="swap activa: /swap.img",
        remediation="sudo swapoff -a",
    )
    audit.save_session(session, _snapshot(), _snapshot(), checks=(p3,), forced=True)

    assert cli.main(["audit", "--session", str(session)]) == 0

    out = capsys.readouterr().out
    assert "P3 FALLA (bloqueante) swap: swap activa: /swap.img" in out
    assert (
        "SIN HALLAZGOS EN LO COMPROBADO · sesión forzada con 1 fallo(s) bloqueante(s)"
        in out
    )


def test_audit_reproduce_el_informe_byte_a_byte(
    host_ok, monkeypatch, tmp_path, capsys
):
    kernel, initrd = _boot_files(tmp_path)
    session = tmp_path / "sesion.json"
    report = tmp_path / "informe.txt"
    _stub_session(monkeypatch, session)

    code = cli.main(
        _run_args(kernel, initrd, "--qemu", "/bin/true", "--report", str(report))
    )
    assert code == 0
    capsys.readouterr()

    assert cli.main(["audit", "--session", str(session)]) == 0

    texto = report.read_text(encoding="utf-8")
    assert capsys.readouterr().out == texto
    assert texto.endswith("\n") and not texto.endswith("\n\n")


def test_audit_reconstruye_cierre_io_y_wipe(tmp_path, capsys):
    session = tmp_path / "sesion.json"
    extras = audit.SessionExtras(
        closure="SIGTERM",
        killed_after=5.0,
        wipe=audit.WipeInfo(requested=True, disk_mb=8, seconds=0.01),
        image_mode=True,
        disk_mb=256,
        process_io=ProcessIO(rchar=10, wchar=20, read_bytes=30, write_bytes=40),
    )
    audit.save_session(session, _snapshot(), _snapshot(), extras=extras)

    assert cli.main(["audit", "--session", str(session)]) == 0

    out = capsys.readouterr().out
    assert "Cierre: SIGTERM" in out
    assert "SIGKILL a los 5 s" in out
    assert "write_bytes: 40 B" in out
    assert "I/O con GUI y disco de datos: 40 B" in out
    assert "Wipe: 8 MiB a cero en 0.010 s" in out


def test_audit_usa_la_sesion_por_defecto(monkeypatch, tmp_path, capsys):
    session = tmp_path / "ultima.json"
    audit.save_session(session, _snapshot(), _snapshot(taken_at="2026-10-24T10:05:00Z"))
    monkeypatch.setattr(cli.audit, "default_session_path", lambda: session)

    assert cli.main(["audit"]) == 0

    assert "SIN HALLAZGOS EN LO COMPROBADO" in capsys.readouterr().out
