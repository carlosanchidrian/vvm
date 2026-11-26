"""Sesiones vivas: ids, directorios, registro y guardia de grupo.

Este módulo no importa Textual ni pyte: es la pieza que comparten el CLI, la
TUI y `vvm ps`. El registro **es el directorio**: no hay demonio ni IPC, solo
un `live.json` por sesión que se lee y se poda cuando el proceso ya no es una
VM VVM viva.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import stat
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vvm import audit

RUNTIME_MODE = 0o700
_LIVE_NAME = "live.json"
_SOCKET_NAME = "serie.sock"
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _check_id(session_id: str) -> str:
    """Rechaza ids que puedan escapar del directorio de sesiones."""
    if not _SESSION_ID.match(session_id) or ".." in session_id:
        raise ValueError(f"id de sesión no válido: {session_id!r}")
    return session_id


def runtime_root() -> Path:
    """`$XDG_RUNTIME_DIR/vvm/sessions` o el temporal del sistema."""
    base = os.environ.get("XDG_RUNTIME_DIR", "")
    root = Path(base) if base else Path(tempfile.gettempdir())
    return root / "vvm" / "sessions"


def kebab_case(label: str) -> str:
    """Nombre de perfil legible convertido en parte de un id."""
    slug = re.sub(r"[^a-z0-9]+", "-", label.strip().lower()).strip("-")
    return slug or "vm"


def new_session_id(label: str, *, root: Path | None = None) -> str:
    """Genera `<perfil>-<4 hex>` y regenera si el directorio ya existe."""
    base = Path(root) if root is not None else runtime_root()
    slug = kebab_case(label)
    for _ in range(100):
        candidate = f"{slug}-{secrets.token_hex(2)}"
        if not (base / candidate).exists() and not audit.session_path_for(
            candidate
        ).exists():
            return candidate
    raise RuntimeError(
        f"No se pudo generar un id de sesión libre para {label!r} en {base}"
    )


def prepare_session_dir(
    session_id: str, *, root: Path | None = None, exist_ok: bool = True
) -> Path:
    """Crea el runtime 0700 de la sesión y borra el socket anterior."""
    directory = _ensure_session_dir(
        session_id, root=root, exist_ok=exist_ok
    )
    socket = directory / _SOCKET_NAME
    try:
        socket.unlink(missing_ok=True)
    except OSError as exc:
        print(
            f"Aviso: no se pudo borrar el socket anterior {socket}: {exc}",
            file=sys.stderr,
        )
    return directory


def _ensure_session_dir(
    session_id: str, *, root: Path | None = None, exist_ok: bool = True
) -> Path:
    """Crea el directorio de la sesión sin tocar el socket que ya exista."""
    _check_id(session_id)
    base = Path(root) if root is not None else runtime_root()
    base.mkdir(parents=True, exist_ok=True, mode=RUNTIME_MODE)
    for directory in (base.parent, base):
        try:
            os.chmod(directory, RUNTIME_MODE)
        except OSError:
            pass
    directory = base / session_id
    directory.mkdir(mode=RUNTIME_MODE, exist_ok=exist_ok)
    os.chmod(directory, RUNTIME_MODE)
    return directory


def reserve_session(
    label: str, *, root: Path | None = None
) -> tuple[str, Path]:
    """Reserva id y directorio de forma atómica (sin compartir con otra VM)."""
    for _ in range(100):
        candidate = new_session_id(label, root=root)
        try:
            directory = prepare_session_dir(
                candidate, root=root, exist_ok=False
            )
        except FileExistsError:
            continue
        return candidate, directory
    raise RuntimeError(
        f"No se pudo reservar un id de sesión libre para {label!r}"
    )


@dataclass(frozen=True)
class LiveSession:
    """Entrada de `live.json`: lo que `vvm ps` puede afirmar sin preguntar."""

    id: str
    label: str
    pid: int
    mem_mb: int
    disk_mb: int
    image: bool
    started_at: str
    serial: str
    serial_path: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def write_live(entry: LiveSession, *, root: Path | None = None) -> Path:
    """Escribe `live.json` de forma atómica tras lanzar el proceso."""
    directory = _ensure_session_dir(entry.id, root=root)
    path = directory / _LIVE_NAME
    handle, temporary = tempfile.mkstemp(
        prefix=_LIVE_NAME + ".", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(
                json.dumps(entry.to_dict(), ensure_ascii=False, indent=2) + "\n"
            )
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return path


def remove_live(session_id: str, *, root: Path | None = None) -> None:
    """Borra el registro y el socket de una sesión, sin tocar nada más."""
    _check_id(session_id)
    base = Path(root) if root is not None else runtime_root()
    directory = base / session_id
    live = directory / _LIVE_NAME
    socket = directory / _SOCKET_NAME
    for path in (live, socket):
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            print(f"Aviso: no se pudo borrar {path}: {exc}", file=sys.stderr)
    try:
        directory.rmdir()
    except OSError:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _live_from_document(document: object) -> LiveSession | None:
    if not isinstance(document, dict):
        return None
    values = (
        "id",
        "label",
        "pid",
        "mem_mb",
        "disk_mb",
        "image",
        "started_at",
        "serial",
        "serial_path",
    )
    if any(key not in document for key in values):
        return None
    if not all(
        isinstance(document[key], str)
        for key in ("id", "label", "started_at", "serial")
    ):
        return None
    if not isinstance(document["pid"], int) or isinstance(
        document["pid"], bool
    ):
        return None
    if not 0 < document["pid"] <= 2**31 - 1:
        return None
    if not isinstance(document["mem_mb"], int) or isinstance(
        document["mem_mb"], bool
    ):
        return None
    if not isinstance(document["disk_mb"], int) or isinstance(
        document["disk_mb"], bool
    ):
        return None
    if not isinstance(document["image"], bool):
        return None
    serial_path = document["serial_path"]
    if serial_path is not None and not isinstance(serial_path, str):
        return None
    try:
        _check_id(document["id"])
    except ValueError:
        return None
    return LiveSession(
        id=document["id"],
        label=document["label"],
        pid=document["pid"],
        mem_mb=document["mem_mb"],
        disk_mb=document["disk_mb"],
        image=document["image"],
        started_at=document["started_at"],
        serial=document["serial"],
        serial_path=serial_path,
    )


def read_live(path: Path) -> LiveSession | None:
    """Lee una entrada; un JSON roto queda como None, un error de E/S sube."""
    try:
        raw = path.read_text(encoding="utf-8")
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _live_from_document(document)


def _discard(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        path.parent.rmdir()
    except OSError:
        pass


def list_live(
    *,
    root: Path | None = None,
    validator: Callable[[int], bool] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> tuple[LiveSession, ...]:
    """Sesiones cuyo PID sigue vivo **y** es un QEMU VVM; poda el resto."""
    base = Path(root) if root is not None else runtime_root()
    check_qemu = validator or audit.is_vvm_qemu
    entries: list[LiveSession] = []
    try:
        directories = sorted(base.iterdir(), key=lambda path: path.name)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        if on_warning is not None:
            on_warning(f"No se pudo listar {base}: {exc}")
        return ()

    for directory in directories:
        try:
            if not stat.S_ISDIR(directory.stat().st_mode):
                continue
        except FileNotFoundError:
            continue
        except OSError as exc:
            if on_warning is not None:
                on_warning(f"No se pudo leer {directory}: {exc}")
            continue
        path = directory / _LIVE_NAME
        try:
            path.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if on_warning is not None:
                on_warning(f"No se pudo leer {path}: {exc}")
            continue
        try:
            live = read_live(path)
        except OSError as exc:
            if on_warning is not None:
                on_warning(f"No se pudo leer {path}: {exc}")
            continue
        if live is None:
            _discard(path)
            continue
        if not _pid_alive(live.pid) or not check_qemu(live.pid):
            _discard(path)
            continue
        entries.append(live)
    return tuple(sorted(entries, key=lambda entry: entry.id))


def count_others(
    exclude: str | None = None, *, root: Path | None = None
) -> int:
    """Número de sesiones vivas distintas de `exclude`."""
    return sum(
        1 for entry in list_live(root=root) if entry.id != exclude
    )


def now_utc() -> str:
    """Marca temporal del registro, en el mismo formato que los snapshots."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SessionGroup:
    """Un único guardia de señales para todas las sesiones de la TUI.

    Los manejadores solo anotan la señal: el bucle de la interfaz llama a
    `stop_all()` y cierra cada sesión en orden. `install()` y `restore()` son
    idempotentes; `install()` debe ejecutarse en el hilo principal.
    """

    SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)

    def __init__(self, sessions: Sequence[Any] | None = None) -> None:
        self.stop_signal: int | None = None
        self.sessions: list[Any] = list(sessions or ())
        self._previous: dict[int, object] = {}
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self) -> None:
        if self._installed:
            return
        previous: dict[int, object] = {}
        try:
            for signum in self.SIGNALS:
                previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
        except BaseException:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            raise
        self._previous = previous
        self._installed = True

    def restore(self) -> None:
        if not self._installed:
            return
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        self._previous = {}
        self._installed = False

    def should_stop(self) -> bool:
        """Callable inyectable en `Session` para abortar la copia del ISO."""
        return self.stop_signal is not None

    def reset(self) -> None:
        """Olvida la señal anotada (reutilización en pruebas)."""
        self.stop_signal = None

    def _handle(self, signum: int, _frame: object) -> None:
        if self.stop_signal is None:
            self.stop_signal = signum

    def stop_all(
        self,
        sessions: Sequence[Any] | None = None,
        *,
        wipe: bool = True,
    ) -> list[str | None]:
        """Para y cierra todas las sesiones; idempotente y sin dejar hijos."""
        reports: list[str | None] = []
        targets = self.sessions if sessions is None else list(sessions)
        for session in list(targets):
            try:
                if getattr(session, "running", False):
                    session.stop()
            except (OSError, RuntimeError, ValueError) as exc:
                print(
                    f"Aviso: no se pudo parar una sesión: {exc}",
                    file=sys.stderr,
                )
            try:
                if not getattr(session, "finished", False):
                    reports.append(session.finish(wipe=wipe))
                else:
                    reports.append(getattr(session, "report", None))
            except (OSError, RuntimeError, ValueError) as exc:
                print(
                    f"Aviso: no se pudo cerrar una sesión: {exc}",
                    file=sys.stderr,
                )
                reports.append(None)
        return reports
