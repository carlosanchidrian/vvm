"""CLI de VVM: `check` | `run` | `audit` (SPEC §4-§6).

`main()` es la única puerta de entrada: devuelve el código de salida y no llama
a `sys.exit()` (eso lo hace `__main__.py`).

Orden de `run`, deliberado: crear el fd del disco (inocuo) → construir la línea
de QEMU → pre-chequeo. P7 y P9 inspeccionan esa línea, así que el fd y la línea
tienen que existir antes del chequeo. Si algo bloquea, se cierra el fd y se sale.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from vvm import __version__, audit, host_check, images, vm
from vvm.host_check import CheckResult

# `vvm check` no arranca nada: estos nombres solo rellenan la línea que P7 y P9
# inspeccionan, porque no hay kernel ni initrd reales que pasar.
_PLACEHOLDER_KERNEL = "<kernel>"
_PLACEHOLDER_INITRD = "<initrd>"

_STATUS_LABELS = {"OK": "OK", "WARN": "AVISO", "FAIL": "FALLA"}
_STATUS_COLORS = {"OK": "\033[32m", "WARN": "\033[33m", "FAIL": "\033[31m"}
_COLOR_RESET = "\033[0m"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vvm",
        description="VMs volátiles sobre QEMU/KVM: RAM y disco solo en memoria.",
    )
    parser.add_argument("--version", action="version", version=f"vvm {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="{check,run,audit}")

    check = subparsers.add_parser("check", help="pre-chequeo P1-P10 del anfitrión")
    check.add_argument("--mem", type=int, default=512, metavar="MB")
    check.add_argument("--disk", type=int, default=256, metavar="MB")
    check.add_argument("--json", action="store_true", help="salida legible por máquina")

    run = subparsers.add_parser("run", help="arranca una sesión")
    run.add_argument("--kernel", required=True, help="vmlinuz del guest")
    run.add_argument("--initrd", required=True, help="initramfs del guest")
    run.add_argument("--mem", type=int, default=512, metavar="MB")
    run.add_argument("--disk", type=int, default=256, metavar="MB")
    run.add_argument("--cpus", type=int, default=1)
    run.add_argument("--headless", action="store_true")
    run.add_argument("--log", metavar="RUTA", help="log serie (obligatorio con --headless)")
    run.add_argument("--report", metavar="RUTA", help="escribe el informe en un fichero")
    run.add_argument("--qemu", default="qemu-system-x86_64")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="imprime la línea de QEMU y no ejecuta nada",
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="arranca aunque haya fallos bloqueantes",
    )

    audit_parser = subparsers.add_parser("audit", help="informe de la última sesión")
    audit_parser.add_argument(
        "--session",
        metavar="RUTA",
        help="sesión guardada (por defecto, la última de $XDG_STATE_HOME)",
    )
    return parser


def _stdout_supports_color() -> bool:
    """Color solo en terminal interactivo y si `NO_COLOR` no está definido."""
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _format_results(results: Sequence[CheckResult]) -> str:
    """Salida legible del pre-chequeo: nombre, id, estado con color y remedio."""
    color = _stdout_supports_color()
    blocks: list[str] = []
    for result in results:
        label = _STATUS_LABELS[result.status]
        tag = f"[{label}]"
        line = f"{tag:<7} {result.vector}: {result.evidence}"
        if color:
            line = f"{_STATUS_COLORS[result.status]}{line}{_COLOR_RESET}"
        if result.status != "OK":
            line += f"\n   Remedio: {result.remediation}"
        blocks.append(line)

    blocking = [
        result for result in results if result.status == "FAIL" and result.blocking
    ]
    warnings = [result for result in results if result.status == "WARN"]
    if blocking:
        names = ", ".join(result.vector for result in blocking)
        blocks.append(
            f"\nVeredicto: NO apto — {len(blocking)} fallo(s) bloqueante(s): {names}."
        )
    elif warnings:
        blocks.append(f"\nVeredicto: apto para arrancar, con {len(warnings)} aviso(s).")
    else:
        blocks.append("\nVeredicto: apto para arrancar.")
    return "\n".join(blocks)


def cmd_check(args: argparse.Namespace) -> int:
    cmd = vm.build_cmd(
        vm.VmConfig(
            kernel=_PLACEHOLDER_KERNEL,
            initrd=_PLACEHOLDER_INITRD,
            mem_mb=args.mem,
            disk_mb=args.disk,
        ),
        disk_fd=0,
    )
    results = host_check.check_host(args.mem, args.disk, cmd)
    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print(_format_results(results))
    return 1 if host_check.has_blocking(results) else 0


def _emit_report(report: str, report_path: str | None) -> None:
    """Imprime el informe o lo escribe en disco, sin perderlo si falla la escritura."""
    if not report_path:
        print(report)
        return
    try:
        Path(report_path).write_text(report, encoding="utf-8")
    except OSError as exc:
        print(
            f"Error: no se pudo escribir el informe en {report_path}: {exc}",
            file=sys.stderr,
        )
        print(report)
    else:
        print(f"Informe escrito en {report_path}")


def cmd_run(args: argparse.Namespace) -> int:
    try:
        images.validate_boot_files(args.kernel, args.initrd)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    disk_fd: int | None = None
    try:
        # El fd se crea antes de la línea (P9 la inspecciona) y es inocuo:
        # solo reserva memoria, no escribe en disco.
        disk_fd = images.create_disk_fd(args.disk)
        try:
            cmd = vm.build_cmd(
                vm.VmConfig(
                    kernel=args.kernel,
                    initrd=args.initrd,
                    mem_mb=args.mem,
                    disk_mb=args.disk,
                    cpus=args.cpus,
                    headless=args.headless,
                    serial_log=args.log,
                ),
                disk_fd,
                qemu=args.qemu,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        if args.dry_run:
            print(shlex.join(cmd))
            return 0

        results = host_check.check_host(args.mem, args.disk, cmd)
        print(_format_results(results))
        blocking = [
            result
            for result in results
            if result.status == "FAIL" and result.blocking
        ]
        if blocking and not args.force:
            print(
                "No se arranca la VM: hay fallos bloqueantes "
                "(usa --force para ignorarlos).",
                file=sys.stderr,
            )
            return 1
        if blocking and args.force:
            print(
                f"Arrancando con {len(blocking)} fallo(s) bloqueante(s) "
                "ignorados por --force",
                file=sys.stderr,
            )

        before = audit.take_snapshot()
        exit_code = 0
        try:
            exit_code = vm.run_vm(cmd, pass_fds=[disk_fd])
        except KeyboardInterrupt:
            exit_code = 130
            print(
                "Sesión interrumpida; se emite el informe igualmente.",
                file=sys.stderr,
            )
        finally:
            # El informe sale pase lo que pase: salida normal, Ctrl-C o error de QEMU.
            try:
                after = audit.take_snapshot()
            except (OSError, RuntimeError) as exc:
                print(
                    f"Aviso: no se pudo tomar la instantánea final: {exc}",
                    file=sys.stderr,
                )
                after = before
            try:
                audit.save_session(
                    audit.default_session_path(),
                    before,
                    after,
                    checks=tuple(results),
                    forced=args.force,
                )
            except (OSError, RuntimeError) as exc:
                print(f"Aviso: no se pudo guardar la sesión: {exc}", file=sys.stderr)
            _emit_report(
                audit.build_report(
                    before,
                    after,
                    args.log,
                    checks=tuple(results),
                    forced=args.force,
                ),
                args.report,
            )
        return exit_code
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if disk_fd is not None:
            try:
                os.close(disk_fd)
            except OSError:
                pass


def cmd_audit(args: argparse.Namespace) -> int:
    path = Path(args.session) if args.session else audit.default_session_path()
    if not path.exists():
        print(
            f"No hay sesión que auditar en {path}. Ejecuta primero `vvm run`.",
            file=sys.stderr,
        )
        return 1
    try:
        session = audit.load_session_full(path)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"No se pudo cargar la sesión: {exc}", file=sys.stderr)
        return 1
    print(
        audit.build_report(
            session.before,
            session.after,
            checks=session.checks,
            forced=session.forced,
        )
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parsea `argv` y despacha el subcomando. Devuelve el código de salida."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "check":
        return cmd_check(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "audit":
        return cmd_audit(args)
    parser.print_help()
    return 2
