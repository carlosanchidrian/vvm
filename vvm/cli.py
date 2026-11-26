"""CLI de VVM: `check` | `run` | `audit` (SPEC §4-§6).

`main()` es la única puerta de entrada: devuelve el código de salida y no llama
a `sys.exit()` (eso lo hace `__main__.py`).

Orden de `run`, deliberado: validar la fuente de arranque y el tamaño del ISO
(solo `stat`) → pre-chequeo con una línea de descriptores simulados → línea base
→ crear/copiar los medios → línea real con los fds → guard de señales y sesión.
P7 y P9 solo inspeccionan la forma de la línea, así que el pre-chequeo puede
ejecutarse antes de que exista ningún descriptor: así P2 mide la memoria que hay
de verdad y el ISO no se copia si el anfitrión no es apto. La línea base se toma
antes de crear los medios para que el delta de Shmem no descuente el propio ISO
de la herramienta. El guard se instala antes de `Popen` y sigue vigente durante
todo el teardown: una señal en el cierre no impide el informe ni la sesión.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import signal
import sys
from collections.abc import Sequence
from dataclasses import asdict, replace
from pathlib import Path

from vvm import __version__, audit, host_check, images, profiles, session, vm
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
    subparsers = parser.add_subparsers(
        dest="command", metavar="{check,run,audit,profiles,tui}"
    )

    check = subparsers.add_parser("check", help="pre-chequeo P1-P10 del anfitrión")
    check.add_argument("--profile", metavar="NOMBRE", help="perfil de escenario")
    check.add_argument("--image", metavar="ISO", help="imagen ISO del live")
    check.add_argument("--mem", type=int, default=None, metavar="MB")
    check.add_argument("--disk", type=int, default=None, metavar="MB")
    check.add_argument("--json", action="store_true", help="salida legible por máquina")

    run = subparsers.add_parser("run", help="arranca una sesión")
    run.add_argument("--profile", metavar="NOMBRE", help="perfil de escenario")
    run.add_argument("--image", metavar="ISO", help="imagen ISO del live (con ventana)")
    run.add_argument("--kernel", help="vmlinuz del guest")
    run.add_argument("--initrd", help="initramfs del guest")
    run.add_argument("--mem", type=int, default=None, metavar="MB")
    run.add_argument("--disk", type=int, default=None, metavar="MB")
    run.add_argument("--cpus", type=int, default=None)
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
    run.add_argument("--qemu", default=vm.DEFAULT_QEMU)
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

    profiles_parser = subparsers.add_parser(
        "profiles", help="lista los perfiles de escenario"
    )
    profiles_parser.add_argument(
        "--json", action="store_true", help="salida legible por máquina"
    )

    tui_parser = subparsers.add_parser("tui", help="abre la interfaz de terminal")
    tui_parser.add_argument("--profile", metavar="NOMBRE", help="perfil inicial")
    tui_parser.add_argument(
        "--qemu",
        default=vm.DEFAULT_QEMU,
        help="binario de QEMU (para pruebas o compilaciones propias)",
    )
    tui_parser.add_argument(
        "--profiles",
        metavar="RUTA",
        help="fichero de perfiles alternativo (por defecto, el de XDG)",
    )
    tui_parser.add_argument(
        "--refresh",
        type=float,
        default=None,
        metavar="SEGUNDOS",
        help="intervalo de refresco del panel de sesión",
    )
    return parser


def _apply_profile(args: argparse.Namespace) -> str | None:
    """Resuelve `--profile` y los valores por defecto de los flags.

    Este tramo distingue «flag no pasado» (`None`) de «flag con valor»: así el
    perfil puede aportar sus valores y los flags explícitos siguen teniendo la
    última palabra. Sin perfil, los valores son los de siempre.
    """
    profile_name = getattr(args, "profile", None)
    if profile_name is None:
        if getattr(args, "mem", None) is None:
            args.mem = 512
        if getattr(args, "disk", None) is None:
            args.disk = 256
        if getattr(args, "cpus", None) is None:
            args.cpus = 1
        if not hasattr(args, "headless"):
            args.headless = False
        if not hasattr(args, "wipe"):
            args.wipe = False
        if not hasattr(args, "log"):
            args.log = None
        return None

    sources = [
        option
        for option in ("image", "kernel", "initrd")
        if getattr(args, option, None)
    ]
    if sources:
        flags = ", ".join(f"--{name}" for name in sources)
        return (
            f"--profile no se combina con {flags}: "
            "la fuente de arranque la decide el perfil"
        )

    try:
        loaded = profiles.load_profiles()
        profile = loaded.get(profile_name)
    except profiles.ProfilesError as exc:
        return str(exc)

    config = profile.to_config()
    config = replace(
        config,
        mem_mb=(
            args.mem if getattr(args, "mem", None) is not None else profile.mem_mb
        ),
        disk_mb=(
            args.disk
            if getattr(args, "disk", None) is not None
            else profile.disk_mb
        ),
        cpus=(
            args.cpus
            if getattr(args, "cpus", None) is not None
            else profile.cpus
        ),
        headless=(
            bool(getattr(args, "headless", False)) or profile.headless
        ),
        serial_log=(
            args.log
            if getattr(args, "log", None) is not None
            else profile.serial_log
        ),
    )
    try:
        vm._validate_config(config)
    except ValueError as exc:
        return f"perfil {profile_name!r} en {loaded.path}: {exc}"

    args.image = config.image
    args.kernel = config.kernel
    args.initrd = config.initrd
    args.mem = config.mem_mb
    args.disk = config.disk_mb
    args.cpus = config.cpus
    args.headless = config.headless
    args.log = config.serial_log
    args.wipe = bool(getattr(args, "wipe", False)) or profile.wipe
    return None


def cmd_profiles(args: argparse.Namespace) -> int:
    """Lista los perfiles del TOML, en texto o como JSON estable."""
    try:
        loaded = profiles.load_profiles()
    except profiles.ProfilesError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if loaded.default_warning:
        print(f"Aviso: {loaded.default_warning}", file=sys.stderr)

    entries = [
        {
            "name": profile.name,
            "source": profile.source,
            "mem_mb": profile.mem_mb,
            "disk_mb": profile.disk_mb,
            "network": "none",
        }
        for profile in sorted(loaded.profiles.values(), key=lambda item: item.name)
    ]
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    print(f"{'NOMBRE':<20} {'FUENTE':<8} {'RAM':>6} {'DISCO':>6} {'RED':<5}")
    for entry in entries:
        print(
            f"{entry['name']:<20} {entry['source']:<8} "
            f"{entry['mem_mb']:>6} {entry['disk_mb']:>6} {entry['network']:<5}"
        )
    print(f"Perfiles: {loaded.path}")
    if loaded.default is not None:
        print(f"Por defecto: {loaded.default}")
    return 0


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
    profile_error = _apply_profile(args)
    if profile_error is not None:
        print(f"Error: {profile_error}", file=sys.stderr)
        return 2

    image_mb = 0
    try:
        if args.image:
            image_mb = _image_mb(images.validate_image(args.image))
            config = vm.VmConfig(
                image=args.image,
                mem_mb=args.mem,
                disk_mb=args.disk,
                cpus=args.cpus,
                headless=args.headless,
                serial_log=args.log,
            )
            cmd = vm.build_cmd(
                config,
                disk_fd=_PLACEHOLDER_FD if args.disk > 0 else None,
                image_fd=_PLACEHOLDER_FD,
            )
        else:
            config = vm.VmConfig(
                kernel=getattr(args, "kernel", None) or _PLACEHOLDER_KERNEL,
                initrd=getattr(args, "initrd", None) or _PLACEHOLDER_INITRD,
                mem_mb=args.mem,
                disk_mb=args.disk,
                cpus=args.cpus,
                headless=args.headless,
                serial_log=args.log,
            )
            cmd = vm.build_cmd(
                config,
                disk_fd=_PLACEHOLDER_FD if args.disk > 0 else None,
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


def cmd_run(args: argparse.Namespace) -> int:
    profile_error = _apply_profile(args)
    if profile_error is not None:
        print(f"Error: {profile_error}", file=sys.stderr)
        return 2

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

    # 3. Línea de pre-chequeo con descriptores simulados: los chequeos solo
    # miran su forma, así que no necesitan ningún fd real.
    try:
        precheck_cmd = vm.build_cmd(
            config,
            _PLACEHOLDER_FD if args.disk > 0 else None,
            qemu=args.qemu,
            image_fd=_PLACEHOLDER_FD if args.image else None,
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

    # 5. La sesión toma la línea base, crea los medios y lanza QEMU. El CLI
    # conserva su línea serie en stdio; el log solo se declara en el informe.
    current = session.Session(
        config,
        qemu=args.qemu,
        checks=results,
        forced=args.force,
        log_path=None if args.image else args.log,
    )
    exit_code = 0
    outcome: session.SessionOutcome | None = None
    try:
        try:
            current.start()
        except InterruptedError as exc:
            # La copia del ISO se abortó antes de lanzar QEMU: no hay sesión
            # que cerrar ni informe que emitir, igual que en el tramo 5.
            print(f"Interrumpido: {exc}", file=sys.stderr)
            current.close()
            return 128 + (current.interrupt or signal.SIGINT)
        outcome = current.wait()
        exit_code = outcome.returncode
    except KeyboardInterrupt:
        exit_code = 130
        print(
            "Sesión interrumpida; se emite el informe igualmente.",
            file=sys.stderr,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        if current.before is None:
            current.close()
            return 1
        exit_code = 1

    try:
        report_text = current.finish(wipe=args.wipe)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Error: no se pudo cerrar la sesión: {exc}", file=sys.stderr)
        current.close()
        return 1

    _emit_report(report_text, args.report)

    if (outcome is None or outcome.interrupt is None) and current.interrupt is not None:
        # Señal que no inició el cierre de QEMU (p. ej. durante el wipe).
        exit_code = 128 + current.interrupt
    if (
        current.wipe_info is not None
        and current.wipe_info.error is not None
        and exit_code == 0
    ):
        # Un wipe pedido y fallido es un cierre incompleto: no puede salir 0.
        # Si ya había un código de señal (130/143/129) se conserva.
        print(
            "El cierre quedó incompleto por el error de borrado; se sale con 1.",
            file=sys.stderr,
        )
        exit_code = 1
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
    report = audit.build_report(
        session.before,
        session.after,
        checks=session.checks,
        forced=session.forced,
        extras=session.extras,
    )
    # `build_report` ya termina en salto de línea: sin uno extra, el informe de
    # `audit` es byte a byte igual al que escribió `run`.
    sys.stdout.write(report)
    return 0


def _textual_available() -> bool:
    """True si el extra `tui` está instalado, sin importar Textual al cargar."""
    try:
        return importlib.util.find_spec("textual") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def cmd_tui(args: argparse.Namespace) -> int:
    """Abre la TUI. Sin TTY o sin el extra, explica qué falta y sale con 2."""
    no_tty = not sys.stdin.isatty()
    no_textual = not _textual_available()
    if no_tty:
        print(
            "[vvm] la TUI necesita un terminal interactivo "
            "(prueba `vvm run --profile NOMBRE`).",
            file=sys.stderr,
        )
    if no_textual:
        print(
            "[vvm] falta el extra de la TUI: pip install -e '.[tui]'",
            file=sys.stderr,
        )
    if no_tty or no_textual:
        return 2

    # Un TOML ausente ya no bloquea la TUI: la pantalla de perfiles se abre
    # vacía y ofrece crearlo. Sí es un error pedir un perfil concreto cuando
    # no hay fichero que lo contenga.
    target = (
        Path(args.profiles) if args.profiles else profiles.default_profiles_path()
    )
    if target.exists():
        try:
            loaded = profiles.load_profiles(target)
        except profiles.ProfilesError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        if args.profile:
            try:
                loaded.get(args.profile)
            except profiles.ProfilesError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
    elif args.profile:
        print(
            f"Error: no existe el fichero de perfiles: {target}",
            file=sys.stderr,
        )
        return 2

    try:
        from vvm.tui.app import VvmApp
    except ImportError:
        print(
            "[vvm] falta el extra de la TUI: pip install -e '.[tui]'",
            file=sys.stderr,
        )
        return 2

    app = VvmApp(
        profile=args.profile,
        qemu=args.qemu,
        profiles_path=args.profiles,
        refresh_interval=args.refresh,
    )
    app.run()
    return int(getattr(app, "return_code", 0) or 0)


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
    if args.command == "profiles":
        return cmd_profiles(args)
    if args.command == "tui":
        return cmd_tui(args)
    parser.print_help()
    return 2
