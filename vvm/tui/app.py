"""Aplicación Textual de VVM: centro de control multi-VM.

Este módulo es el único de `vvm.tui` que importa Textual al cargarse; el CLI lo
importa solo después de comprobar el terminal y el extra. La lógica de VM vive
en `vvm.session`, `vvm.supervisor`, `vvm.console` y `vvm.audit`: aquí solo se
pinta y se enrutan eventos.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.widgets import Button, DataTable, Static, Tab

from vvm import audit, host_check, images, profiles, supervisor, vm
from vvm.console import SerialConsole
from vvm.host_check import CheckResult
from vvm.session import Session

from .screens import (
    CloseAllScreen,
    DeleteProfileScreen,
    MainScreen,
    PreCheckScreen,
    ProfileFormScreen,
    ProfilesScreen,
    ReportScreen,
    VmMenuScreen,
)
from .widgets import ConsoleView, SessionRuntime

# Ruta simbólica del pre-chequeo: P7/P9 solo miran la forma de la línea.
_PRECHECK_SOCKET = "<socket>"


class VvmApp(App):
    """TUI de VVM. Las sesiones viven fuera de la interfaz y se gobiernan aquí."""

    CSS_PATH = "app.tcss"
    TITLE = "VVM — Volatile Virtual Machines"

    BINDINGS = [
        Binding("ctrl+g", "open_vm_menu", "Menú VM", priority=True, show=False),
        Binding(
            "ctrl+c",
            "guest_ctrl_c",
            "Ctrl-C al guest",
            priority=True,
            show=False,
        ),
        Binding(
            "ctrl+q",
            "close_all_prompt",
            "Cerrar todo + wipe",
            priority=True,
        ),
    ]

    def __init__(
        self,
        *,
        profile: str | None = None,
        profile_name: str | None = None,
        profiles_path: str | Path | None = None,
        qemu: str | None = None,
        refresh_interval: float | None = None,
        session_factory: Any | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.requested_profile = profile or profile_name
        self.profiles_path = Path(profiles_path) if profiles_path else None
        self.qemu = qemu or vm.DEFAULT_QEMU
        self.refresh_interval = (
            1.0 if refresh_interval is None else max(0.05, refresh_interval)
        )
        self.session_factory = session_factory or Session
        self.profiles_file: profiles.Profiles | None = None
        self.sessions: dict[str, SessionRuntime] = {}
        self.main_screen: MainScreen | None = None
        self.group = supervisor.SessionGroup()
        self.last_report: str | None = None
        self.last_reports: dict[str, str | None] = {}
        self._closing_all = False
        self._signal_closing = False

    # -- arranque de la pantalla -----------------------------------------

    def on_mount(self) -> None:
        try:
            self.group.install()
        except (OSError, ValueError) as exc:
            self.notify(
                f"No se pudo instalar el guardia de señales: {exc}",
                severity="warning",
            )
        self.set_interval(self.refresh_interval, self._refresh_sessions)
        self.set_interval(0.2, self._poll_group_signal)


        target = self.profiles_path or profiles.default_profiles_path()
        warning: str | None = None
        try:
            loaded = profiles.load_profiles(target)
        except profiles.ProfilesError as exc:
            if target.exists():
                self.notify(str(exc), severity="error", timeout=10)
                self.exit(return_code=2)
                return
            # Sin TOML no se cierra la TUI: se abre vacía para poder crear el
            # primer perfil desde el formulario y guardarlo en esa ruta.
            loaded = profiles.Profiles(path=target, profiles={}, default=None)
            warning = (
                f"no existe {target}; pulsa n para crear el primer perfil"
            )

        self.profiles_file = loaded
        selected = self.requested_profile or loaded.default
        if warning is None and loaded.default_warning:
            warning = loaded.default_warning
        if self.requested_profile and self.requested_profile not in loaded.profiles:
            warning = (
                f"el perfil {self.requested_profile!r} no existe en {loaded.path}"
            )
            selected = None
        self.push_screen(
            ProfilesScreen(loaded, selected=selected, warning=warning)
        )

    # -- configuración y pre-chequeo -------------------------------------

    def session_config(self, profile: profiles.Profile) -> vm.VmConfig:
        """Config del perfil con la consola serie en un socket de la sesión."""
        return replace(
            profile.to_config(),
            serial_mode="socket",
            serial_log=None,
            serial_sock=_PRECHECK_SOCKET,
        )

    def precheck(
        self, profile: profiles.Profile
    ) -> tuple[vm.VmConfig, list[CheckResult]]:
        config = self.session_config(profile)
        image_mb = 0
        if config.image:
            size = images.validate_image(config.image)
            image_mb = math.ceil(size / (1024 * 1024))
        cmd = vm.build_cmd(
            config,
            "<fd>" if config.disk_mb > 0 else None,
            qemu=self.qemu,
            image_fd="<fd>" if config.image else None,
        )
        results = host_check.check_host(
            config.mem_mb,
            config.disk_mb,
            cmd,
            image_mb=image_mb,
        )
        return config, results

    def open_precheck(self, profile: profiles.Profile, *, check_only: bool) -> None:
        self.push_screen(
            PreCheckScreen(profile, check_only=check_only)
        )

    # -- sesiones ---------------------------------------------------------

    def start_session(
        self,
        profile: profiles.Profile,
        config: vm.VmConfig,
        results: Sequence[CheckResult],
        *,
        forced: bool,
    ) -> None:
        # El id y su directorio se reservan de forma atómica antes de arrancar:
        # la ruta del socket serie nace de ahí y `live.json` se escribe tras el
        # `Popen`.
        try:
            session_id, _runtime_dir = supervisor.reserve_session(profile.name)
        except (OSError, RuntimeError) as exc:
            self.notify(f"No se pudo reservar la sesión: {exc}", severity="error")
            return
        runtime_config = replace(config, serial_sock=None)
        current = self.session_factory(
            runtime_config,
            qemu=self.qemu,
            checks=tuple(results),
            forced=forced,
            log_path=None,
            session_id=session_id,
            label=profile.name,
            install_signals=False,
            should_stop=self.group.should_stop,
        )
        try:
            current.start()
        except InterruptedError as exc:
            current.close()
            self.notify(f"Arranque interrumpido: {exc}", severity="warning")
            return
        except (OSError, ValueError, RuntimeError) as exc:
            current.close()
            self.notify(f"No se pudo arrancar: {exc}", severity="error")
            return

        if not current.started:
            current.close()
            self.notify(
                "Arranque interrumpido antes de lanzar QEMU",
                severity="warning",
            )
            return

        serial_path = current.config.serial_sock
        if not serial_path:
            current.close()
            self.notify(
                "La sesión no tiene socket de consola serie", severity="error"
            )
            return
        try:
            console = SerialConsole(serial_path)
        except (OSError, ValueError) as exc:
            current.finish(wipe=False)
            self.notify(f"No se pudo abrir la consola serie: {exc}", severity="error")
            return

        warnings = tuple(
            result for result in results if result.status != "OK"
        )
        runtime = SessionRuntime(
            id=current.session_id or session_id,
            label=profile.name,
            profile=profile,
            session=current,
            warnings=warnings,
            console=console,
            wipe=bool(profile.wipe),
        )
        self.sessions[runtime.id] = runtime
        self.group.sessions.append(current)
        self._mount_runtime(runtime)

    def _mount_runtime(self, runtime: SessionRuntime) -> None:
        if self.main_screen is None:
            self.main_screen = MainScreen()
            self.run_worker(
                self._push_and_add(self.main_screen, runtime)
            )
            return
        self.run_worker(self._add_runtime(runtime))

    async def _push_and_add(
        self, main: MainScreen, runtime: SessionRuntime
    ) -> None:
        if main not in self.screen_stack:
            # Sustituye el PreCheck por el centro de control: no se acumulan
            # pantallas debajo al arrancar la primera VM.
            await self.switch_screen(main)
        await self._add_runtime(runtime)

    async def _add_runtime(self, runtime: SessionRuntime) -> None:
        main = self.main_screen
        if main is None:
            return
        for _ in range(200):
            if main.is_ready:
                break
            await asyncio.sleep(0.01)
        await main.add_session(runtime)
        # Si la sesión se lanzó desde el menú, hay pantallas encima de la
        # principal: se retiran para volver al centro de control.
        if main in self.screen_stack and self.screen is not main:
            while (
                self.screen is not main
                and len(self.screen_stack) > 1
            ):
                await self.pop_screen()

    def _tab(self, runtime: SessionRuntime):
        main = self.main_screen
        return main.tab_for(runtime.id) if main is not None else None

    # -- refresco ---------------------------------------------------------

    def _refresh_sessions(self) -> None:
        if not self.sessions:
            return
        main = self.main_screen
        if (
            main is None
            or not main.is_ready
            or main not in self.screen_stack
        ):
            return
        try:
            snapshot = audit.take_snapshot()
        except (OSError, RuntimeError):
            snapshot = None
        main.refresh_all(snapshot)
        for runtime in list(self.sessions.values()):
            if (
                runtime.report is None
                and not runtime.finishing
                and runtime.session.started
                and runtime.session.poll() is not None
            ):
                self._begin_finish(
                    runtime, wipe=runtime.wipe, stop=False, remove=False
                )

    def _poll_group_signal(self) -> None:
        if self.group.stop_signal is None or self._signal_closing:
            return
        self._signal_closing = True
        self.close_all(
            wipe=True, exit_code=128 + self.group.stop_signal
        )

    # -- cierre de una VM -------------------------------------------------

    def _schedule_remove(self, runtime: SessionRuntime) -> None:
        if runtime.removing:
            return
        runtime.removing = True
        self.run_worker(self._remove_runtime(runtime.id))

    def _begin_finish(
        self,
        runtime: SessionRuntime,
        *,
        wipe: bool,
        stop: bool,
        remove: bool,
    ) -> None:
        if runtime.removing:
            return
        if runtime.report is not None:
            if remove:
                self._schedule_remove(runtime)
            return
        if runtime.finishing:
            if remove:
                runtime.pending_remove = True
            return
        runtime.finishing = True
        tab = self._tab(runtime)
        if tab is not None:
            tab.mark_finishing()
        if self.main_screen is not None:
            self.main_screen.update_tab_label(runtime.id)
        self.run_worker(
            lambda: self._finish_worker(runtime, wipe, stop, remove),
            thread=True,
            group="finish",
        )

    def _finish_worker(
        self,
        runtime: SessionRuntime,
        wipe: bool,
        stop: bool,
        remove: bool,
    ) -> None:
        current = runtime.session
        try:
            if stop and current.running:
                current.stop()
            report = (
                current.report
                if current.finished
                else current.finish(wipe=wipe)
            )
        except (OSError, RuntimeError, ValueError) as exc:
            try:
                current.close()
            except (OSError, RuntimeError):
                pass
            report = f"Error al cerrar la VM: {exc}"
        finally:
            runtime.console.stop()
        try:
            self.call_from_thread(
                self._finish_done, runtime, report, remove
            )
        except RuntimeError:
            # La app se cerró mientras terminaba el worker: no hay UI que
            # actualizar, pero la sesión ya quedó cerrada.
            pass

    def _finish_done(
        self,
        runtime: SessionRuntime,
        report: str,
        remove: bool,
    ) -> None:
        runtime.finishing = False
        runtime.report = report
        runtime.state = "terminada"
        self.last_report = report
        self.last_reports[runtime.id] = report
        tab = self._tab(runtime)
        if tab is not None:
            tab.show_report(report)
        if self.main_screen is not None:
            self.main_screen.update_tab_label(runtime.id)
        if remove or runtime.pending_remove:
            runtime.pending_remove = False
            self._schedule_remove(runtime)

    async def _remove_runtime(self, runtime_id: str) -> None:
        runtime = self.sessions.pop(runtime_id, None)
        if runtime is None:
            return
        main = self.main_screen
        runtime.console.stop()
        try:
            runtime.session.close()
        except (OSError, RuntimeError):
            pass
        if runtime.session in self.group.sessions:
            self.group.sessions.remove(runtime.session)
        if main is not None:
            try:
                await main.remove_session(runtime_id)
            except NoMatches:
                pass
        if not self.sessions:
            self.main_screen = None
            if not await self._pop_to_profiles():
                self.show_profiles()

    def close_session(self, runtime_id: str) -> None:
        """Cierra solo la VM indicada; la app sigue viva."""
        runtime = self.sessions.get(runtime_id)
        if runtime is None or runtime.removing:
            return
        if runtime.report is not None:
            self._schedule_remove(runtime)
            return
        if runtime.finishing:
            runtime.pending_remove = True
            return
        self._begin_finish(
            runtime, wipe=runtime.wipe, stop=True, remove=True
        )

    def stop_session(self, runtime_id: str) -> None:
        runtime = self.sessions.get(runtime_id)
        if runtime is None:
            return
        self._begin_finish(
            runtime, wipe=runtime.wipe, stop=True, remove=False
        )

    def toggle_wipe(self, runtime_id: str) -> bool:
        runtime = self.sessions.get(runtime_id)
        if runtime is None:
            return False
        tab = self._tab(runtime)
        if tab is not None:
            return tab.toggle_wipe()
        runtime.wipe = not runtime.wipe
        return runtime.wipe

    def show_report(self, runtime_id: str) -> None:
        runtime = self.sessions.get(runtime_id)
        if runtime is None or runtime.report is None:
            self.notify("Esta VM todavía no tiene informe", severity="warning")
            return
        tab = self._tab(runtime)
        if tab is not None:
            tab.show_report(runtime.report)

    def send_ctrl_c(self, runtime_id: str) -> None:
        runtime = self.sessions.get(runtime_id)
        if runtime is not None:
            runtime.console.send(b"\x03")

    def action_guest_ctrl_c(self) -> None:
        """Ctrl-C prioritario: con captura va al guest; sin captura no hace nada."""
        if self.main_screen is None or self.screen is not self.main_screen:
            return
        runtime_id = self.main_screen.active_runtime_id()
        if runtime_id is None:
            return
        runtime = self.sessions.get(runtime_id)
        tab = self._tab(runtime) if runtime is not None else None
        if tab is None:
            return
        try:
            view = tab.query_one(ConsoleView)
        except NoMatches:
            return
        if view.capture:
            runtime.console.send(b"\x03")

    def switch_tab(self, offset: int) -> None:
        if self.main_screen is not None:
            self.main_screen.switch_tab(offset)

    # -- cierre total -----------------------------------------------------

    def confirm_close_all(self) -> None:
        if self._closing_all or isinstance(self.screen, CloseAllScreen):
            return
        if not self.sessions:
            self.exit(return_code=0)
            return
        self.push_screen(
            CloseAllScreen(len(self.sessions)),
            callback=self._on_close_all_confirmed,
        )

    def _on_close_all_confirmed(self, confirmed: bool | None) -> None:
        if confirmed:
            self.close_all(wipe=True, exit_code=0)

    def close_all(self, *, wipe: bool, exit_code: int) -> None:
        if self._closing_all:
            return
        self._closing_all = True
        runtimes = list(self.sessions.values())
        for runtime in runtimes:
            runtime.finishing = True
            tab = self._tab(runtime)
            if tab is not None:
                tab.mark_finishing()
            if self.main_screen is not None:
                self.main_screen.update_tab_label(runtime.id)
        if not runtimes:
            self.exit(return_code=exit_code)
            return
        self.run_worker(
            lambda: self._close_all_worker(runtimes, wipe, exit_code),
            thread=True,
            group="close-all",
        )

    def _close_all_worker(
        self,
        runtimes: Sequence[SessionRuntime],
        wipe: bool,
        exit_code: int,
    ) -> None:
        reports: dict[str, str | None] = {}
        for runtime in list(runtimes):
            current = runtime.session
            try:
                if current.running:
                    current.stop()
                reports[runtime.id] = (
                    current.report
                    if current.finished
                    else current.finish(wipe=wipe)
                )
            except (OSError, RuntimeError, ValueError) as exc:
                try:
                    current.close()
                except (OSError, RuntimeError):
                    pass
                reports[runtime.id] = f"Error al cerrar la VM: {exc}"
            finally:
                runtime.console.stop()
        try:
            self.call_from_thread(
                self._close_all_done, reports, exit_code
            )
        except RuntimeError:
            pass

    def _close_all_done(
        self,
        reports: dict[str, str | None],
        exit_code: int,
    ) -> None:
        for runtime_id, report in reports.items():
            runtime = self.sessions.get(runtime_id)
            if runtime is not None and report is not None:
                runtime.report = report
                runtime.state = "terminada"
                tab = self._tab(runtime)
                if tab is not None:
                    tab.show_report(report)
                if self.main_screen is not None:
                    self.main_screen.update_tab_label(runtime_id)
        valid = [report for report in reports.values() if report]
        if valid:
            self.last_report = valid[-1]
        self.last_reports.update(reports)
        self.exit(return_code=exit_code)

    # -- menú y perfiles --------------------------------------------------

    def action_open_vm_menu(self) -> None:
        if isinstance(self.screen, (CloseAllScreen, VmMenuScreen)):
            return
        if self.main_screen is None:
            return
        runtime_id = self.main_screen.active_runtime_id()
        if runtime_id is None:
            return
        runtime = self.sessions.get(runtime_id)
        if runtime is None:
            return
        tab = self._tab(runtime)
        if tab is not None:
            tab.leave_capture()
        self.push_screen(VmMenuScreen(runtime))

    def action_close_all_prompt(self) -> None:
        self.confirm_close_all()

    def open_new_vm(self) -> None:
        if self.profiles_file is None:
            return
        self.push_screen(
            ProfilesScreen(
                self.profiles_file,
                selected=self.requested_profile or self.profiles_file.default,
            )
        )

    # -- informes ---------------------------------------------------------

    def report_path(self) -> Path:
        return audit.default_session_path().with_name("last-report.txt")

    def show_last_report(self) -> None:
        path = audit.default_session_path()
        if not path.exists():
            self.notify(
                f"No hay sesión que auditar en {path}. Ejecuta primero `vvm run`.",
                severity="warning",
            )
            return
        try:
            saved = audit.load_session_full(path)
        except (OSError, RuntimeError, ValueError) as exc:
            self.notify(f"No se pudo cargar la sesión: {exc}", severity="error")
            return
        report = audit.build_report(
            saved.before,
            saved.after,
            saved.extras.log_path,
            checks=saved.checks,
            forced=saved.forced,
            extras=saved.extras,
        )
        self.last_report = report
        self.push_screen(ReportScreen(report))

    # -- formulario de perfiles ------------------------------------------

    def open_profile_form(
        self, profile: profiles.Profile | None = None
    ) -> None:
        if self.profiles_file is None:
            return
        self.push_screen(
            ProfileFormScreen(self.profiles_file, profile=profile)
        )

    def confirm_delete(self, profile: profiles.Profile) -> None:
        if self.profiles_file is None:
            return
        self.push_screen(
            DeleteProfileScreen(profile, self.profiles_file)
        )

    # -- navegación --------------------------------------------------------

    async def _return_to_main(self) -> None:
        main = self.main_screen
        if main is None:
            return
        while (
            main in self.screen_stack
            and self.screen is not main
            and len(self.screen_stack) > 1
        ):
            await self.pop_screen()

    async def _pop_to_profiles(self) -> bool:
        """Vuelve a una `ProfilesScreen` ya presente, sin crear otra."""
        if not any(
            isinstance(screen, ProfilesScreen)
            for screen in self.screen_stack
        ):
            return False
        while (
            len(self.screen_stack) > 1
            and not isinstance(self.screen, ProfilesScreen)
        ):
            await self.pop_screen()
        return isinstance(self.screen, ProfilesScreen)

    def quit_or_return(self) -> None:
        """`q` sale solo si no hay VMs; con VMs vuelve al centro de control."""
        if (
            self.sessions
            and self.main_screen is not None
            and self.main_screen in self.screen_stack
        ):
            self.run_worker(self._return_to_main())
            return
        self.exit(return_code=0)

    def show_profiles(
        self,
        *,
        profiles_file: profiles.Profiles | None = None,
        selected: str | None = None,
        warning: str | None = None,
    ) -> None:
        loaded = profiles_file or self.profiles_file
        if loaded is None:
            return
        self.profiles_file = loaded
        if warning is None:
            warning = loaded.default_warning
        self.switch_screen(
            ProfilesScreen(
                loaded,
                selected=selected or self.requested_profile,
                warning=warning,
            )
        )

    # -- utilidades para la captura de texto ------------------------------

    def export_text(self) -> str:
        """Texto plano de la pantalla actual, útil para pruebas sin TTY."""
        chunks: list[str] = [type(self.screen).__name__]
        if self.screen is not None:
            for widget in self.screen.walk_children():
                if not widget.display:
                    continue
                if isinstance(widget, Static):
                    try:
                        chunks.append(str(widget.render()))
                    except Exception:  # pragma: no cover - widget sin render
                        pass
                elif isinstance(widget, Button):
                    chunks.append(str(widget.label))
                elif isinstance(widget, Tab):
                    chunks.append(str(widget.label))
                elif isinstance(widget, DataTable):
                    for row_index in range(widget.row_count):
                        try:
                            row = widget.get_row_at(row_index)
                        except Exception:  # pragma: no cover - tabla vacía
                            continue
                        chunks.append(" | ".join(str(cell) for cell in row))
        return "\n".join(chunk for chunk in chunks if chunk)

    def on_unmount(self) -> None:
        self.group.restore()
        for runtime in list(self.sessions.values()):
            runtime.console.stop()
            current = runtime.session
            try:
                if current.running:
                    current.stop()
                if not current.finished:
                    current.finish(wipe=True)
            except (OSError, RuntimeError, ValueError):
                try:
                    current.close()
                except (OSError, RuntimeError):
                    pass


# Alias razonables para quien importe la app desde una prueba o un script.
VvmTUI = VvmApp
VvmTui = VvmApp
App = VvmApp
