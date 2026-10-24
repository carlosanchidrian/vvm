"""Comprobaciones P1-P10 del anfitrión (SPEC §4).

Contrato: `check_host()` devuelve una lista de `CheckResult` en el orden P1..P10.
Los lectores de `/proc`, `/sys` y `/dev` son funciones de módulo para poder
sustituirlos en los tests con monkeypatch.

Este módulo no ejecuta nada del sistema: solo lee y decide.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Status = Literal["OK", "WARN", "FAIL"]


@dataclass(frozen=True)
class CheckResult:
    """Resultado de una comprobación del anfitrión.

    `evidence` lleva la medida o salida cruda que justifica el estado;
    `remediation` indica qué debe hacer el usuario si no es OK.
    """

    id: str
    vector: str
    status: Status
    blocking: bool
    evidence: str
    remediation: str


def check_host(mem_mb: int, disk_mb: int, cmd: Sequence[str]) -> list[CheckResult]:
    """Ejecuta P1-P10 y devuelve los resultados.

    `cmd` es la línea de QEMU ya construida; P7 y P9 la inspeccionan para
    comprobar `dump-guest-core=off` y `share=off` + disco por fd.
    """
    raise NotImplementedError("contrato sin implementar: tramo 1")


def has_blocking(results: Sequence[CheckResult]) -> bool:
    """True si alguna comprobación es FALLA y bloqueante."""
    raise NotImplementedError("contrato sin implementar: tramo 1")
