"""Comprobaciones P1-P10 del anfitrión (SPEC §4).

Contrato: `check_host()` devuelve una lista de `CheckResult` en el orden P1..P10.
Los lectores de `/proc`, `/sys` y `/dev` son funciones de módulo para poder
sustituirlos en los tests con monkeypatch.

Este módulo no ejecuta nada del sistema: solo lee y decide.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

Status = Literal["OK", "WARN", "FAIL"]

KVM_PATH = "/dev/kvm"
SHM_DIR = "/dev/shm"
MEMINFO_PATH = "/proc/meminfo"
SWAPS_PATH = "/proc/swaps"
POWER_STATE_PATH = "/sys/power/state"
CORE_PATTERN_PATH = "/proc/sys/kernel/core_pattern"
KSM_RUN_PATH = "/sys/kernel/mm/ksm/run"
SHMEM_ENABLED_PATH = "/sys/kernel/mm/transparent_hugepage/shmem_enabled"

# Margen de MB que P2 suma a --mem y --disk antes de comparar con MemAvailable.
RAM_MARGIN_MB = 512


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


# --- Lectores (sustituibles en los tests) ---------------------------------


def _read_text(path: str) -> str:
    """Lee un fichero de texto del sistema y devuelve su contenido crudo."""
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def read_meminfo() -> dict[str, int]:
    """`/proc/meminfo` como diccionario clave -> valor en kB."""
    info: dict[str, int] = {}
    for line in _read_text(MEMINFO_PATH).splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        fields = rest.split()
        if fields and fields[0].isdigit():
            info[key.strip()] = int(fields[0])
    return info


def read_swaps() -> str:
    """Contenido crudo de `/proc/swaps`."""
    return _read_text(SWAPS_PATH)


def read_power_state() -> str:
    """Contenido crudo de `/sys/power/state`."""
    return _read_text(POWER_STATE_PATH)


def read_core_pattern() -> str:
    """Contenido crudo de `/proc/sys/kernel/core_pattern`."""
    return _read_text(CORE_PATTERN_PATH)


def read_ksm_run() -> str:
    """Contenido crudo de `/sys/kernel/mm/ksm/run`."""
    return _read_text(KSM_RUN_PATH)


def read_shmem_enabled() -> str:
    """Contenido crudo de `.../transparent_hugepage/shmem_enabled`."""
    return _read_text(SHMEM_ENABLED_PATH)


def kvm_accessible() -> bool:
    """True si `/dev/kvm` existe y se puede leer y escribir."""
    try:
        return os.access(KVM_PATH, os.R_OK | os.W_OK)
    except OSError:
        return False


def shm_free_bytes() -> int:
    """Bytes libres para usuarios no privilegiados en `/dev/shm`."""
    stats = os.statvfs(SHM_DIR)
    return stats.f_bavail * stats.f_frsize


def list_shm_entries() -> list[str]:
    """Nombres de las entradas de `/dev/shm`. Solo se listan: no se borra nada."""
    return sorted(os.listdir(SHM_DIR))


# --- Especificación de cada comprobación ----------------------------------


@dataclass(frozen=True)
class _Spec:
    """Metadatos fijos de una comprobación (los de la tabla de SPEC §4)."""

    id: str
    vector: str
    blocking: bool
    remediation: str


_P1 = _Spec(
    "P1",
    "Aceleración",
    True,
    'Instala qemu-kvm y añade tu usuario al grupo kvm: sudo apt install qemu-kvm && sudo usermod -aG kvm "$USER"',
)
_P2 = _Spec("P2", "RAM", True, "Cierra aplicaciones o reduce --mem y --disk.")
_P3 = _Spec("P3", "swap", True, "Desactiva la swap: sudo swapoff -a.")
_P4 = _Spec("P4", "Hibernación", False, "Enmascara la hibernación: sudo systemctl mask hibernate.target.")
_P5 = _Spec(
    "P5",
    "Core dumps",
    False,
    "El lanzador ya fija RLIMIT_CORE=0; si persiste, elimina coredump de /proc/sys/kernel/core_pattern.",
)
_P6 = _Spec(
    "P6",
    "KSM",
    False,
    "El lanzador pasa mem-merge=off; si persiste: echo 0 | sudo tee /sys/kernel/mm/ksm/run",
)
_P7 = _Spec("P7", "Volcado de RAM del guest", True, "No arranques sin dump-guest-core=off en -machine.")
_P8 = _Spec(
    "P8",
    "/dev/shm",
    True,
    f"Libera {SHM_DIR}: cierra procesos que lo usen y elimina los restos vvm-* (vvm no los borra).",
)
_P9 = _Spec(
    "P9",
    "Anonimato",
    True,
    "No arranques sin share=off en el backend memfd ni sin el disco por fd (file=/dev/fd/N).",
)
_P10 = _Spec(
    "P10",
    "THP en shmem",
    False,
    "Desactiva THP en shmem: echo never | sudo tee /sys/kernel/mm/transparent_hugepage/shmem_enabled",
)


def _result(spec: _Spec, status: Status, evidence: str) -> CheckResult:
    """Construye el resultado de una comprobación a partir de su tabla fija."""
    return CheckResult(
        id=spec.id,
        vector=spec.vector,
        status=status,
        blocking=spec.blocking,
        evidence=evidence,
        remediation=spec.remediation,
    )


def _find_token(cmd: Sequence[str], needle: str) -> str | None:
    """Primer token de `cmd` que contiene `needle`, o None."""
    for token in cmd:
        if needle in token:
            return token
    return None


_FD_DISK = re.compile(r"file=/dev/fd/\d+")


def _find_fd_disk(cmd: Sequence[str]) -> str | None:
    """Token que pasa el disco como descriptor heredado (`file=/dev/fd/N`)."""
    for token in cmd:
        if _FD_DISK.search(token):
            return token
    return None


def _shmem_thp_value(raw: str) -> str:
    """Valor activo de shmem_enabled: el que va entre corchetes si los hay."""
    match = re.search(r"\[([^\]]+)\]", raw)
    if match:
        return match.group(1).strip()
    fields = raw.split()
    return fields[0] if fields else ""


# --- Comprobaciones P1-P10 -------------------------------------------------


def _check_p1() -> CheckResult:
    if kvm_accessible():
        return _result(_P1, "OK", f"{KVM_PATH} accesible (lectura y escritura)")
    return _result(
        _P1,
        "FAIL",
        f"{KVM_PATH} no accesible: no existe o falta permiso de lectura/escritura",
    )


def _check_p2(mem_mb: int, disk_mb: int) -> CheckResult:
    meminfo = read_meminfo()
    needed_mb = mem_mb + disk_mb + RAM_MARGIN_MB
    available_kb = meminfo.get("MemAvailable")
    if available_kb is None:
        return _result(
            _P2,
            "FAIL",
            f"MemAvailable ausente en {MEMINFO_PATH}: {meminfo or 'vacío'}",
        )
    evidence = (
        f"MemAvailable={available_kb} kB ({available_kb // 1024} MB); "
        f"necesario={needed_mb} MB ({mem_mb} + {disk_mb} + {RAM_MARGIN_MB})"
    )
    status: Status = "OK" if available_kb >= needed_mb * 1024 else "FAIL"
    return _result(_P2, status, evidence)


def _check_p3() -> CheckResult:
    raw = read_swaps()
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) > 1:
        return _result(_P3, "FAIL", f"swap activa: {lines[1]}")
    if lines:
        return _result(_P3, "OK", f"sin swap activa: {lines[0]}")
    return _result(_P3, "OK", f"{SWAPS_PATH} vacío: sin swap activa")


def _check_p4() -> CheckResult:
    raw = read_power_state().strip()
    if "disk" in raw.split():
        return _result(_P4, "WARN", raw)
    return _result(_P4, "OK", raw or f"{POWER_STATE_PATH} vacío")


def _check_p5() -> CheckResult:
    raw = read_core_pattern().strip()
    if "coredump" in raw:
        return _result(_P5, "WARN", raw)
    return _result(_P5, "OK", raw)


def _check_p6() -> CheckResult:
    raw = read_ksm_run().strip()
    if raw == "1":
        return _result(_P6, "WARN", f"ksm/run={raw}: KSM activo")
    return _result(_P6, "OK", f"ksm/run={raw or 'vacío'}")


def _check_p7(cmd: Sequence[str]) -> CheckResult:
    token = _find_token(cmd, "dump-guest-core=off")
    if token is not None:
        return _result(_P7, "OK", token)
    return _result(_P7, "FAIL", "sin dump-guest-core=off en la línea: " + " ".join(cmd))


def _check_p8(disk_mb: int) -> CheckResult:
    needed = disk_mb * 1024 * 1024
    free = shm_free_bytes()
    evidence = f"{SHM_DIR} libre={free} B; necesario={needed} B ({disk_mb} MB)"
    if free < needed:
        return _result(_P8, "FAIL", evidence)
    try:
        leftovers = [name for name in list_shm_entries() if name.startswith("vvm-")]
    except OSError as exc:
        return _result(_P8, "WARN", f"{evidence}; no se pudo listar {SHM_DIR}: {exc}")
    if leftovers:
        return _result(_P8, "WARN", f"{evidence}; restos vvm-*: {', '.join(leftovers)}")
    return _result(_P8, "OK", f"{evidence}; sin restos vvm-*")


def _check_p9(cmd: Sequence[str]) -> CheckResult:
    share = _find_token(cmd, "share=off")
    fd_disk = _find_fd_disk(cmd)
    evidence = (
        f"share=off: {share or 'ausente'}; "
        f"disco por fd: {fd_disk or 'ausente'}"
    )
    status: Status = "OK" if share is not None and fd_disk is not None else "FAIL"
    return _result(_P9, status, evidence)


def _check_p10() -> CheckResult:
    raw = read_shmem_enabled().strip()
    value = _shmem_thp_value(raw) or "desconocido"
    evidence = f"shmem_enabled={raw or 'vacío'}; valor activo={value}"
    status: Status = "WARN" if value == "always" else "OK"
    return _result(_P10, status, evidence)


def check_host(mem_mb: int, disk_mb: int, cmd: Sequence[str]) -> list[CheckResult]:
    """Ejecuta P1-P10 y devuelve los resultados.

    `cmd` es la línea de QEMU ya construida; P7 y P9 la inspeccionan para
    comprobar `dump-guest-core=off` y `share=off` + disco por fd.

    Si un lector falla (fichero ausente o sin permiso), la comprobación queda en
    FALLA si es bloqueante y en aviso si no, citando la causa cruda.
    """
    checks: list[tuple[_Spec, Callable[[], CheckResult]]] = [
        (_P1, _check_p1),
        (_P2, lambda: _check_p2(mem_mb, disk_mb)),
        (_P3, _check_p3),
        (_P4, _check_p4),
        (_P5, _check_p5),
        (_P6, _check_p6),
        (_P7, lambda: _check_p7(cmd)),
        (_P8, lambda: _check_p8(disk_mb)),
        (_P9, lambda: _check_p9(cmd)),
        (_P10, _check_p10),
    ]
    results: list[CheckResult] = []
    for spec, check in checks:
        try:
            results.append(check())
        except OSError as exc:
            status: Status = "FAIL" if spec.blocking else "WARN"
            results.append(_result(spec, status, f"no se pudo leer la medida: {exc}"))
    return results


def has_blocking(results: Sequence[CheckResult]) -> bool:
    """True si alguna comprobación es FALLA y bloqueante."""
    return any(result.status == "FAIL" and result.blocking for result in results)
