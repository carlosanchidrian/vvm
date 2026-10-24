"""CLI de VVM: `check` | `run` | `audit` (SPEC §4-§6).

`main()` es la única puerta de entrada: devuelve el código de salida y no
llama a `sys.exit()` (eso lo hace `__main__.py`).
"""

from __future__ import annotations

from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Parsea `argv` y despacha el subcomando. Devuelve el código de salida."""
    raise NotImplementedError("contrato sin implementar: tramo 4")
