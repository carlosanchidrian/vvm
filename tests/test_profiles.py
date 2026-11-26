"""Tests de perfiles TOML y de su integración con el CLI (SPEC tramo 6 §4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vvm import cli, host_check, profiles

SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"


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


def _write_profiles(tmp_path: Path, text: str) -> Path:
    directory = tmp_path / "config" / "vvm"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "profiles.toml"
    path.write_text(text, encoding="utf-8")
    return path


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))


def _image(tmp_path: Path, size: int = 3 * 1024 * 1024) -> Path:
    iso = tmp_path / "live.iso"
    iso.write_bytes(b"ISO" * (size // 3))
    return iso


def _boot(tmp_path: Path) -> tuple[Path, Path]:
    kernel = tmp_path / "vmlinuz"
    initrd = tmp_path / "initramfs"
    kernel.write_bytes(b"kernel")
    initrd.write_bytes(b"initrd")
    return kernel, initrd


def test_lee_perfil_y_resuelve_rutas_relativas_al_toml(tmp_path: Path):
    _boot(tmp_path)
    path = _write_profiles(
        tmp_path,
        """
default = "min"

[profiles.min]
kernel = "vmlinuz"
initrd = "initramfs"
mem_mb = 512
disk_mb = 8
""",
    )

    loaded = profiles.load_profiles(path)

    assert loaded.default == "min"
    profile = loaded.get("min")
    assert profile.source == "kernel"
    assert profile.kernel == str((path.parent / "vmlinuz").resolve())
    assert profile.initrd == str((path.parent / "initramfs").resolve())
    assert profile.mem_mb == 512
    assert profile.disk_mb == 8


def test_clave_desconocida_lista_las_validas_y_el_fichero(tmp_path: Path):
    path = _write_profiles(
        tmp_path,
        """
[profiles.min]
kernel = "vmlinuz"
initrd = "initramfs"
memmb = 512
""",
    )

    with pytest.raises(profiles.ProfilesError) as excinfo:
        profiles.load_profiles(path)

    message = str(excinfo.value)
    assert "memmb" in message
    assert "mem_mb" in message
    assert str(path) in message


def test_image_y_kernel_es_error_del_perfil(tmp_path: Path):
    path = _write_profiles(
        tmp_path,
        """
[profiles.roto]
image = "live.iso"
kernel = "vmlinuz"
initrd = "initramfs"
""",
    )

    with pytest.raises(profiles.ProfilesError, match="excluyentes"):
        profiles.load_profiles(path)


def test_perfil_inexistente_lista_los_disponibles(tmp_path: Path):
    path = _write_profiles(
        tmp_path,
        """
[profiles.kali]
image = "live.iso"
""",
    )
    loaded = profiles.load_profiles(path)

    with pytest.raises(profiles.ProfilesError) as excinfo:
        loaded.get("nope")

    message = str(excinfo.value)
    assert "nope" in message
    assert "kali" in message


def test_fichero_ausente_cita_la_ruta_buscada(tmp_path: Path):
    missing = tmp_path / "no" / "existe" / "profiles.toml"

    with pytest.raises(profiles.ProfilesError) as excinfo:
        profiles.load_profiles(missing)

    assert str(missing) in str(excinfo.value)


def test_default_roto_solo_avisa(tmp_path: Path):
    path = _write_profiles(
        tmp_path,
        """
default = "nope"

[profiles.kali]
image = "live.iso"
""",
    )

    loaded = profiles.load_profiles(path)

    assert loaded.default_profile is None
    assert loaded.default_warning is not None
    assert "nope" in loaded.default_warning


def test_profiles_json_es_estable_y_ordenado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    _write_profiles(
        tmp_path,
        """
[profiles.zeta]
image = "live.iso"
mem_mb = 2048

[profiles.alfa]
image = "live.iso"
""",
    )

    assert cli.main(["profiles", "--json"]) == 0

    raw = capsys.readouterr().out
    data = json.loads(raw)
    assert [item["name"] for item in data] == ["alfa", "zeta"]
    for item in data:
        assert list(item) == sorted(item)
        assert item["source"] == "ISO"
        assert item["network"] == "none"


def test_profile_y_flags_dan_la_misma_linea(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    profile_path = _write_profiles(
        tmp_path,
        """
default = "kali"

