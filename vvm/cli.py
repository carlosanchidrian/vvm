"""CLI de VVM: `check` | `run` | `audit` (SPEC §4-§6).

`main()` es la única puerta de entrada: devuelve el código de salida y no llama
a `sys.exit()` (eso lo hace `__main__.py`).

Orden de `run`, deliberado: validar la fuente de arranque y el tamaño del ISO
(solo `stat`) → pre-chequeo con una línea de descriptores simulados → línea base
→ crear/copiar los medios → línea real con los fds. P7 y P9 solo inspeccionan la
forma de la línea, así que el pre-chequeo puede ejecutarse antes de que exista
ningún descriptor: así P2 mide la memoria que hay de verdad y el ISO no se copia
si el anfitrión no es apto. La línea base se toma antes de crear los medios para
que el delta de Shmem no descuente el propio ISO de la herramienta.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import sys
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

from vvm import __version__, audit, host_check, images, vm
from vvm.host_check import CheckResult

# `vvm check` no arranca nada: estos nombres solo rellenan la línea que P7 y P9
# inspeccionan, porque no hay kernel ni initrd reales que pasar.
_PLACEHOLDER_KERNEL = "<kernel>"
_PLACEHOLDER_INITRD = "<initrd>"
_PLACEHOLDER_FD = "<fd>"

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
    check.add_argument("--image", metavar="ISO", help="imagen ISO del live")
    check.add_argument("--mem", type=int, default=512, metavar="MB")
    check.add_argument("--disk", type=int, default=256, metavar="MB")
    check.add_argument("--json", action="store_true", help="salida legible por máquina")

    run = subparsers.add_parser("run", help="arranca una sesión")
    run.add_argument("--image", metavar="ISO", help="imagen ISO del live (con ventana)")
    run.add_argument("--kernel", help="vmlinuz del guest")
    run.add_argument("--initrd", help="initramfs del guest")
    run.add_argument("--mem", type=int, default=512, metavar="MB")
    run.add_argument("--disk", type=int, default=256, metavar="MB")
    run.add_argument("--cpus", type=int, default=1)
    run.add_argument("--headless", action="store_true")
    run.add_argument(
        "--wipe",
        action="store_true",
        help="sobrescribe con ceros el disco de datos antes de cerrarlo",
    )
    run.add_argument(
        "--log",
        metavar="RUTA",
        help="log serie (solo --kernel/--initrd, obligatorio con --headless)",
    )
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


def _image_mb(size_bytes: int) -> int:
    """Tamaño del ISO redondeado hacia arriba a MiB (lo que P2 suma)."""
    mib = 1024 * 1024
    return (size_bytes + mib - 1) // mib


def cmd_check(args: argparse.Namespace) -> int:
    image_mb = 0
    try:
        if args.image:
            image_mb = _image_mb(images.validate_image(args.image))
            cmd = vm.build_cmd(
                vm.VmConfig(
                    image=args.image,
                    mem_mb=args.mem,
                    disk_mb=args.disk,
                ),
                disk_fd=0 if args.disk > 0 else None,
                image_fd=_PLACEHOLDER_FD,
            )
        else:
            cmd = vm.build_cmd(
                vm.VmConfig(
                    kernel=_PLACEHOLDER_KERNEL,
                    initrd=_PLACEHOLDER_INITRD,
                    mem_mb=args.mem,
                    disk_mb=args.disk,
                ),
                disk_fd=0 if args.disk > 0 else None,
            )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    results = host_check.check_host(
        args.mem,
        args.disk,
        cmd,
        image_mb=image_mb,
    )
    if args.json:
        print(json.dumps([asdict(result) for result in results], ensure_ascii=False, indent=2))
    else:
        print(_format_results(results))
    return 1 if host_check.has_blocking(results) else 0


def _close_fd(fd: int | None) -> None:
    """Cierra un descriptor; el `finally` exterior lo usa como red de seguridad."""
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _emit_report(report: str, report_path: str | None) -> None:
    """Imprime el informe o lo escribe en disco, sin perderlo si falla la escritura."""
    if not report_path:
        print(report)
        return
    path = Path(report_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(report, encoding="utf-8")
    except OSError as exc:
        print(
            f"Error: no se pudo escribir el informe en {report_path}: {exc}",
            file=sys.stderr,
        )
        print(report)
    else:
        print(f"Informe escrito en {report_path}")


def _boot_source_error(args: argparse.Namespace) -> str | None:
    """Valida la fuente de arranque; devuelve el mensaje de error o None.

    Todo lo que se puede rechazar sin tocar la memoria se rechaza aquí, antes de
    validar ficheros y de copiar el ISO (salida 2 en los dos modos).
    """
    if args.image and (args.kernel or args.initrd):
        return "--image y --kernel/--initrd son excluyentes"
    if args.image and args.headless:
        return (
            "--image con --headless no está soportado.\n"
            "[vvm] --image necesita pantalla: el cargador del ISO pinta su "
            "menú en la consola VGA y con -display none la sesión se queda "
            "parada ahí (medido: 1 MB leído, 0 B de log).\n"
            "[vvm] El modo sin ventana es solo para --kernel/--initrd."
        )
    if args.image and args.log:
        return (
            "--image y --log son excluyentes.\n"
            "El live se pilota en la ventana (-display gtk) y la consola del "
            "guest va a la VGA: no hay consola serie que guardar.\n"
            "El log serie es solo para --kernel/--initrd con --headless."
        )
    if not args.image:
        if args.kernel and not args.initrd:
            return "--kernel sin --initrd"
        if args.initrd and not args.kernel:
            return "--initrd sin --kernel"
        if not args.kernel and not args.initrd:
            return "Falta la fuente de arranque: usa --image o --kernel/--initrd"
    if args.mem <= 0:
        return f"--mem debe ser mayor que cero: {args.mem} MB"
    if args.disk < 0:
        return f"--disk no puede ser negativo: {args.disk} MB"
    if args.cpus <= 0:
        return f"--cpus debe ser mayor que cero: {args.cpus}"
    return None


def _closure_label(interrupt: int | None) -> str:
    """Etiqueta del cierre para el informe."""
    if interrupt is None:
        return "normal"
    if interrupt == signal.SIGINT:
        return "Ctrl-C"
    if interrupt == signal.SIGTERM:
        return "SIGTERM"
    if interrupt == signal.SIGHUP:
        return "SIGHUP"
    return f"señal {interrupt}"


def _wipe_disk(
    args: argparse.Namespace,
    disk_fd: int | None,
) -> audit.WipeInfo:
    """Borra el disco si hay uno; con `--disk 0` solo lo declara, no falla."""
    if args.disk == 0 or disk_fd is None:
        return audit.WipeInfo(requested=True, disk_mb=0)
    try:
        result = images.wipe_fd(disk_fd)
    except OSError as exc:
        print(f"Aviso: no se pudo borrar el disco: {exc}", file=sys.stderr)
        return audit.WipeInfo(
            requested=True,
            disk_mb=args.disk,
            error=str(exc),
        )
    return audit.WipeInfo(
        requested=True,
        disk_mb=args.disk,
        seconds=result.seconds,
    )


def cmd_run(args: argparse.Namespace) -> int:
    error = _boot_source_error(args)
    if error is not None:
        print(f"Error: {error}", file=sys.stderr)
        return 2

    # 1. Validación barata: fuente de arranque y tamaño del ISO (solo `stat`).
    image_mb = 0
    try:
        if args.image:
            image_mb = _image_mb(images.validate_image(args.image))
        else:
            images.validate_boot_files(args.kernel, args.initrd)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2 if args.image else 1

    config = vm.VmConfig(
        kernel=args.kernel,
        initrd=args.initrd,
        image=args.image,
        mem_mb=args.mem,
        disk_mb=args.disk,
        cpus=args.cpus,
        headless=args.headless,
        serial_log=args.log,
    )

    # 2. `--dry-run`: línea con descriptores simbólicos. No se crea ningún fd ni
    # se lee el ISO.
    if args.dry_run:
        try:
            cmd = vm.build_cmd(
                config,
                _PLACEHOLDER_FD if args.disk > 0 else None,
                qemu=args.qemu,
                image_fd=_PLACEHOLDER_FD if args.image else None,
            )
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2 if args.image else 1
        print(shlex.join(cmd))
        return 0

    # 3. Línea de pre-chequeo con descriptores simulados: P7 y P9 solo miran la
    # forma de la línea, así que el chequeo no necesita ningún fd real.
    try:
        precheck_cmd = vm.build_cmd(
            config,
            0 if args.disk > 0 else None,
            qemu=args.qemu,
            image_fd=0 if args.image else None,
        )
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2 if args.image else 1

    results = host_check.check_host(
        args.mem,
        args.disk,
        precheck_cmd,
        image_mb=image_mb,
    )
    print(_format_results(results))
    blocking = [
        result
        for result in results
        if result.status == "FAIL" and result.blocking
    ]
    # 4. Bloqueo sin `--force`: se sale sin haber copiado el ISO.
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

    disk_fd: int | None = None
    image_fd: int | None = None
    # 5. Guard de señales instalado antes de la línea base y de `Popen`, y
    # vigente durante todo el cierre (wipe, instantánea, informe y sesión).
    guard = vm.SignalGuard()
    try:
        guard.install()
    except (OSError, ValueError) as exc:
        print(f"Error: no se pudo instalar el guard de señales: {exc}", file=sys.stderr)
        return 1

    exit_code = 0
    qemu_interrupt: int | None = None
    try:
        # 6. Línea base limpia: antes de crear el disco y de copiar el ISO.
        before = audit.take_snapshot()
        extras = audit.SessionExtras(image_mode=bool(args.image))
        # 7. Medios ya autorizados por el pre-chequeo. El disco disperso (`memfd`
        # + `ftruncate`) no ocupa RAM hasta que el guest escribe; el ISO sí se
        # copia entero.
        if args.disk > 0:
            disk_fd = images.create_disk_fd(args.disk)
        if args.image:
            image_fd = images.create_image_fd(args.image)
        # Línea real, con los descriptores de verdad.
        cmd = vm.build_cmd(config, disk_fd, qemu=args.qemu, image_fd=image_fd)
        try:
            pass_fds = [
                fd for fd in (disk_fd, image_fd) if fd is not None
            ]
            # 8. Sesión bajo el guard del llamador: `run_session` no lo restaura.
            outcome = vm.run_session(cmd, pass_fds=pass_fds, guard=guard)
            exit_code = outcome.returncode
            qemu_interrupt = outcome.interrupt
            extras = audit.SessionExtras(
                closure=_closure_label(outcome.interrupt),
                killed_after=outcome.killed_after,
                image_mode=bool(args.image),
                process_io=outcome.process_io,
            )
        except KeyboardInterrupt:
            exit_code = 130
            qemu_interrupt = signal.SIGINT
            extras = audit.SessionExtras(
                closure="Ctrl-C",
                image_mode=bool(args.image),
            )
            print(
                "Sesión interrumpida; se emite el informe igualmente.",
                file=sys.stderr,
            )
        finally:
            # 9. El informe sale pase lo que pase: salida normal, Ctrl-C o
            # señal. Durante el teardown el guard solo registra la señal; no
            # aborta el wipe, la instantánea, el informe ni la sesión.
            # Orden deliberado: cerrar el ISO, borrar el disco si se pidió,
            # cerrar el disco y solo entonces medir. Si el disco siguiera
            # abierto, Shmem incluiría lo que el guest escribió y el «después»
            # nunca volvería a la línea base. El finally exterior queda como
            # red de seguridad si algo falla antes.
            _close_fd(image_fd)
            image_fd = None
            if args.wipe:
                extras = replace(extras, wipe=_wipe_disk(args, disk_fd))
            _close_fd(disk_fd)
            disk_fd = None
            try:
                after = audit.take_snapshot()
            except (OSError, RuntimeError) as exc:
                print(
                    f"Aviso: no se pudo tomar la instantánea final: {exc}",
                    file=sys.stderr,
                )
                after = before
            if guard.interrupt is not None:
                # La señal llegó antes de construir el informe: se declara.
                extras = replace(extras, closure=_closure_label(guard.interrupt))
            report_text = audit.build_report(
                before,
                after,
                None if args.image else args.log,
                checks=tuple(results),
                forced=args.force,
                extras=extras,
            )
            _emit_report(report_text, args.report)
            try:
                audit.save_session(
                    audit.default_session_path(),
                    before,
                    after,
                    checks=tuple(results),
                    forced=args.force,
                    extras=extras,
                )
            except (OSError, RuntimeError) as exc:
                print(f"Aviso: no se pudo guardar la sesión: {exc}", file=sys.stderr)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        _close_fd(image_fd)
        _close_fd(disk_fd)
        # 10. Restauración del guard al final del cierre.
        guard.restore()

    if qemu_interrupt is None and guard.interrupt is not None:
        # Señal que no inició el cierre de QEMU (p. ej. durante el wipe).
        exit_code = 128 + guard.interrupt
    return exit_code


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
            extras=session.extras,
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
