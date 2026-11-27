"""Pantallas de la TUI: perfiles, pre-chequeo, sesión e informe."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
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
    TabbedContent,
    TabPane,
)

from vvm import audit, profiles, vm
from vvm.host_check import CheckResult
from vvm.session import Session

from .widgets import (
    CheckTable,
    ReportView,
    SessionPanel,
    SessionRuntime,
    SessionTab,
)


def _blocking(results: Sequence[CheckResult]) -> list[CheckResult]:
    return [
        result
        for result in results
        if result.status == "FAIL" and result.blocking
    ]


def _warnings(results: Sequence[CheckResult]) -> list[CheckResult]:
    return [result for result in results if result.status != "OK"]


def fill_profile_table(table: DataTable, profiles_file: profiles.Profiles) -> list[str]:
    """Rellena la tabla de perfiles y devuelve los nombres en el orden de filas.

    La usan la pantalla completa de perfiles y el selector en superposición, para
    que las dos enseñen exactamente las mismas columnas y el mismo orden.
    """
    names = sorted(profiles_file.profiles)
    table.add_columns("NOMBRE", "FUENTE", "RAM", "DISCO", "WIPE")
    for name in names:
        profile = profiles_file.profiles[name]
        table.add_row(
            profile.name,
            profile.source,
            f"{profile.mem_mb} MB",
            f"{profile.disk_mb} MB",
            "sí" if profile.wipe else "no",
            key=name,
        )
    return names


class ProfilesScreen(Screen):
    """Lista de perfiles con la fuente, RAM, disco y wipe de cada uno."""

    BINDINGS = [
        ("enter", "start", "Arrancar"),
        ("c", "check", "Pre-chequeo"),
        ("n", "new", "Nuevo perfil"),
        ("e", "edit", "Editar"),
        ("d", "delete", "Eliminar"),
        ("a", "last_report", "Último informe"),
        ("escape", "quit", "Volver/Salir"),
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
        self.names = fill_profile_table(table, self.profiles_file)
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
        self.app.quit_or_return()


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
            yield Label("Log serie (solo CLI)")
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
        previous_default = self.profiles_file.default
        try:
            saved = profiles.delete_profile(
                self.profiles_file.path, self.profile.name
            )
        except (OSError, profiles.ProfilesError) as exc:
            self.query_one("#delete-error", Static).update(f"Error: {exc}")
            return
        if previous_default == self.profile.name:
            if saved.default is None:
                self.app.notify("No queda ningún perfil por defecto")
            else:
                self.app.notify(
                    f"El perfil por defecto pasa a ser {saved.default!r}"
                )
        self.app.show_profiles(profiles_file=saved)

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "delete-yes":
            self.action_confirm()
        elif event.button.id == "delete-no":
            self.action_cancel()


@dataclass(frozen=True)
class PickedProfile:
    """Resultado del selector: arrancar un perfil o ir a gestionarlos."""

    name: str | None = None
    manage: bool = False


class ProfilePickerScreen(ModalScreen["PickedProfile | None"]):
    """Selector de perfil en superposición: las pestañas siguen a la vista.

    Es el botón «casa» de la barra superior. Arranca otra VM sin salir del centro
    de control, así que las sesiones vivas no desaparecen de la pantalla.
    """

    BINDINGS = [
        ("enter", "pick", "Arrancar"),
        ("p", "manage", "Gestionar perfiles"),
        ("escape", "cancel", "Volver"),
        ("q", "cancel", "Volver"),
    ]

    def __init__(
        self,
        profiles_file: profiles.Profiles,
        *,
        selected: str | None = None,
        running: int = 0,
    ) -> None:
        super().__init__()
        self.profiles_file = profiles_file
        self.selected = selected
        self.running = running
        self.names: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-box"):
            yield Static("Arrancar una VM", id="picker-title")
            yield Static(
                f"{self.running} VM(s) en marcha, siguen a la vista"
                if self.running
                else "No hay VMs vivas",
                id="picker-sub",
            )
            yield DataTable(id="picker-table", cursor_type="row", zebra_stripes=True)
            yield Static(
                "enter: arrancar · p: gestionar perfiles · esc: volver",
                id="picker-hint",
            )

    def on_mount(self) -> None:
        table = self.query_one("#picker-table", DataTable)
        self.names = fill_profile_table(table, self.profiles_file)
        if self.selected in self.names:
            table.move_cursor(row=self.names.index(self.selected))
        if self.names:
            table.focus()

    def _current_name(self) -> str | None:
        if not self.names:
            return None
        table = self.query_one("#picker-table", DataTable)
        row = table.cursor_row
        if row is None or not (0 <= row < len(self.names)):
            return None
        return self.names[row]

    def action_pick(self) -> None:
        name = self._current_name()
        if name is None:
            self.app.notify("No hay ningún perfil seleccionado", severity="warning")
            return
        self.dismiss(PickedProfile(name=name))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """`enter` sobre la tabla lo consume DataTable: arrancar desde su evento."""
        event.stop()
        name = str(event.row_key.value) if event.row_key.value else None
        if name:
            self.dismiss(PickedProfile(name=name))
        else:  # pragma: no cover - la tabla siempre lleva key=nombre
            self.action_pick()

    def action_manage(self) -> None:
        self.dismiss(PickedProfile(manage=True))

    def action_cancel(self) -> None:
        self.dismiss(None)


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
        self.app.quit_or_return()


class MainScreen(Screen):
    """Centro de control multi-VM: barra superior, pestañas y cierre total."""

    BINDINGS = [
        ("q", "close_active", "Cerrar VM"),
        ("n", "new_vm", "Nueva VM"),
        ("p", "manage_profiles", "Perfiles"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.tabs_by_id: dict[str, SessionTab] = {}
        self.pane_order: list[str] = []

    def compose(self) -> ComposeResult:
        with Horizontal(id="topbar"):
            yield Button(
                "⌂",
                id="home",
                tooltip="Menú de VMs (perfiles)",
            )
            yield Static("VVM · 0 VMs", id="topbar-title")
            yield Button(
                "🗑",
                id="close-all",
                variant="error",
                tooltip="Cerrar todo + wipe",
            )
        yield Static("sin VMs · n abre perfiles", id="empty-state")
        yield Horizontal(id="tabstrip")
        yield TabbedContent(id="session-tabs")
        yield Footer()

    def on_mount(self) -> None:
        self._update_empty()

    @property
    def is_ready(self) -> bool:
        """True cuando la barra superior y las pestañas ya están compuestas."""
        try:
            self.query_one("#topbar-title", Static)
            self.query_one(TabbedContent)
        except NoMatches:
            return False
        return True

    # -- pestañas ---------------------------------------------------------

    def _pane_id(self, runtime_id: str) -> str:
        return f"pane-{runtime_id}"

    async def add_session(self, runtime: SessionRuntime) -> None:
        tabs = self.query_one(TabbedContent)
        pane = TabPane(
            f"● {runtime.label}",
            SessionTab(runtime),
            id=self._pane_id(runtime.id),
        )
        await tabs.add_pane(pane)
        tab = pane.query_one(SessionTab)
        self.tabs_by_id[runtime.id] = tab
        self.pane_order.append(runtime.id)
        tabs.active = self._pane_id(runtime.id)
        await self._add_tab_buttons(runtime, tab)
        self._focus_tab(runtime.id)
        self.call_after_refresh(self._focus_tab, runtime.id)
        self._update_empty()

    async def remove_session(self, runtime_id: str) -> None:
        pane_id = self._pane_id(runtime_id)
        self.tabs_by_id.pop(runtime_id, None)
        if runtime_id in self.pane_order:
            self.pane_order.remove(runtime_id)
        if pane_id in [pane.id for pane in self.query(TabPane)]:
            await self.query_one(TabbedContent).remove_pane(pane_id)
        item = self._tab_item(runtime_id)
        if item is not None:
            await item.remove()
        if self.pane_order:
            active = self.active_runtime_id() or self.pane_order[0]
            self._focus_tab(active)
        self._update_empty()

    def tab_for(self, runtime_id: str) -> SessionTab | None:
        return self.tabs_by_id.get(runtime_id)

    def _tab_item(self, runtime_id: str) -> Horizontal | None:
        try:
            return self.query_one(f"#tabitem-{runtime_id}", Horizontal)
        except NoMatches:
            return None

    async def _add_tab_buttons(self, runtime: SessionRuntime, tab: SessionTab) -> None:
        """Pestaña con forma de navegador: etiqueta que activa y ✕ que cierra."""
        strip = self.query_one("#tabstrip", Horizontal)
        item = Horizontal(
            Button(
                runtime.label,
                id=f"tabbtn-{runtime.id}",
                classes="tabbtn",
                tooltip=f"{runtime.label} · {runtime.state}",
            ),
            Button(
                "✕",
                id=f"tabclose-{runtime.id}",
                classes="tabclose",
                tooltip=f"Cerrar {runtime.label} (igual que q)",
            ),
            id=f"tabitem-{runtime.id}",
            classes="tabitem",
        )
        await strip.mount(item)
        self._mark_active_tab()
        self.call_after_refresh(self._mark_active_tab)

    def _mark_active_tab(self) -> None:
        active = self.active_runtime_id()
        for runtime_id in self.tabs_by_id:
            for widget_id in (f"#tabbtn-{runtime_id}", f"#tabclose-{runtime_id}"):
                try:
                    widget = self.query_one(widget_id, Button)
                except NoMatches:
                    continue
                widget.set_class(runtime_id == active, "active-tab")

    def activate_runtime(self, runtime_id: str) -> None:
        if runtime_id not in self.tabs_by_id:
            return
        self.query_one(TabbedContent).active = self._pane_id(runtime_id)
        self._mark_active_tab()
        self._focus_tab(runtime_id)
        self.call_after_refresh(self._focus_tab, runtime_id)

    def active_tab(self) -> SessionTab | None:
        runtime_id = self.active_runtime_id()
        return self.tabs_by_id.get(runtime_id) if runtime_id else None

    def active_runtime_id(self) -> str | None:
        tabs = self.query_one(TabbedContent)
        active = tabs.active
        if not active:
            return None
        runtime_id = active.removeprefix("pane-")
        return runtime_id if runtime_id in self.tabs_by_id else None

    def switch_tab(self, offset: int) -> str | None:
        if not self.pane_order:
            return None
        current = self.active_runtime_id()
        index = (
            self.pane_order.index(current)
            if current in self.pane_order
            else 0
        )
        index = (index + offset) % len(self.pane_order)
        runtime_id = self.pane_order[index]
        self.query_one(TabbedContent).active = self._pane_id(runtime_id)
        self._focus_tab(runtime_id)
        self.call_after_refresh(self._focus_tab, runtime_id)
        return runtime_id

    def _focus_tab(self, runtime_id: str) -> None:
        """El foco debe caer en la pestaña visible, no en una oculta."""
        tab = self.tabs_by_id.get(runtime_id)
        if tab is None or not tab.is_mounted:
            return
        try:
            tab.query_one(SessionPanel).focus()
        except NoMatches:
            self.call_after_refresh(self._focus_tab, runtime_id)

    def _focus_active(self) -> None:
        runtime_id = self.active_runtime_id()
        if runtime_id is not None:
            self._focus_tab(runtime_id)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        """Al activar una pestaña, el foco va a su panel aunque llegue tarde."""
        self._mark_active_tab()
        self.call_after_refresh(self._focus_active)

    def update_tab_label(self, runtime_id: str) -> None:
        tab = self.tabs_by_id.get(runtime_id)
        if tab is None:
            return
        try:
            content_tab = self.query_one(TabbedContent).get_tab(
                self._pane_id(runtime_id)
            )
            content_tab.label = tab.runtime.label
        except (ValueError, NoMatches):
            pass
        try:
            button = self.query_one(f"#tabbtn-{runtime_id}", Button)
            button.label = tab.runtime.label
            button.tooltip = f"{tab.runtime.label} · {tab.runtime.state}"
        except NoMatches:
            pass
        self._mark_active_tab()

    def refresh_all(self, snapshot: audit.Snapshot | None) -> None:
        if not self.is_ready:
            return
        for runtime_id, tab in list(self.tabs_by_id.items()):
            tab.refresh_view(snapshot)
            self.update_tab_label(runtime_id)
        self._update_title()

    def _update_title(self) -> None:
        try:
            self.query_one("#topbar-title", Static).update(
                f"VVM · {len(self.pane_order)} VM(s)"
            )
        except NoMatches:
            return

    def _update_empty(self) -> None:
        empty = len(self.pane_order) == 0
        try:
            self.query_one("#empty-state", Static).display = empty
            self.query_one("#session-tabs", TabbedContent).display = not empty
            self.query_one("#tabstrip", Horizontal).display = not empty
        except NoMatches:
            return
        self._update_title()

    # -- acciones ---------------------------------------------------------

    def action_close_active(self) -> None:
        runtime_id = self.active_runtime_id()
        if runtime_id is None:
            return
        self.app.close_session(runtime_id)

    def action_new_vm(self) -> None:
        self.app.open_vm_picker()

    def action_manage_profiles(self) -> None:
        self.app.open_new_vm()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "home":
            self.app.open_vm_picker()
        elif button_id == "close-all":
            self.app.confirm_close_all()
        elif button_id.startswith("tabbtn-"):
            self.activate_runtime(button_id.removeprefix("tabbtn-"))
        elif button_id.startswith("tabclose-"):
            self.app.close_session(button_id.removeprefix("tabclose-"))


class VmMenuScreen(ModalScreen[None]):
    """Menú de la VM activa; es la única salida de la captura de teclado."""

    BINDINGS = [
        ("n", "new_vm", "Nueva VM"),
        ("s", "stop_vm", "Parar"),
        ("w", "wipe", "Wipe"),
        ("r", "report", "Informe"),
        ("left", "prev_tab", "Anterior"),
        ("right", "next_tab", "Siguiente"),
        ("q", "close_vm", "Cerrar VM"),
        ("c", "ctrl_c", "Ctrl-C al guest"),
        ("escape", "dismiss_menu", "Volver"),
    ]

    def __init__(self, runtime: SessionRuntime) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        with Vertical(id="vm-menu"):
            yield Static(
                f"VM {self.runtime.id} · {self.runtime.label}", id="vm-menu-title"
            )
            yield Static(
                "n nueva VM   s parar   w wipe   r informe\n"
                "←/→ cambiar pestaña   q cerrar esta VM   c Ctrl-C\n"
                "esc volver",
                id="vm-menu-options",
            )
        yield Footer()

    def action_dismiss_menu(self) -> None:
        self.dismiss(None)

    def action_new_vm(self) -> None:
        self.dismiss(None)
        self.app.open_vm_picker()

    def action_stop_vm(self) -> None:
        self.dismiss(None)
        self.app.stop_session(self.runtime.id)

    def action_wipe(self) -> None:
        enabled = self.app.toggle_wipe(self.runtime.id)
        self.query_one("#vm-menu-title", Static).update(
            f"VM {self.runtime.id} · {self.runtime.label} · "
            f"wipe {'sí' if enabled else 'no'}"
        )

    def action_report(self) -> None:
        self.dismiss(None)
        self.app.show_report(self.runtime.id)

    def action_prev_tab(self) -> None:
        self.dismiss(None)
        self.app.switch_tab(-1)

    def action_next_tab(self) -> None:
        self.dismiss(None)
        self.app.switch_tab(1)

    def action_close_vm(self) -> None:
        self.dismiss(None)
        self.app.close_session(self.runtime.id)

    def action_ctrl_c(self) -> None:
        self.dismiss(None)
        self.app.send_ctrl_c(self.runtime.id)


class CloseAllScreen(ModalScreen[bool]):
    """Confirmación de una tecla para cerrar todas las VM con wipe."""

    BINDINGS = [
        Binding("enter", "confirm", "Confirmar", priority=True),
        ("escape", "cancel", "Cancelar"),
    ]

    def __init__(self, count: int) -> None:
        super().__init__()
        self.count = count

    def compose(self) -> ComposeResult:
        with Vertical(id="close-all-box"):
            yield Static(
                f"¿Cerrar {self.count} VM(s)?", id="close-all-title"
            )
            yield Static(
                "Se pondrá a cero el disco de cada una. "
                "Enter confirma; Esc cancela.",
                id="close-all-text",
            )
            with Horizontal(id="close-all-buttons"):
                yield Button("Cancelar", id="cancel-all")
                yield Button(
                    "Cerrar todo + wipe",
                    id="confirm-all",
                    variant="error",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#cancel-all", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-all":
            self.dismiss(True)
        elif event.button.id == "cancel-all":
            self.dismiss(False)


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
        self.app.quit_or_return()
