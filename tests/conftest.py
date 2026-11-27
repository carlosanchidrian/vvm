"""Aislamiento común: ningún test escribe en el runtime ni el estado reales."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Cada test tiene su propio runtime corto (AF_UNIX) y su estado."""
    runtime = Path(tempfile.mkdtemp(prefix="vvm-rt-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    yield
    shutil.rmtree(runtime, ignore_errors=True)
