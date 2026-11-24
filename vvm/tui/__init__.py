"""Paquete de la TUI de VVM.

No importa Textual al cargarse: el extra `tui` es opcional y el CLI debe seguir
funcionando sin él. `from vvm.tui import VvmApp` resuelve la app de forma
perezosa, solo cuando se pide explícitamente.
"""

from __future__ import annotations

from typing import Any

__all__ = ["VvmApp", "VvmTUI", "VvmTui"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from .app import VvmApp

        return VvmApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
