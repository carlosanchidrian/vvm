"""Memfds de PipeWire como pila del anfitrión (sin tocar tests/test_audit.py)."""

from __future__ import annotations

from pathlib import Path

from vvm import audit

PIPEWIRE_MEMFD = (
    "memfd:pipewire-memfd:flags=0x0000000f,type=2,size=65664"
)


def _snapshot(memfds: tuple[str, ...]) -> audit.Snapshot:
    return audit.Snapshot(
        shmem_kb=100_000,
        mem_available_kb=4_000_000,
        shm_entries=(),
        qemu_pids=(),
        taken_at="2026-10-24T10:00:00Z",
        memfds=memfds,
    )


def _fake_memfd(proc: Path, pid: str, fd: str, target: str) -> None:
    fd_dir = proc / pid / "fd"
    fd_dir.mkdir(parents=True, exist_ok=True)
    (fd_dir / fd).symlink_to(target)


def test_pipewire_es_pila_del_anfitrion_por_prefijo(tmp_path: Path):
    _fake_memfd(tmp_path, "100", "3", "/" + PIPEWIRE_MEMFD)

    assert audit.list_memfd_names(tmp_path) == (PIPEWIRE_MEMFD,)
    assert audit.is_host_stack_memfd(PIPEWIRE_MEMFD) is True


def test_pipewire_no_es_hallazgo_en_el_informe():
    report = audit.build_report(
        _snapshot(()),
        _snapshot((PIPEWIRE_MEMFD,)),
    )

    assert "1. Entradas de /dev/shm y memfds" in report
    assert (
        "Memfds nuevos de la pila del anfitrión (no del guest): "
        + PIPEWIRE_MEMFD
    ) in report
    assert "Memfds nuevos fuera de esa pila: ninguno" in report
    assert report.rstrip("\n").splitlines()[-1] == (
        "SIN HALLAZGOS EN LO COMPROBADO"
    )


def test_un_memfd_pipewire_con_otro_sufijo_tambien_es_del_anfitrion():
    assert audit.is_host_stack_memfd("memfd:pipewire-memfd:otro") is True
    assert audit.is_host_stack_memfd("memfd:pipewire-otro") is True
    assert audit.is_host_stack_memfd("memfd:pipewire") is False