[profiles.kali]
image = "live.iso"
mem_mb = 4096
disk_mb = 256
cpus = 2
wipe = true
""",
    )
    iso = profile_path.parent / "live.iso"
    iso.write_bytes(b"ISO" * (3 * 1024 * 1024 // 3))

    assert cli.main(["run", "--profile", "kali", "--dry-run"]) == 0
    profile_line = capsys.readouterr().out

    assert (
        cli.main(
            [
                "run",
                "--image",
                str(iso.resolve()),
                "--mem",
                "4096",
                "--disk",
                "256",
                "--cpus",
                "2",
                "--wipe",
                "--dry-run",
            ]
        )
        == 0
    )
    explicit_line = capsys.readouterr().out

    assert profile_line == explicit_line


def test_flags_sobrescriben_el_perfil(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    profile_path = _write_profiles(
        tmp_path,
        """
[profiles.min]
image = "live.iso"
mem_mb = 4096
disk_mb = 256
cpus = 2
""",
    )
    (profile_path.parent / "live.iso").write_bytes(b"ISO" * 1024)

    assert (
        cli.main(
            [
                "run",
                "--profile",
                "min",
                "--mem",
                "1024",
                "--disk",
                "0",
                "--cpus",
                "1",
                "--dry-run",
            ]
        )
        == 0
    )

    line = capsys.readouterr().out
    assert "size=1024M" in line
    assert "if=virtio" not in line


def test_check_profile_cuenta_el_iso(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, host_ok
):
    _env(monkeypatch, tmp_path)
    profile_path = _write_profiles(
        tmp_path,
        """
[profiles.kali]
image = "live.iso"
mem_mb = 4096
disk_mb = 0
""",
    )
    (profile_path.parent / "live.iso").write_bytes(b"ISO" * 1024)

    assert cli.main(["check", "--profile", "kali", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    p2 = next(item for item in data if item["id"] == "P2")
    assert "imagen" in p2["evidence"]


def test_profile_con_fuente_explicita_sale_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    _write_profiles(
        tmp_path,
        """
[profiles.kali]
image = "live.iso"
""",
    )

    assert cli.main(["run", "--profile", "kali", "--image", "otra.iso"]) == 2

    err = capsys.readouterr().err
    assert "no se combina" in err
    assert "--image" in err


def test_profile_inexistente_en_cli_sale_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    _write_profiles(
        tmp_path,
        """
[profiles.kali]
image = "live.iso"
""",
    )

    assert cli.main(["run", "--profile", "nope", "--dry-run"]) == 2

    err = capsys.readouterr().err
    assert "nope" in err
    assert "kali" in err


def test_profiles_sin_fichero_sale_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "vacio"))

    assert cli.main(["profiles"]) == 2

    err = capsys.readouterr().err
    assert str(tmp_path / "vacio" / "vvm" / "profiles.toml") in err


def test_profiles_con_default_roto_avisa_pero_lista(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    _write_profiles(
        tmp_path,
        """
default = "nope"

[profiles.kali]
image = "live.iso"
""",
    )

    assert cli.main(["profiles"]) == 0

    captured = capsys.readouterr()
    assert "kali" in captured.out
    assert "nope" in captured.err


def test_add_profile_crea_el_fichero_y_marca_default(tmp_path: Path):
    target = tmp_path / "config" / "vvm" / "profiles.toml"
    kernel = str(tmp_path / "vmlinuz")
    initrd = str(tmp_path / "initramfs")
    profile = profiles.Profile(
        name="min",
        kernel=kernel,
        initrd=initrd,
        mem_mb=256,
        disk_mb=0,
        cpus=1,
    )

    saved = profiles.add_profile(target, profile)

    assert target.is_file()
    assert saved.default == "min"
    loaded = profiles.load_profiles(target)
    assert loaded.default == "min"
    assert loaded.get("min").kernel == kernel
    assert loaded.get("min").disk_mb == 0


def test_add_profile_conserva_los_existentes_y_el_default(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
default = "kali"

[profiles.kali]
image = "live.iso"
mem_mb = 2048
""",
    )
    loaded = profiles.load_profiles(target)
    nuevo = profiles.Profile(
        name="min",
        kernel=str(tmp_path / "vmlinuz"),
        initrd=str(tmp_path / "initramfs"),
    )

    saved = profiles.add_profile(target, nuevo)

    assert saved.default == "kali"
    assert set(saved.profiles) == {"kali", "min"}
    reloaded = profiles.load_profiles(target)
    assert reloaded.get("kali").image == str((target.parent / "live.iso").resolve())
    assert reloaded.get("min").kernel == str(tmp_path / "vmlinuz")


