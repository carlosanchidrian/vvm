"""Empaquetado: extra `tui` con pyte y CLI sin extras."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_dependencies_vacio_y_extra_tui_con_pyte():
    document = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert document["project"]["dependencies"] == []
    extra = document["project"]["optional-dependencies"]["tui"]
    assert any(item.startswith("textual") for item in extra)
    assert any(item.startswith("pyte") for item in extra)
    assert document["tool"]["setuptools"]["packages"] == ["vvm", "vvm.tui"]


def test_tui_sin_textual_ni_pyte_sale_2():
    code = r"""
import builtins, io, sys

real_import = builtins.__import__

def blocked(name, *args, **kwargs):
    if name.split(".", 1)[0] in {"textual", "pyte"}:
        raise ImportError(f"bloqueado: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked

class TTY(io.StringIO):
    def isatty(self):
        return True

sys.stdin = TTY()
from vvm import cli
raise SystemExit(cli.main(["tui"]))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "falta el extra de la TUI" in result.stderr
    assert "pip install -e '.[tui]'" in result.stderr


def test_ps_y_profiles_sin_los_extras():
    runtime = tempfile.mkdtemp(prefix="vvm-runtime-")
    state = tempfile.mkdtemp(prefix="vvm-state-")
    code = r"""
import builtins, os, sys, tempfile
from pathlib import Path

real_import = builtins.__import__

def blocked(name, *args, **kwargs):
    if name.split(".", 1)[0] in {"textual", "pyte"}:
        raise ImportError(f"bloqueado: {name}")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked

from vvm import cli

root = Path(tempfile.mkdtemp(prefix="vvm-sin-extra-"))
config = root / "config" / "vvm"
config.mkdir(parents=True)
profiles = config / "profiles.toml"
profiles.write_text(
    '[profiles.min]\nkernel = "/k"\ninitrd = "/i"\nmem_mb = 256\ndisk_mb = 8\n',
    encoding="utf-8",
)
os.environ["XDG_CONFIG_HOME"] = str(root / "config")
assert cli.main(["ps"]) == 0
assert cli.main(["profiles", "--json"]) == 0
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT),
            "XDG_RUNTIME_DIR": runtime,
            "XDG_STATE_HOME": state,
        },
    )

    assert result.returncode == 0, result.stderr
    assert "no hay sesiones vivas" in result.stdout
    assert '"name": "min"' in result.stdout


def test_cli_no_importa_textual_ni_pyte():
    code = (
        "import sys; from vvm import cli; "
        "print('textual' in sys.modules, 'pyte' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False False"
