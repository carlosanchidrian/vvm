"""Widgets de la TUI, sin lógica de procesos ni de ficheros del sistema."""

from __future__ import annotations

import time
from collections.abc import Sequence

from rich.text import Text
from textual.widgets import DataTable, Static

from vvm import audit
from vvm.host_check import CheckResult
from vvm.session import IOStats, Session

_STATUS_STYLE = {"OK": "green", "WARN": "yellow", "FAIL": "bold red"}


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

    def __init__(self) -> None:
        super().__init__(id="session-panel")
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
    ) -> None:
        """Compone el panel con la última muestra; nunca lee ficheros él mismo."""
        io: IOStats | None = session.io()
        elapsed = 0.0
        if session.started_at is not None:
            elapsed = max(0.0, time.monotonic() - session.started_at)
        pid = session.pid if session.started else 0
        lines: list[Text] = [
            Text(
                f"Estado: {self.state}    Sesión: {elapsed:6.1f} s    PID: {pid}"
            )
        ]
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
        lines.append(Text(f"Wipe: {'sí' if wipe else 'no'}"))
        self.text = "\n".join(line.plain for line in lines)
        self.update(Text("\n").join(lines))


class ReportView(Static):
    """Informe completo, seleccionable y desplazable."""

    def __init__(self, report: str) -> None:
        super().__init__(id="report-view", markup=False, expand=True)
        self.report = report
        self.update(report)
