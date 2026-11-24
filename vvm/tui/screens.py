"""Pantallas de la TUI: perfiles, pre-chequeo, sesión e informe."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from vvm import audit, host_check, images, profiles, vm
from vvm.host_check import CheckResult
from vvm.session import Session

from .widgets import CheckTable, ReportView, SessionPanel


def _blocking(results: Sequence[CheckResult]) -> list[CheckResult]:
    return [
        result
        for result in results
        if result.status == "FAIL" and result.blocking
    ]


def _warnings(results: Sequence[CheckResult]) -> list[CheckResult]:
    return [result for result in results if result.status != "OK"]


class ProfilesScreen(Screen):
    """Lista de perfiles con la fuente, RAM, disco y wipe de cada uno."""

    BINDINGS = [
        ("enter", "start", "Arrancar"),
        ("c", "check", "Pre-chequeo"),
        ("n", "new", "Nuevo perfil"),
        ("e", "edit", "Editar"),
        ("d", "delete", "Eliminar"),
        ("a", "last_report", "Último informe"),
        ("q", "quit", "Salir"),
    ]

    def __init__(
        self,
        profiles_file: profiles.Profiles,
        *,
        selected: str | None = None,
        warning: str | None = None,
    ) -> None:
        super().__init__()
        self.profiles_file = profiles_file
        self.selected = selected
        self.warning = warning
        self.names = sorted(profiles_file.profiles)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Perfiles de escenario", id="profiles-title")
        yield DataTable(id="profile-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="profiles-path")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#profile-table", DataTable)
        table.add_columns("NOMBRE", "FUENTE", "RAM", "DISCO", "WIPE")
        for name in self.names:
            profile = self.profiles_file.profiles[name]
            table.add_row(
                profile.name,
                profile.source,
                f"{profile.mem_mb} MB",
                f"{profile.disk_mb} MB",
                "sí" if profile.wipe else "no",
                key=name,
            )
        if self.selected in self.names:
            table.move_cursor(row=self.names.index(self.selected))
        if self.names:
            table.focus()

        message = f"Fichero: {self.profiles_file.path}"
        if not self.names:
            message += "\nNo hay perfiles todavía. Pulsa n para crear el primero."
        if self.warning:
            message += f"\nAviso: {self.warning}"
            self.app.notify(self.warning, severity="warning")
        self.query_one("#profiles-path", Static).update(message)

    def _current_name(self) -> str | None:
        if not self.names:
            return None
        table = self.query_one("#profile-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self.names)):
            return None
        return self.names[row]

    def _current_profile(self) -> profiles.Profile | None:
        name = self._current_name()
        return self.profiles_file.profiles.get(name) if name else None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "profile-table":
            event.stop()
            self.action_start()

    def action_start(self) -> None:
        profile = self._current_profile()
        if profile is None:
            self.app.notify("No hay ningún perfil seleccionado", severity="warning")
            return
        self.app.open_precheck(profile, check_only=False)

    def action_check(self) -> None:
        profile = self._current_profile()
        if profile is None:
            self.app.notify("No hay ningún perfil seleccionado", severity="warning")
            return
        self.app.open_precheck(profile, check_only=True)

    def action_last_report(self) -> None:
        self.app.show_last_report()

    def action_new(self) -> None:
        self.app.open_profile_form()

    def action_edit(self) -> None:
        profile = self._current_profile()
        if profile is None:
            self.app.notify("No hay ningún perfil seleccionado", severity="warning")
            return
        self.app.open_profile_form(profile)

    def action_delete(self) -> None:
        profile = self._current_profile()
        if profile is None:
            self.app.notify("No hay ningún perfil seleccionado", severity="warning")
            return
        self.app.confirm_delete(profile)

    def action_quit(self) -> None:
        self.app.exit(0)


class ProfileFormScreen(Screen):
    """Formulario para crear o editar un perfil y guardarlo en el TOML."""

    BINDINGS = [
        ("ctrl+s", "save", "Guardar"),
        ("escape", "cancel", "Cancelar"),
        ("q", "cancel", "Cancelar"),
    ]

    def __init__(
        self,
        profiles_file: profiles.Profiles,
        *,
        profile: profiles.Profile | None = None,
    ) -> None:
        super().__init__()
        self.profiles_file = profiles_file
        self.editing = profile

    def compose(self) -> ComposeResult:
        editing = self.editing
        yield Header(show_clock=True)
        yield Static(
            f"Editar perfil {editing.name}" if editing else "Nuevo perfil",
            id="form-title",
        )
        with VerticalScroll(id="form-scroll"):
            yield Label("Nombre")
            yield Input(
                value=editing.name if editing else "",
                placeholder="kali",
                id="profile-name",
                disabled=editing is not None,
            )
            yield Label("Fuente de arranque")
            yield Select(
                [("ISO (live)", "iso"), ("kernel + initrd", "kernel")],
                value="iso" if editing is None or editing.image else "kernel",
                id="profile-source",
                allow_blank=False,
            )
            yield Label("Imagen ISO")
            yield Input(
                value=editing.image or "" if editing else "",
                placeholder="~/isos/live.iso",
                id="profile-image",
            )
            yield Label("Kernel")
            yield Input(
                value=editing.kernel or "" if editing else "",
                placeholder="~/vmlinuz",
                id="profile-kernel",
            )
            yield Label("Initrd")
            yield Input(
                value=editing.initrd or "" if editing else "",
                placeholder="~/initramfs",
                id="profile-initrd",
            )
            yield Label("RAM (MB)")
            yield Input(
                value=str(editing.mem_mb if editing else 512),
                id="profile-mem",
            )
            yield Label("Disco (MB)")
            yield Input(
                value=str(editing.disk_mb if editing else 256),
                id="profile-disk",
            )
            yield Label("CPUs")
            yield Input(
                value=str(editing.cpus if editing else 1),
                id="profile-cpus",
            )
            yield Label("Log serie (opcional)")
            yield Input(
                value=editing.serial_log or "" if editing else "",
                placeholder="~/vvm-logs/serie.log",
                id="profile-log",
            )
            yield Checkbox(
                "Headless (solo kernel)",
                value=bool(editing.headless) if editing else False,
                id="profile-headless",
            )
            yield Checkbox(
                "Wipe al cerrar",
                value=bool(editing.wipe) if editing else False,
                id="profile-wipe",
            )
        yield Static("", id="form-error")
        with Horizontal(id="form-buttons"):
            yield Button("Guardar", id="save", variant="primary")
            yield Button("Cancelar", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        name = self.query_one("#profile-name", Input)
        if name.disabled:
            self.query_one("#profile-source", Select).focus()
        else:
            name.focus()

    def _int_field(self, selector: str, label: str) -> int:
        raw = self.query_one(selector, Input).value.strip()
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"{label} debe ser un número entero: {raw!r}") from None
        if value < 0 or (value == 0 and selector != "#profile-disk"):
            raise ValueError(f"{label} debe ser mayor que cero: {value}")
        return value

    def _build_profile(self) -> profiles.Profile:
        name = self.query_one("#profile-name", Input).value.strip()
        if not name:
            raise ValueError("El nombre del perfil no puede estar vacío")
        if self.editing is not None:
            name = self.editing.name
        elif name in self.profiles_file.profiles:
            raise ValueError(
                f"Ya existe un perfil {name!r}; elige otro nombre"
            )

        source = str(self.query_one("#profile-source", Select).value)
        image = self.query_one("#profile-image", Input).value.strip()
        kernel = self.query_one("#profile-kernel", Input).value.strip()
        initrd = self.query_one("#profile-initrd", Input).value.strip()
        serial_log = self.query_one("#profile-log", Input).value.strip()
        headless = self.query_one("#profile-headless", Checkbox).value
        wipe = self.query_one("#profile-wipe", Checkbox).value

        if source == "iso":
            if not image:
                raise ValueError("La fuente ISO necesita una ruta de imagen")
            kernel = ""
            initrd = ""
            if headless:
                raise ValueError("El modo ISO no admite headless")
            if serial_log:
                raise ValueError("El modo ISO no usa log serie")
        else:
            if not kernel or not initrd:
                raise ValueError("La fuente kernel necesita kernel e initrd")
            image = ""
            if headless and not serial_log:
                raise ValueError("Headless necesita un log serie")

        profile = profiles.Profile(
            name=name,
            image=image or None,
            kernel=kernel or None,
            initrd=initrd or None,
            mem_mb=self._int_field("#profile-mem", "La RAM"),
            disk_mb=self._int_field("#profile-disk", "El disco"),
            cpus=self._int_field("#profile-cpus", "Las CPUs"),
            headless=headless,
            serial_log=serial_log or None,
            wipe=wipe,
        )
        vm._validate_config(profile.to_config())
        return profile

    def _show_error(self, message: str) -> None:
        self.query_one("#form-error", Static).update(f"Error: {message}")

    def action_save(self) -> None:
        try:
            profile = self._build_profile()
            if self.editing is None:
                saved = profiles.add_profile(self.profiles_file.path, profile)
            else:
                saved = profiles.update_profile(
                    self.profiles_file.path, profile
                )
        except (ValueError, OSError, profiles.ProfilesError) as exc:
            self._show_error(str(exc))
            return
        self.app.show_profiles(profiles_file=saved, selected=profile.name)

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        elif event.button.id == "cancel":
            self.action_cancel()


class DeleteProfileScreen(Screen):
    """Confirmación antes de borrar un perfil del TOML."""

    BINDINGS = [
        ("s", "confirm", "Sí, eliminar"),
        ("enter", "confirm", "Sí, eliminar"),
        ("n", "cancel", "Cancelar"),
        ("escape", "cancel", "Cancelar"),
    ]

    def __init__(
        self,
        profile: profiles.Profile,
        profiles_file: profiles.Profiles,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.profiles_file = profiles_file

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="delete-box"):
            yield Static(
                f"¿Eliminar el perfil {self.profile.name!r}?",
                id="delete-title",
            )
            yield Static(
                f"Se borrará de {self.profiles_file.path}. "
                "Esta acción no se puede deshacer.",
                id="delete-text",
            )
            yield Static("", id="delete-error")
            with Horizontal(id="delete-buttons"):
                yield Button(
                    "Sí, eliminar", id="delete-yes", variant="error"
                )
                yield Button("Cancelar", id="delete-no")
        yield Footer()

    def action_confirm(self) -> None:
        try:
            saved = profiles.delete_profile(
                self.profiles_file.path, self.profile.name
            )
        except (OSError, profiles.ProfilesError) as exc:
            self.query_one("#delete-error", Static).update(f"Error: {exc}")
            return
        self.app.show_profiles(profiles_file=saved)

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete-yes":
            self.action_confirm()
        elif event.button.id == "delete-no":
            self.action_cancel()


class PreCheckScreen(Screen):
    """P1-P10 del perfil, con la opción de seguir o forzar."""

    BINDINGS = [
        ("enter", "continue", "Seguir"),
        ("f", "force", "Forzar"),
        ("r", "repeat", "Repetir"),
        ("escape", "back", "Volver"),
        ("q", "quit", "Salir"),
    ]

    def __init__(
        self,
        profile: profiles.Profile,
        *,
        check_only: bool = False,
    ) -> None:
        super().__init__()
        self.profile = profile
        self.check_only = check_only
        self.results: tuple[CheckResult, ...] = ()
        self.config: vm.VmConfig | None = None
        self.error: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="precheck-title")
        yield CheckTable()
        yield Static("", id="precheck-verdict")
        yield Footer()

    def on_mount(self) -> None:
        self.reload()

    def reload(self) -> None:
        self.query_one("#precheck-title", Static).update(
            f"Pre-chequeo del perfil {self.profile.name}"
        )
        try:
            config, results = self.app.precheck(self.profile)
        except (OSError, ValueError, RuntimeError) as exc:
            self.error = str(exc)
            self.results = ()
            self.config = None
            self.query_one(CheckTable).load([])
            self.query_one("#precheck-verdict", Static).update(f"Error: {exc}")
            return

        self.error = None
        self.config = config
        self.results = tuple(results)
        self.query_one(CheckTable).load(results)

        blocking = _blocking(results)
        warnings = _warnings(results)
        if blocking:
            names = ", ".join(result.vector for result in blocking)
            verdict = (
                f"NO apto: {len(blocking)} fallo(s) bloqueante(s): {names}. "
                "Pulsa f para forzar y arrancar."
            )
        elif warnings:
            verdict = (
                f"Apto para arrancar, con {len(warnings)} aviso(s). "
                "Pulsa enter para seguir."
            )
        else:
            verdict = "Apto para arrancar. Pulsa enter para seguir."
        self.query_one("#precheck-verdict", Static).update(verdict)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "check-table":
            event.stop()
            self.action_continue()

    def action_continue(self) -> None:
        if self.check_only:
            self.app.pop_screen()
            return
        if self.config is None or not self.results:
            self.app.notify("No hay un pre-chequeo válido", severity="warning")
            return
        if _blocking(self.results):
            self.app.notify(
                "Hay fallos bloqueantes: pulsa f para forzar", severity="warning"
            )
            return
        self.app.start_session(self.profile, self.config, self.results, forced=False)

    def action_force(self) -> None:
        if self.check_only:
            self.app.notify(
                "Este pre-chequeo no arranca la VM; vuelve y pulsa enter",
                severity="warning",
            )
            return
        if self.config is None:
            self.app.notify("No hay un pre-chequeo válido", severity="warning")
            return
        self.app.start_session(self.profile, self.config, self.results, forced=True)

    def action_repeat(self) -> None:
        self.reload()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit(0)


class SessionScreen(Screen):
    """Sesión en vivo con el panel de I/O y memoria."""

    BINDINGS = [
        ("s", "stop", "Parar"),
        ("l", "log", "Log serie"),
        ("w", "toggle_wipe", "Wipe"),
        ("q", "close", "Cerrar e informar"),
    ]

    def __init__(
        self,
        session: Session,
        warnings: Sequence[CheckResult],
        *,
        refresh_interval: float | None = None,
    ) -> None:
        super().__init__()
        self.session = session
        self.warnings = tuple(warnings)
        self.refresh_interval = refresh_interval
        self.wipe = False
        self._finished = False
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Sesión en vivo", id="session-title")
        yield SessionPanel()
        yield Static("", id="session-log")
        yield Footer()

    def on_mount(self) -> None:
        interval = (
            self.refresh_interval
            if self.refresh_interval is not None
            else self.app.refresh_interval
        )
        self._timer = self.set_interval(max(0.05, interval), self.refresh_panel)
        self.refresh_panel()

    def _snapshot(self) -> audit.Snapshot | None:
        try:
            return audit.take_snapshot()
        except (OSError, RuntimeError):
            return None

    def refresh_panel(self) -> None:
        if self._finished:
            return
        panel = self.query_one(SessionPanel)
        if self.session.running:
            panel.state = "viva"
            panel.show(
                self.session,
                self._snapshot(),
                self.warnings,
                wipe=self.wipe,
            )
            self.query_one("#session-log", Static).update(
                f"Log serie: {self.session.log_path or 'ninguno'}"
            )
            return

        panel.state = "terminada"
        panel.show(
            self.session,
            self._snapshot(),
            self.warnings,
            wipe=self.wipe,
        )
        self._finish_and_report(auto_exit=True)

    def _finish_and_report(self, *, auto_exit: bool) -> None:
        if self._finished:
            return
        self._finished = True
        panel = self.query_one(SessionPanel)
        panel.update_state("cerrando")
        report = self.session.finish(wipe=self.wipe)
        self.app.last_report = report
        self.app.push_screen(ReportScreen(report, session=self.session))
        if auto_exit and self.app.auto_exit_on_finish:
            self.app.set_timer(self.app.exit_delay, lambda: self.app.exit(0))

    def action_stop(self) -> None:
        if not self._finished:
            self.query_one(SessionPanel).update_state("cerrando")
            self.session.stop()
        self._finish_and_report(auto_exit=False)
        self.app.exit(0)

    def action_close(self) -> None:
        if not self._finished:
            if self.session.running:
                self.session.stop()
            self._finish_and_report(auto_exit=False)

    def action_toggle_wipe(self) -> None:
        self.wipe = not self.wipe
        self.refresh_panel()

    def action_log(self) -> None:
        path = self.session.log_path
        if not path:
            self.app.notify("Esta sesión no tiene log serie")
            return
        if not Path(path).exists():
            self.app.notify(f"El log todavía no existe: {path}")
            return
        self.app.notify(f"Log serie: {path}")

    def on_unmount(self) -> None:
        if self._finished:
            return
        try:
            if self.session.running:
                self.session.stop()
            self.session.finish(wipe=self.wipe)
        except (OSError, RuntimeError, ValueError):
            self.session.close()


class ReportScreen(Screen):
    """Informe completo con desplazamiento y guardado."""

    BINDINGS = [
        ("g", "save", "Guardar"),
        ("y", "copy", "Copiar ruta"),
        ("escape", "back", "Volver"),
        ("q", "quit", "Salir"),
    ]

    def __init__(self, report: str, *, session: Session | None = None) -> None:
        super().__init__()
        self.report = report
        self.session = session

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("Informe de la sesión", id="report-title")
        with VerticalScroll(id="report-scroll"):
            yield ReportView(self.report)
        yield Static("", id="report-path")
        yield Footer()

    def action_save(self) -> None:
        path = self.app.report_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.report, encoding="utf-8")
        except OSError as exc:
            self.app.notify(f"No se pudo guardar: {exc}", severity="error")
            return
        self.query_one("#report-path", Static).update(f"Informe guardado en {path}")
        self.app.notify(f"Informe guardado en {path}")

    def action_copy(self) -> None:
        try:
            self.app.copy_to_clipboard(self.report)
        except Exception:  # pragma: no cover - depende del portapapeles real
            self.app.notify("El portapapeles no está disponible", severity="warning")
            return
        self.app.notify("Informe copiado al portapapeles")

    def action_back(self) -> None:
        self.app.show_profiles()

    def action_quit(self) -> None:
        self.app.exit(0)
