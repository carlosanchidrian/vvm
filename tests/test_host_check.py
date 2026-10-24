"""Tests de P1-P10 sin KVM, sin QEMU y sin tocar `/dev/shm`.

Todos los lectores del sistema se sustituyen con monkeypatch: aquí solo se
prueba la decisión (estado, bloqueo y evidencia), no el anfitrión real.
"""

from types import SimpleNamespace

import pytest

from vvm import host_check
from vvm.host_check import check_host, has_blocking

GOOD_CMD = [
    "qemu-system-x86_64",
    "-accel",
    "kvm",
    "-m",
    "512M",
    "-object",
    "memory-backend-memfd,id=mem,size=512M,share=off",
    "-machine",
    "q35,memory-backend=mem,dump-guest-core=off,mem-merge=off",
    "-drive",
    "file=/dev/fd/7,format=raw,if=virtio,cache=writethrough",
]

EMPTY_SWAPS = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
SWAP_ACTIVE = EMPTY_SWAPS + "/swap.img                               file\t\t8388604\t\t0\t\t-2\n"

# Valores por defecto: un anfitrión apto en todo salvo lo que el test cambie.
DEFAULTS = {
    "kvm_accessible": lambda: True,
    "read_meminfo": lambda: {"MemAvailable": 4 * 1024 * 1024},
    "read_swaps": lambda: EMPTY_SWAPS,
    "read_power_state": lambda: "freeze mem\n",
    "read_core_pattern": lambda: "core\n",
    "read_ksm_run": lambda: "0\n",
    "read_shmem_enabled": lambda: "always [madvise] never\n",
    "list_shm_entries": lambda: ["bus"],
}


@pytest.fixture
def readers(monkeypatch):
    """Anfitrión apto por defecto; cada test sobrescribe lo que le interesa."""
    for name, value in DEFAULTS.items():
        monkeypatch.setattr(host_check, name, value)
    return monkeypatch


def result_of(results, check_id):
    return next(result for result in results if result.id == check_id)


def run_check(readers, mem_mb=512, disk_mb=256, cmd=GOOD_CMD):
    return check_host(mem_mb=mem_mb, disk_mb=disk_mb, cmd=cmd)


def test_order_and_length(readers):
    results = run_check(readers)

    assert [result.id for result in results] == [f"P{n}" for n in range(1, 11)]
    assert len(results) == 10


def test_blocking_flags_match_table(readers):
    results = run_check(readers)

    blocking = {result.id for result in results if result.blocking}
    assert blocking == {"P1", "P2", "P3", "P7", "P8", "P9"}


def test_all_ok_when_host_is_apt(readers):
    results = run_check(readers)

    assert [result.status for result in results] == ["OK"] * 10
    assert has_blocking(results) is False


def test_p1_without_kvm(readers):
    readers.setattr(host_check, "kvm_accessible", lambda: False)

    p1 = result_of(run_check(readers), "P1")

    assert p1.status == "FAIL"
    assert p1.blocking is True
    assert "/dev/kvm" in p1.evidence


def test_p2_low_mem_available(readers):
    readers.setattr(host_check, "read_meminfo", lambda: {"MemAvailable": 100 * 1024})

    p2 = result_of(run_check(readers), "P2")

    assert p2.status == "FAIL"
    assert "MemAvailable=102400 kB" in p2.evidence
    assert "necesario=1280 MB" in p2.evidence


def test_p2_ok_at_exact_threshold(readers):
    # 512 + 256 + 512 = 1280 MB justos.
    readers.setattr(host_check, "read_meminfo", lambda: {"MemAvailable": 1280 * 1024})

    p2 = result_of(run_check(readers), "P2")

    assert p2.status == "OK"
    assert "MemAvailable=1310720 kB" in p2.evidence


def test_p2_unreadable_meminfo_fails_blocking(readers):
    def boom():
        raise OSError("no such file or directory: /proc/meminfo")

    readers.setattr(host_check, "read_meminfo", boom)

    p2 = result_of(run_check(readers), "P2")

    assert p2.status == "FAIL"
    assert p2.blocking is True
    assert "no such file or directory" in p2.evidence


def test_p3_with_swap_fails_blocking(readers):
    readers.setattr(host_check, "read_swaps", lambda: SWAP_ACTIVE)

    results = run_check(readers)
    p3 = result_of(results, "P3")

    assert p3.status == "FAIL"
    assert p3.blocking is True
    assert "/swap.img" in p3.evidence
    assert has_blocking(results) is True


def test_p3_without_swap_is_ok(readers):
    p3 = result_of(run_check(readers), "P3")

    assert p3.status == "OK"
    assert p3.blocking is True


def test_p4_hibernation_warns(readers):
    readers.setattr(host_check, "read_power_state", lambda: "freeze mem disk\n")

    p4 = result_of(run_check(readers), "P4")

    assert p4.status == "WARN"
    assert p4.blocking is False
    assert "disk" in p4.evidence


def test_p4_without_hibernation_is_ok(readers):
    readers.setattr(host_check, "read_power_state", lambda: "freeze mem\n")

    assert result_of(run_check(readers), "P4").status == "OK"


def test_p4_unreadable_power_state_warns(readers):
    def boom():
        raise OSError("permiso denegado: /sys/power/state")

    readers.setattr(host_check, "read_power_state", boom)

    p4 = result_of(run_check(readers), "P4")

    assert p4.status == "WARN"
    assert p4.blocking is False


