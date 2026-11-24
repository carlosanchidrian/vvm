"""Aplicación Textual de VVM: front-end de perfiles, sesión e informe.

Este módulo es el único de `vvm.tui` que importa Textual al cargarse; el CLI lo
importa solo después de comprobar el terminal y el extra.
"""

from __future__ import annotations

import math
import os
import tempfile
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from textual.app import App
from textual.widgets import DataTable, Static

from vvm import audit, host_check, images, profiles, vm
from vvm.host_check import CheckResult
from vvm.session import Session

from .screens import (
    DeleteProfileScreen,
    PreCheckScreen,
    ProfileFormScreen,
    ProfilesScreen,
    ReportScreen,
    SessionScreen,
)


class VvmApp(App):
    """TUI de VVM. La lógica de VM vive en `vvm.session` y `vvm.audit`."""

    CSS_PATH = "app.tcss"
    TITLE = "VVM — Volatile Virtual Machines"

    def __init__(
        self,
        *,
        profile: str | None = None,
        profile_name: str | None = None,
        profiles_path: str | Path | None = None,
        qemu: str | None = None,
        refresh_interval: float | None = None,
        session_factory: Any | None = None,
        auto_exit_on_finish: bool = True,
        exit_delay: float = 1.0,
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
        self.auto_exit_on_finish = auto_exit_on_finish
        self.exit_delay = max(0.0, exit_delay)
        self.profiles_file: profiles.Profiles | None = None
        self.current_session: Session | None = None
        self.last_report: str | None = None
        self.serial_log: str | None = None

    # -- arranque de la pantalla -----------------------------------------

    def on_mount(self) -> None:
        target = self.profiles_path or profiles.default_profiles_path()
        warning: str | None = None
        try:
            loaded = profiles.load_profiles(target)
        except profiles.ProfilesError as exc:
            if target.exists():
                self.notify(str(exc), severity="error", timeout=10)
                self.exit(2)
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

    def _runtime_log(self) -> str:
        """Log serie en tmpfs del usuario; nunca en el directorio del repo."""
        base = Path(os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir())
        path = base / "vvm" / "serie.log"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            path = Path(tempfile.gettempdir()) / "vvm" / "serie.log"
            path.parent.mkdir(parents=True, exist_ok=True)
        return str(path)

    def session_config(self, profile: profiles.Profile) -> vm.VmConfig:
        """Config del perfil con la serie en fichero para no robar el terminal."""
        log = self._runtime_log()
        self.serial_log = log
        return replace(
            profile.to_config(),
            serial_mode="file",
            serial_log=log,
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

    # -- sesión -----------------------------------------------------------

    def start_session(
        self,
        profile: profiles.Profile,
        config: vm.VmConfig,
        results: Sequence[CheckResult],
        *,
        forced: bool,
    ) -> None:
        del profile
        current = self.session_factory(
            config,
            qemu=self.qemu,
            checks=tuple(results),
            forced=forced,
            log_path=config.serial_log,
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

        self.current_session = current
        warnings = tuple(
            result for result in results if result.status != "OK"
        )
        self.push_screen(
            SessionScreen(
                current,
                warnings,
                refresh_interval=self.refresh_interval,
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
            checks=saved.checks,
            forced=saved.forced,
            extras=saved.extras,
        )
        self.last_report = report
        self.push_screen(ReportScreen(report))

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
                if isinstance(widget, Static):
                    try:
                        chunks.append(str(widget.render()))
                    except Exception:  # pragma: no cover - widget sin render
                        pass
                elif isinstance(widget, DataTable):
                    for row_index in range(widget.row_count):
                        try:
                            row = widget.get_row_at(row_index)
                        except Exception:  # pragma: no cover - tabla vacía
                            continue
                        chunks.append(" | ".join(str(cell) for cell in row))
        return "\n".join(chunk for chunk in chunks if chunk)

    def on_unmount(self) -> None:
        current = self.current_session
        if current is None:
            return
        try:
            if current.running:
                current.stop()
            if not current.finished:
                current.finish(wipe=False)
        except (OSError, RuntimeError, ValueError):
            current.close()


# Alias razonables para quien importe la app desde una prueba o un script.
VvmTUI = VvmApp
VvmTui = VvmApp
App = VvmApp
