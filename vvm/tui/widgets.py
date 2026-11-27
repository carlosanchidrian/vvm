"""Widgets de la TUI, sin lógica de procesos ni de ficheros del sistema."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from rich.text import Text
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import DataTable, Static

from vvm import audit, profiles
from vvm.console import ConsoleSnapshot, SerialConsole, key_to_bytes, span_style
from vvm.host_check import CheckResult
from vvm.session import IOStats, Session

_STATUS_STYLE = {"OK": "green", "WARN": "yellow", "FAIL": "bold red"}
_STATE_SYMBOL = {"viva": "●", "cerrando": "◐", "terminada": "○", "arrancando": "◌"}


def _format_kb(value: int) -> str:
    return f"{value} kB"


class CheckTable(DataTable):
    """Tabla P1-P10 con estado, evidencia y remedio."""

    def __init__(self) -> None:
        super().__init__(id="check-table", cursor_type="row", zebra_stripes=True)

    def load(self, results: Sequence[CheckResult]) -> None:
        self.clear(columns=True)
        self.add_columns("ID", "ESTADO", "COMPROBACIÓN", "EVIDENCIA", "REMEDIO")
        for result in results:
            self.add_row(
                result.id,
                result.status,
                result.vector,
                result.evidence,
                result.remediation if result.status != "OK" else "",
                key=result.id,
            )


class SessionPanel(Static):
    """Panel de la sesión: estado, PID, I/O y memoria frente a la línea base."""

    can_focus = True

    def __init__(self, id: str = "session-panel") -> None:
        super().__init__(id=id, classes="session-panel")
        self.text = ""
        self.state = "arrancando"

    def update_state(self, state: str) -> None:
        self.state = state
        self.update(self.text)

    def show(
        self,
        session: Session,
        snapshot: audit.Snapshot | None,
        warnings: Sequence[CheckResult],
        *,
        wipe: bool,
        other_sessions: int = 0,
    ) -> None:
        """Compone el panel con la última muestra; nunca lee ficheros él mismo."""
        io: IOStats | None = session.io()
        lines: list[Text] = []
        if io is None:
            lines.append(Text("I/O: no disponible (el proceso ya no existe)."))
        else:
            write_style = "green" if io.write_bytes == 0 else "yellow"
            write = Text("write_bytes: ", style="bold")
            write.append(f"{io.write_bytes} B", style=write_style)
            lines.append(write)
            if io.write_bytes > 0 and session.config.image:
                if session.config.disk_mb == 0:
                    lines.append(
                        Text(
                            "  I/O del anfitrión con GUI — QEMU no tiene ningún "
                            "dispositivo de bloque abierto",
                            style="yellow",
                        )
                    )
                else:
                    lines.append(
                        Text(
                            "  I/O con GUI y disco de datos: no se puede "
                            "atribuir solo al anfitrión",
                            style="yellow",
                        )
                    )
            lines.append(
                Text(f"wchar: {io.wchar} B    rchar: {io.rchar} B")
            )
            lines.append(Text(f"read_bytes: {io.read_bytes} B"))

        if snapshot is not None and session.before is not None:
            shmem = snapshot.shmem_kb - session.before.shmem_kb
            mem = snapshot.mem_available_kb - session.before.mem_available_kb
            lines.append(Text(f"ΔShmem: {shmem:+d} kB    ΔMemAvailable: {mem:+d} kB"))
            new_entries = sorted(
                set(snapshot.shm_entries) - set(session.before.shm_entries)
            )
            lines.append(
                Text(
                    "Entradas nuevas en /dev/shm: "
                    + (", ".join(new_entries) if new_entries else "ninguna")
                )
            )
            if snapshot.memfds is None or session.before.memfds is None:
                lines.append(Text("Memfds del guest: no medidos"))
            else:
                new_memfds = {
                    name
                    for name in (
                        set(snapshot.memfds) - set(session.before.memfds)
                    )
                    if not audit.is_host_stack_memfd(name)
                }
                lines.append(
                    Text(
                        "Memfds del guest: "
                        + (
                            ", ".join(sorted(new_memfds))
                            if new_memfds
                            else "ninguno"
                        )
                    )
                )
        else:
            lines.append(Text("Memoria: medida no disponible."))

        if warnings:
            names = ", ".join(check.vector for check in warnings)
            lines.append(Text(f"Avisos del pre-chequeo: {names}"))
        if other_sessions > 0:
            lines.append(
                Text(
                    f"Otras sesiones vivas: {other_sessions} "
                    "(su actividad entra en el delta de Shmem)",
                    style="yellow",
                )
            )
        lines.append(Text(f"Wipe: {'sí' if wipe else 'no'}"))
        self.text = "\n".join(line.plain for line in lines)
        self.update(Text("\n").join(lines))


class ReportView(Static):
    """Informe completo, seleccionable y desplazable."""

    def __init__(
        self, report: str, *, id: str | None = None
    ) -> None:
        super().__init__(id=id or "report-view", markup=False, expand=True)
        self.report = report
        self.update(report)


@dataclass
class SessionRuntime:
    """Una VM de la TUI: sesión, consola, perfil y estado de la pestaña."""

    id: str
    label: str
    profile: profiles.Profile
    session: Session
    warnings: tuple[CheckResult, ...]
    console: SerialConsole
    wipe: bool = False
    report: str | None = None
    state: str = "viva"
    finishing: bool = False
    removing: bool = False
    pending_remove: bool = False


class ConsoleView(Static):
    """Pantalla VT100 de una sesión y captura de teclado hacia el guest."""

    can_focus = True

    def __init__(self, console: SerialConsole, *, id: str | None = None) -> None:
        super().__init__(id=id, markup=False, expand=True)
        self.console = console
        self.capture = False

    def enter_capture(self) -> None:
        self.capture = True
        self.add_class("capturing")
        self.border_title = "CAPTURA — Ctrl-G menú"
        self.focus()

    def leave_capture(self) -> None:
        self.capture = False
        self.remove_class("capturing")
        self.border_title = "Consola serie"

    def _render_rows(self) -> Text:
        snapshot: ConsoleSnapshot = self.console.snapshot()
        cursor_x, cursor_y = snapshot.cursor
        text = Text()
        for y, row in enumerate(snapshot.rows):
            x = 0
            for span in row:
                style = span_style(span)
                if y == cursor_y and x <= cursor_x < x + len(span.text):
                    offset = cursor_x - x
                    before = span.text[:offset]
                    cursor = span.text[offset : offset + 1]
                    after = span.text[offset + 1 :]
                    if before:
                        text.append(before, style=style)
                    text.append(cursor, style=(style + " reverse").strip())
                    if after:
                        text.append(after, style=style)
                else:
                    text.append(span.text, style=style)
                x += len(span.text)
            if y < len(snapshot.rows) - 1:
                text.append("\n")
        return text

    def render(self) -> Text:
        return self._render_rows()

    def refresh_view(self) -> None:
        self.refresh()

    def send(self, data: bytes) -> bool:
        """Envía al guest y repinta ya, sin esperar al timer de la consola."""
        sent = self.console.send(data)
        self.refresh_view()
        return sent

    def on_key(self, event) -> None:
        if not self.capture:
            return
        data = key_to_bytes(event.key, getattr(event, "character", None))
        if data:
            self.send(data)
        event.stop()
        event.prevent_default()


class SessionTab(Vertical):
    """Pestaña de una VM: cabecera, panel, consola y, al cerrar, informe."""

    def __init__(self, runtime: SessionRuntime) -> None:
        super().__init__(id=f"session-tab-{runtime.id}")
        self.runtime = runtime
        self._report_view: ReportView | None = None

    def compose(self):
        suffix = self.runtime.id
        yield Static("", id=f"session-header-{suffix}", classes="session-header")
        yield Static("", id=f"session-meta-{suffix}", classes="session-meta")
        yield SessionPanel(id=f"session-panel-{suffix}")
        yield ConsoleView(
            self.runtime.console, id=f"console-{suffix}"
        )
        yield ReportView("", id=f"report-view-{suffix}")
        yield Static("", id=f"console-hint-{suffix}", classes="console-hint")

    def on_mount(self) -> None:
        self._report_view = self.query_one(ReportView)
        self._report_view.display = False
        self.query_one(SessionPanel).focus()
        self.refresh_view(None)
        self.refresh_console()

    # -- estado -----------------------------------------------------------

    @property
    def symbol(self) -> str:
        return _STATE_SYMBOL.get(self.runtime.state, "●")

    def _state(self) -> str:
        if self.runtime.report is not None:
            return "terminada"
        if self.runtime.finishing:
            return "cerrando"
        if not self.runtime.session.started:
            return "arrancando"
        if self.runtime.session.poll() is not None:
            return "terminada"
        return "viva"

    def _uptime(self) -> float:
        if self.runtime.session.started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self.runtime.session.started_at)

    def _header(self) -> str:
        session = self.runtime.session
        return (
            f"{self.runtime.id} · {self.runtime.label} · "
            f"{self.runtime.state} · {self._uptime():.1f} s · "
            f"{session.config.mem_mb} MB · PID "
            f"{session.pid if session.started else 0}"
        )

    def _meta(self) -> str:
        console = self.runtime.console
        if console.bytes_received:
            console_state = "consola con salida"
        elif self.runtime.session.config.image:
            console_state = "consola sin datos"
        else:
            console_state = (
                "consola sin datos (el guest no tiene getty en ttyS0)"
            )
        parts = [console_state]
        if self.runtime.session.config.image:
            parts.append("ventana GTK")
        return " · ".join(parts)

    def refresh_view(self, snapshot: audit.Snapshot | None) -> None:
        self.runtime.state = self._state()
        try:
            panel = self.query_one(SessionPanel)
        except NoMatches:
            # El timer puede adelantar al montaje de los hijos de la pestaña.
            return
        panel.state = self.runtime.state
        panel.show(
            self.runtime.session,
            snapshot,
            self.runtime.warnings,
            wipe=self.runtime.wipe,
            other_sessions=max(0, self._other_sessions()),
        )
        try:
            self.query_one(f"#session-header-{self.runtime.id}", Static).update(
                self._header()
            )
            self.query_one(f"#session-meta-{self.runtime.id}", Static).update(
                self._meta()
            )
            hint = self.query_one(f"#console-hint-{self.runtime.id}", Static)
            if self.runtime.report is not None:
                hint.display = True
                hint.update("informe de la sesión · q cierra la pestaña")
            elif self._console_view().capture:
                # Con captura el borde verde y su título ya lo dicen todo.
                hint.display = False
            else:
                hint.display = True
                hint.update("enter: consola · Ctrl-G: menú")
        except NoMatches:
            return
        if self._report_view is not None and self.runtime.report is not None:
            self._report_view.update(self.runtime.report)
            self._report_view.display = True
            self._console_view().display = False

    def _other_sessions(self) -> int:
        app = self.app
        sessions = getattr(app, "sessions", {})
        return len(
            [
                key
                for key, runtime in sessions.items()
                if key != self.runtime.id and runtime.session.running
            ]
        )

    def _console_view(self) -> ConsoleView:
        return self.query_one(ConsoleView)

    def refresh_console(self) -> None:
        if self.runtime.report is not None:
            return
        try:
            self._console_view().refresh_view()
        except NoMatches:
            return

    def send_ctrl_c(self) -> None:
        self._console_view().send(b"\x03")

    def mark_finishing(self) -> None:
        self.runtime.finishing = True
        self.runtime.state = "cerrando"
        try:
            self.query_one(f"#session-header-{self.runtime.id}", Static).update(
                self._header()
            )
            self.query_one(SessionPanel).update_state("cerrando")
        except NoMatches:
            return

    def show_report(self, report: str) -> None:
        self.runtime.finishing = False
        self.runtime.report = report
        self.runtime.state = "terminada"
        try:
            if self._report_view is None:
                self._report_view = self.query_one(ReportView)
            self._report_view.update(report)
            self._report_view.display = True
            self._console_view().display = False
            self.query_one(f"#session-header-{self.runtime.id}", Static).update(
                self._header()
            )
            hint = self.query_one(f"#console-hint-{self.runtime.id}", Static)
            hint.display = True
            hint.update("informe de la sesión · q cierra la pestaña")
            self.query_one(SessionPanel).update_state("terminada")
        except NoMatches:
            # La pestaña aún se está montando: `on_mount` pintará el informe.
            return

    def toggle_wipe(self) -> bool:
        self.runtime.wipe = not self.runtime.wipe
        self.refresh_view(None)
        return self.runtime.wipe

    def enter_capture(self) -> None:
        if self.runtime.report is not None:
            return
        self._console_view().enter_capture()

    def leave_capture(self) -> None:
        self._console_view().leave_capture()

    def on_key(self, event) -> None:
        if event.key == "enter" and not self._console_view().capture:
            self.enter_capture()
            event.stop()
            event.prevent_default()