def test_p5_coredump_warns(readers):
    pattern = "|/usr/lib/systemd/systemd-coredump %P %u %g %s %t %c %h\n"
    readers.setattr(host_check, "read_core_pattern", lambda: pattern)

    p5 = result_of(run_check(readers), "P5")

    assert p5.status == "WARN"
    assert p5.blocking is False
    assert "coredump" in p5.evidence


def test_p6_ksm_enabled_warns(readers):
    readers.setattr(host_check, "read_ksm_run", lambda: "1\n")

    p6 = result_of(run_check(readers), "P6")

    assert p6.status == "WARN"
    assert p6.blocking is False
    assert "1" in p6.evidence


def test_p6_ksm_disabled_is_ok(readers):
    readers.setattr(host_check, "read_ksm_run", lambda: "0\n")

    assert result_of(run_check(readers), "P6").status == "OK"


def test_p7_good_cmd_is_ok(readers):
    p7 = result_of(run_check(readers), "P7")

    assert p7.status == "OK"
    assert "dump-guest-core=off" in p7.evidence


def test_p7_cmd_without_dump_guest_core_fails(readers):
    cmd = [token for token in GOOD_CMD if "dump-guest-core=off" not in token]

    p7 = result_of(run_check(readers, cmd=cmd), "P7")

    assert p7.status == "FAIL"
    assert p7.blocking is True
    assert "dump-guest-core=off" in p7.evidence


def test_p8_no_exige_espacio_y_falla_si_no_puede_listar(readers):
    # El disco del guest va en memfd: con 1 MB libre en /dev/shm debe dar OK.
    readers.setattr(
        host_check.os,
        "statvfs",
        lambda path: SimpleNamespace(f_bavail=1, f_frsize=1024 * 1024),
    )
    readers.setattr(host_check, "list_shm_entries", lambda: ["bus"])

    results = run_check(readers)
    p8 = result_of(results, "P8")

    assert p8.status == "OK"
    assert p8.blocking is True
    assert "sin restos vvm-*" in p8.evidence
    assert has_blocking(results) is False

    def denied():
        raise OSError("permiso denegado")

    readers.setattr(host_check, "list_shm_entries", denied)
    p8 = result_of(run_check(readers), "P8")

    assert p8.status == "FAIL"
    assert p8.blocking is True
    assert "permiso denegado" in p8.evidence


def test_p8_leftovers_only_warn(readers):
    readers.setattr(host_check, "list_shm_entries", lambda: ["bus", "vvm-viejo"])

    results = run_check(readers)
    p8 = result_of(results, "P8")

    assert p8.status == "WARN"
    assert p8.blocking is True
    assert "vvm-viejo" in p8.evidence
    assert has_blocking(results) is False


def test_p8_without_leftovers_is_ok(readers):
    readers.setattr(host_check, "list_shm_entries", lambda: ["bus"])

    p8 = result_of(run_check(readers), "P8")

    assert p8.status == "OK"
    assert "sin restos vvm-*" in p8.evidence


def test_p9_good_cmd_is_ok(readers):
    p9 = result_of(run_check(readers), "P9")

    assert p9.status == "OK"
    assert "share=off" in p9.evidence
    assert "file=/dev/fd/7" in p9.evidence


def test_p9_cmd_without_share_off_fails(readers):
    cmd = [token.replace(",share=off", "") for token in GOOD_CMD]

    p9 = result_of(run_check(readers, cmd=cmd), "P9")

    assert p9.status == "FAIL"
    assert p9.blocking is True
    assert "share=off: ausente" in p9.evidence


def test_p9_cmd_without_fd_disk_fails(readers):
    cmd = [token.replace("file=/dev/fd/7", "file=/tmp/disco.img") for token in GOOD_CMD]

    p9 = result_of(run_check(readers, cmd=cmd), "P9")

    assert p9.status == "FAIL"
    assert "disco por fd: ausente" in p9.evidence


def test_p10_always_warns(readers):
    readers.setattr(host_check, "read_shmem_enabled", lambda: "[always] madvise never\n")

    p10 = result_of(run_check(readers), "P10")

    assert p10.status == "WARN"
    assert p10.blocking is False
    assert "always" in p10.evidence


def test_p10_force_warns(readers):
    readers.setattr(
        host_check,
        "read_shmem_enabled",
        lambda: "[force] always within_size advise never deny\n",
    )

    p10 = result_of(run_check(readers), "P10")

    assert p10.status == "WARN"
    assert p10.blocking is False
    assert "force" in p10.evidence


def test_p10_bracketed_never_is_ok(readers):
    readers.setattr(host_check, "read_shmem_enabled", lambda: "[never] always madvise\n")

    p10 = result_of(run_check(readers), "P10")

    assert p10.status == "OK"


def test_p10_plain_value_is_ok(readers):
    readers.setattr(host_check, "read_shmem_enabled", lambda: "never\n")

    assert result_of(run_check(readers), "P10").status == "OK"


def test_has_blocking_ignores_warnings(readers):
    readers.setattr(host_check, "read_power_state", lambda: "disk\n")
    results = run_check(readers)

    assert result_of(results, "P4").status == "WARN"
    assert has_blocking(results) is False


def test_has_blocking_detects_failure(readers):
    readers.setattr(host_check, "kvm_accessible", lambda: False)

    assert has_blocking(run_check(readers)) is True