def test_add_profile_rechaza_nombre_duplicado_sin_tocar_el_fichero(
    tmp_path: Path,
):
    target = _write_profiles(
        tmp_path,
        """
[profiles.min]
kernel = "/boot/vmlinuz"
initrd = "/boot/initramfs"
""",
    )
    original = target.read_text(encoding="utf-8")

    with pytest.raises(profiles.ProfilesError, match="ya existe"):
        profiles.add_profile(
            target,
            profiles.Profile(
                name="min",
                kernel="/otro",
                initrd="/otro-initrd",
            ),
        )

    assert target.read_text(encoding="utf-8") == original


def test_add_profile_serializa_nombres_con_espacios_y_escapes(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    profile = profiles.Profile(
        name='mi "perfil"',
        image='/tmp/una "iso".iso',
        mem_mb=1024,
        disk_mb=0,
        cpus=2,
        wipe=True,
    )

    profiles.add_profile(target, profile)

    loaded = profiles.load_profiles(target)
    assert loaded.get('mi "perfil"').image == '/tmp/una "iso".iso'
    assert loaded.get('mi "perfil"').wipe is True


def test_add_profile_valida_antes_de_escribir(tmp_path: Path):
    target = tmp_path / "profiles.toml"

    with pytest.raises(ValueError, match="excluyentes"):
        profiles.add_profile(
            target,
            profiles.Profile(
                name="roto",
                image="/tmp/live.iso",
                kernel="/boot/vmlinuz",
                initrd="/boot/initramfs",
            ),
        )

    assert not target.exists()


def test_default_roto_se_conserva_al_anadir(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
default = "nope"

[profiles.viejo]
image = "live.iso"
""",
    )

    saved = profiles.add_profile(
        target,
        profiles.Profile(name="nuevo", kernel="/k", initrd="/i"),
    )

    assert saved.default == "nope"
    reloaded = profiles.load_profiles(target)
    assert reloaded.default == "nope"
    assert reloaded.default_warning is not None


def test_update_profile_conserva_el_resto_y_el_default(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
default = "kali"

[profiles.kali]
image = "live.iso"
mem_mb = 2048

[profiles.min]
kernel = "vmlinuz"
initrd = "initramfs"
""",
    )
    actualizado = profiles.Profile(
        name="kali",
        image=str(tmp_path / "nueva.iso"),
        mem_mb=4096,
        disk_mb=0,
        cpus=2,
        wipe=True,
    )

    saved = profiles.update_profile(target, actualizado)

    assert saved.default == "kali"
    assert set(saved.profiles) == {"kali", "min"}
    reloaded = profiles.load_profiles(target)
    assert reloaded.get("kali").mem_mb == 4096
    assert reloaded.get("kali").wipe is True
    assert reloaded.get("min").kernel == str((target.parent / "vmlinuz").resolve())


def test_update_profile_inexistente_no_toca_el_fichero(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
[profiles.min]
kernel = "vmlinuz"
initrd = "initramfs"
""",
    )
    original = target.read_text(encoding="utf-8")

    with pytest.raises(profiles.ProfilesError, match="no existe"):
        profiles.update_profile(
            target,
            profiles.Profile(name="nope", kernel="/k", initrd="/i"),
        )

    assert target.read_text(encoding="utf-8") == original


def test_delete_profile_no_default_conserva_el_default(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
default = "kali"

[profiles.kali]
image = "live.iso"

[profiles.min]
kernel = "vmlinuz"
initrd = "initramfs"
""",
    )

    saved = profiles.delete_profile(target, "min")

    assert saved.default == "kali"
    assert set(saved.profiles) == {"kali"}
    assert profiles.load_profiles(target).default == "kali"


def test_delete_profile_default_reasigna_al_primero_alfabetico(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
default = "zeta"

[profiles.zeta]
image = "live.iso"

[profiles.beta]
image = "live2.iso"

[profiles.alfa]
image = "live3.iso"
""",
    )

    saved = profiles.delete_profile(target, "zeta")

    assert saved.default == "alfa"
    assert set(saved.profiles) == {"alfa", "beta"}
    assert profiles.load_profiles(target).default == "alfa"


def test_delete_profile_ultimo_deja_el_fichero_sin_default(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
default = "unico"

[profiles.unico]
image = "live.iso"
""",
    )

    saved = profiles.delete_profile(target, "unico")

    assert saved.default is None
    assert saved.profiles == {}
    reloaded = profiles.load_profiles(target)
    assert reloaded.profiles == {}
    assert reloaded.default is None


def test_delete_profile_inexistente_no_toca_el_fichero(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
[profiles.min]
kernel = "vmlinuz"
initrd = "initramfs"
""",
    )
    original = target.read_text(encoding="utf-8")

    with pytest.raises(profiles.ProfilesError, match="no existe"):
        profiles.delete_profile(target, "nope")

    assert target.read_text(encoding="utf-8") == original


def test_add_profile_conserva_comentarios_orden_y_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = tmp_path / "profiles.toml"
    original = (
        "# cabecera del fichero\n"
        "\n"
        "[profiles.kali]\n"
        "# comentario del perfil kali\n"
        'image = "~/isos/x.iso"\n'
        "mem_mb = 2048\n"
        "\n"
        "[profiles.min]\n"
        "# comentario del perfil min\n"
        'kernel = "~/vmlinuz"\n'
        'initrd = "~/initramfs"\n'
    )
    target.write_text(original, encoding="utf-8")

    profiles.add_profile(
        target,
        profiles.Profile(
            name="nuevo",
            kernel=str(home / "otro-kernel"),
            initrd=str(home / "otro-initrd"),
            disk_mb=0,
        ),
    )

    text = target.read_text(encoding="utf-8")
    assert text.startswith(original)
    assert "# cabecera del fichero" in text
    assert "# comentario del perfil min" in text
    assert 'image = "~/isos/x.iso"' in text
    assert 'kernel = "~/otro-kernel"' in text
    assert "default" not in text
    assert profiles.load_profiles(target).default is None


def test_update_profile_solo_cambia_su_bloque_y_conserva_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            "# cabecera\n"
            "\n"
            "[profiles.kali]\n"
            'image = "~/isos/x.iso"\n'
            "mem_mb = 2048\n"
            "\n"
            "[profiles.min]\n"
            "# min intacto\n"
            'kernel = "~/vmlinuz"\n'
            'initrd = "~/initramfs"\n'
        ),
        encoding="utf-8",
    )

    profiles.update_profile(
        target,
        profiles.Profile(
            name="kali",
            image=str(home / "isos" / "x.iso"),
            mem_mb=4096,
            disk_mb=0,
        ),
    )

    text = target.read_text(encoding="utf-8")
    assert "# cabecera" in text
    assert "# min intacto" in text
    assert 'image = "~/isos/x.iso"' in text
    assert "mem_mb = 4096" in text
    assert text.index("[profiles.kali]") < text.index("[profiles.min]")


def test_delete_profile_solo_quita_su_bloque_y_conserva_comentarios(
    tmp_path: Path,
):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            "# cabecera\n"
            "\n"
            "[profiles.kali]\n"
            "# kali intacto\n"
            'image = "~/isos/x.iso"\n'
            "\n"
            "[profiles.min]\n"
            'kernel = "~/vmlinuz"\n'
            'initrd = "~/initramfs"\n'
        ),
        encoding="utf-8",
    )

    profiles.delete_profile(target, "min")

    text = target.read_text(encoding="utf-8")
    assert "# cabecera" in text
    assert "# kali intacto" in text
    assert 'image = "~/isos/x.iso"' in text
    assert "[profiles.min]" not in text


def test_delete_sin_default_no_inventa_uno(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            "# sin default\n"
            "[profiles.a]\n"
            'image = "/tmp/a.iso"\n'
            "\n"
            "[profiles.b]\n"
            'image = "/tmp/b.iso"\n'
        ),
        encoding="utf-8",
    )

    saved = profiles.delete_profile(target, "a")

    text = target.read_text(encoding="utf-8")
    assert "default =" not in text
    assert saved.default is None
    assert profiles.load_profiles(target).default is None


def test_delete_con_default_roto_lo_deja_como_esta(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            'default = "nope"\n'
            "\n"
            "[profiles.a]\n"
            'image = "/tmp/a.iso"\n'
            "\n"
            "[profiles.b]\n"
            'image = "/tmp/b.iso"\n'
        ),
        encoding="utf-8",
    )

    saved = profiles.delete_profile(target, "a")

    text = target.read_text(encoding="utf-8")
    assert 'default = "nope"' in text
    assert saved.default == "nope"
    assert profiles.load_profiles(target).default_warning is not None


def test_load_profiles_rechaza_image_con_serial_log(tmp_path: Path):
    target = _write_profiles(
        tmp_path,
        """
[profiles.roto]
image = "live.iso"
serial_log = "serie.log"
""",
    )

    with pytest.raises(profiles.ProfilesError, match="serial_log"):
        profiles.load_profiles(target)


def test_run_profile_image_con_serial_log_sale_2_sin_lanzar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    _env(monkeypatch, tmp_path)
    _write_profiles(
        tmp_path,
        """
[profiles.roto]
image = "live.iso"
serial_log = "serie.log"
""",
    )

    def boom(*args, **kwargs):
        raise AssertionError("no debe lanzarse nada")

    monkeypatch.setattr(cli.images, "create_disk_fd", boom)
    monkeypatch.setattr(cli.images, "create_image_fd", boom)

    assert cli.main(["run", "--profile", "roto", "--dry-run"]) == 2

    err = capsys.readouterr().err
    assert "serial_log" in err
    assert "excluyentes" in err


def test_update_profile_conserva_el_comentario_del_siguiente(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            'default = "b"\n'
            "\n"
            "# cabecera\n"
            "\n"
            "# === A: kernel ===\n"
            "[profiles.a]\n"
            'kernel = "/a"\n'
            'initrd = "/ai"\n'
            "wipe = false\n"
            "\n"
            "# === B: kernel del PoC, no borrar\n"
            "\n"
            "[profiles.b]\n"
            'kernel = "/b"\n'
            'initrd = "/bi"\n'
            "wipe = false\n"
        ),
        encoding="utf-8",
    )

    profiles.update_profile(
        target,
        profiles.Profile(
            name="a",
            kernel="/a",
            initrd="/ai",
            wipe=True,
        ),
    )

    assert target.read_text(encoding="utf-8") == (
        'default = "b"\n'
        "\n"
        "# cabecera\n"
        "\n"
        "[profiles.a]\n"
        'kernel = "/a"\n'
        'initrd = "/ai"\n'
        "mem_mb = 512\n"
        "disk_mb = 256\n"
        "cpus = 1\n"
        "headless = false\n"
        "wipe = true\n"
        "\n"
        "# === B: kernel del PoC, no borrar\n"
        "\n"
        "[profiles.b]\n"
        'kernel = "/b"\n'
        'initrd = "/bi"\n'
        "wipe = false\n"
    )
    assert profiles.load_profiles(target).get("a").wipe is True
    assert profiles.load_profiles(target).default == "b"


def test_delete_profile_quita_su_comentario_y_conserva_el_siguiente(
    tmp_path: Path,
):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            'default = "b"\n'
            "\n"
            "# cabecera\n"
            "\n"
            "# === A: kernel ===\n"
            "[profiles.a]\n"
            'kernel = "/a"\n'
            'initrd = "/ai"\n'
            "wipe = false\n"
            "\n"
            "# === B: kernel del PoC, no borrar\n"
            "\n"
            "[profiles.b]\n"
            'kernel = "/b"\n'
            'initrd = "/bi"\n'
            "wipe = false\n"
        ),
        encoding="utf-8",
    )

    profiles.delete_profile(target, "a")

    assert target.read_text(encoding="utf-8") == (
        'default = "b"\n'
        "\n"
        "# cabecera\n"
        "\n"
        "# === B: kernel del PoC, no borrar\n"
        "\n"
        "[profiles.b]\n"
        'kernel = "/b"\n'
        'initrd = "/bi"\n'
        "wipe = false\n"
    )
    loaded = profiles.load_profiles(target)
    assert loaded.default == "b"
    assert set(loaded.profiles) == {"b"}


def test_add_profile_desde_cero_deja_una_sola_linea_en_blanco(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    target = tmp_path / "profiles.toml"

    profiles.add_profile(
        target,
        profiles.Profile(
            name="uno",
            image="/tmp/uno.iso",
            mem_mb=512,
            disk_mb=0,
            cpus=1,
        ),
    )

    assert target.read_text(encoding="utf-8") == (
        'default = "uno"\n'
        "\n"
        "[profiles.uno]\n"
        'image = "/tmp/uno.iso"\n'
        "mem_mb = 512\n"
        "disk_mb = 0\n"
        "cpus = 1\n"
        "headless = false\n"
        "wipe = false\n"
    )


def test_add_profile_conserva_finales_de_linea_crlf(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    original = (
        "# cabecera\r\n"
        "\r\n"
        "[profiles.uno]\r\n"
        'image = "/tmp/uno.iso"\r\n'
        "\r\n"
        "[profiles.dos]\r\n"
        'image = "/tmp/dos.iso"\r\n'
    ).encode("utf-8")
    target.write_bytes(original)

    profiles.add_profile(
        target,
        profiles.Profile(name="tres", image="/tmp/tres.iso", disk_mb=0),
    )

    raw = target.read_bytes()
    assert raw.startswith(original)
    assert b'[profiles.tres]\r\nimage = "/tmp/tres.iso"\r\n' in raw
    assert raw.replace(b"\r\n", b"").count(b"\n") == 0
    assert raw.replace(b"\r\n", b"").count(b"\r") == 0
    assert profiles.load_profiles(target).get("tres").disk_mb == 0


def test_update_y_delete_conservan_finales_de_linea_crlf(tmp_path: Path):
    original = (
        'default = "b"\r\n'
        "\r\n"
        "[profiles.a]\r\n"
        'image = "/a.iso"\r\n'
        "\r\n"
        "[profiles.b]\r\n"
        'image = "/b.iso"\r\n'
    ).encode("utf-8")
    update_target = tmp_path / "update.toml"
    update_target.write_bytes(original)
    profiles.update_profile(
        update_target,
        profiles.Profile(name="a", image="/a2.iso"),
    )
    update_raw = update_target.read_bytes()
    assert update_raw.replace(b"\r\n", b"").count(b"\n") == 0
    assert update_raw.replace(b"\r\n", b"").count(b"\r") == 0
    assert profiles.load_profiles(update_target).get("a").image == "/a2.iso"

    delete_target = tmp_path / "delete.toml"
    delete_target.write_bytes(original)
    profiles.delete_profile(delete_target, "a")
    delete_raw = delete_target.read_bytes()
    assert delete_raw.replace(b"\r\n", b"").count(b"\n") == 0
    assert delete_raw.replace(b"\r\n", b"").count(b"\r") == 0
    assert profiles.load_profiles(delete_target).default == "b"
    assert set(profiles.load_profiles(delete_target).profiles) == {"b"}


def test_delete_profile_no_duplica_la_linea_en_blanco(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            'default = "a"\n'
            "\n"
            "[profiles.a]\n"
            'image = "/a.iso"\n'
            "\n"
            "[profiles.b]\n"
            'image = "/b.iso"\n'
            "\n"
            "[profiles.c]\n"
            'image = "/c.iso"\n'
        ),
        encoding="utf-8",
    )

    profiles.delete_profile(target, "b")

    assert target.read_text(encoding="utf-8") == (
        'default = "a"\n'
        "\n"
        "[profiles.a]\n"
        'image = "/a.iso"\n'
        "\n"
        "[profiles.c]\n"
        'image = "/c.iso"\n'
    )


def test_delete_default_entrecomillado_no_duplica_la_clave(tmp_path: Path):
    target = tmp_path / "profiles.toml"
    target.write_text(
        (
            '"default" = "a"\n'
            "\n"
            "[profiles.a]\n"
            'image = "/a.iso"\n'
            "\n"
            "[profiles.b]\n"
            'image = "/b.iso"\n'
        ),
        encoding="utf-8",
    )

    saved = profiles.delete_profile(target, "a")

    text = target.read_text(encoding="utf-8")
    assert saved.default == "b"
    assert text.count("default") == 1
    assert 'default = "b"' in text
    assert profiles.load_profiles(target).default == "b"
