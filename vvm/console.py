"""Consola serie bidireccional de la TUI: socket, VT100 y teclas.

Este es el único módulo que importa `pyte`. `SerialConsole` no sabe nada de
Textual: recibe bytes del socket, los interpreta en una pantalla VT100 y
entrega una instantánea con estilos. La UI solo pinta y traduce teclas.
"""

from __future__ import annotations

import codecs
import select
import socket
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pyte

# Reintento de conexión mientras QEMU todavía no ha creado el socket.
RETRY_SECONDS = 0.1
# Tiempo máximo de espera del hilo al cerrar.
STOP_JOIN_SECONDS = 2.0


@dataclass(frozen=True)
class Span:
    """Tramo de texto con el mismo estilo en una fila de la consola."""

    text: str
    fg: str | int | None = None
    bg: str | int | None = None
    bold: bool = False
    italics: bool = False
    underscore: bool = False
    strikethrough: bool = False
    reverse: bool = False
    blink: bool = False


@dataclass(frozen=True)
class ConsoleSnapshot:
    """Filas ya renderizadas y posición del cursor."""

    rows: tuple[tuple[Span, ...], ...]
    cursor: tuple[int, int]


_KEY_BYTES: dict[str, bytes] = {
    "enter": b"\r",
    "tab": b"\t",
    "shift+tab": b"\x1b[Z",
    "backspace": b"\x7f",
    "escape": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "space": b" ",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
    "f5": b"\x1b[15~",
    "f6": b"\x1b[17~",
    "f7": b"\x1b[18~",
    "f8": b"\x1b[19~",
    "f9": b"\x1b[20~",
    "f10": b"\x1b[21~",
    "f11": b"\x1b[23~",
    "f12": b"\x1b[24~",
}


def key_to_bytes(key: str, character: str | None = None) -> bytes:
    """Traduce una tecla de Textual a los bytes que espera el guest.

    Es una función pura y con tabla: la UI no inventa secuencias. `ctrl+<letra>`
    se convierte en el byte de control (`ctrl+c` → ``0x03``); un imprimible se
    codifica en UTF-8.
    """
    mapped = _KEY_BYTES.get(key)
    if mapped is not None:
        return mapped
    if key.startswith("ctrl+"):
        suffix = key[5:]
        if len(suffix) == 1 and suffix.isalpha():
            return bytes([ord(suffix.lower()) & 0x1F])
        if suffix in {"space", "@", "2"}:
            return b"\x00"
        if suffix == "[":
            return b"\x1b"
        if suffix == "\\":
            return b"\x1c"
        if suffix == "]":
            return b"\x1d"
        if suffix == "^":
            return b"\x1e"
        if suffix == "_":
            return b"\x1f"
        return b""
    if character:
        return character.encode("utf-8")
    if len(key) == 1:
        return key.encode("utf-8")
    return b""


_COLOR_NAMES = {
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightyellow": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
    "brown": "yellow",
}


def _color_style(value: str | int | None, *, background: bool = False) -> str | None:
    if value is None or value == "default":
        return None
    prefix = "on " if background else ""
    if isinstance(value, int):
        return f"{prefix}color({value})"
    name = _COLOR_NAMES.get(value, value)
    return f"{prefix}{name}"


def _span_style(span: Span) -> str:
    parts: list[str] = []
    fg = _color_style(span.fg)
    bg = _color_style(span.bg, background=True)
    if fg:
        parts.append(fg)
    if bg:
        parts.append(bg)
    if span.bold:
        parts.append("bold")
    if span.italics:
        parts.append("italic")
    if span.underscore:
        parts.append("underline")
    if span.strikethrough:
        parts.append("strike")
    if span.reverse:
        parts.append("reverse")
    if span.blink:
        parts.append("blink")
    return " ".join(parts)


def span_style(span: Span) -> str:
    """Estilo Rich de un tramo, público para el widget de la TUI."""
    return _span_style(span)


class SerialConsole:
    """Socket serie de QEMU conectado a una pantalla pyte.

    El hilo lector reintenta la conexión cada `RETRY_SECONDS` mientras el
    socket no exista o se caiga. `snapshot()` se lee con un lock y no toca el
    socket; `send()` escribe desde la UI. `stop()` cierra y une el hilo.
    """

    def __init__(self, path: str | Path, cols: int = 80, rows: int = 24) -> None:
        self.path = Path(path)
        self.cols = cols
        self.rows = rows
        self._screen = pyte.Screen(cols, rows)
        self._stream = pyte.Stream(self._screen)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._socket: socket.socket | None = None
        self._bytes_received = 0
        self._thread = threading.Thread(
            target=self._reader,
            name=f"vvm-console-{self.path.name}",
            daemon=True,
        )
        self._thread.start()

    # -- estado -----------------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._socket is not None

    @property
    def bytes_received(self) -> int:
        with self._lock:
            return self._bytes_received

    # -- lectura ----------------------------------------------------------

    def feed(self, data: bytes) -> None:
        """Alimenta bytes como si llegaran del socket (pruebas y lector)."""
        if not data:
            return
        with self._lock:
            text = self._decoder.decode(data, final=False)
        self._feed_decoded(text, len(data))

    def _feed_decoded(self, text: str, raw_size: int) -> None:
        if not text and not raw_size:
            return
        with self._lock:
            self._bytes_received += raw_size
            try:
                self._stream.feed(text)
            except Exception as exc:  # pragma: no cover - pyte defensivo
                print(
                    f"Aviso: la consola no pudo interpretar los datos: {exc}",
                    file=sys.stderr,
                )

    def snapshot(self) -> ConsoleSnapshot:
        """Filas renderizadas y cursor; consistente bajo lock."""
        with self._lock:
            rows: list[tuple[Span, ...]] = []
            for y in range(self.rows):
                spans: list[Span] = []
                current: Span | None = None
                for x in range(self.cols):
                    char = self._screen.buffer[y][x]
                    piece = Span(
                        text=char.data or " ",
                        fg=char.fg,
                        bg=char.bg,
                        bold=bool(getattr(char, "bold", False)),
                        italics=bool(getattr(char, "italics", False)),
                        underscore=bool(getattr(char, "underscore", False)),
                        strikethrough=bool(
                            getattr(char, "strikethrough", False)
                        ),
                        reverse=bool(getattr(char, "reverse", False)),
                        blink=bool(getattr(char, "blink", False)),
                    )
                    if current is not None and (
                        current.fg,
                        current.bg,
                        current.bold,
                        current.italics,
                        current.underscore,
                        current.strikethrough,
                        current.reverse,
                        current.blink,
                    ) == (
                        piece.fg,
                        piece.bg,
                        piece.bold,
                        piece.italics,
                        piece.underscore,
                        piece.strikethrough,
                        piece.reverse,
                        piece.blink,
                    ):
                        current = Span(
                            text=current.text + piece.text,
                            fg=current.fg,
                            bg=current.bg,
                            bold=current.bold,
                            italics=current.italics,
                            underscore=current.underscore,
                            strikethrough=current.strikethrough,
                            reverse=current.reverse,
                            blink=current.blink,
                        )
                    else:
                        if current is not None:
                            spans.append(current)
                        current = piece
                if current is not None:
                    spans.append(current)
                rows.append(tuple(spans))
            return ConsoleSnapshot(
                rows=tuple(rows),
                cursor=(self._screen.cursor.x, self._screen.cursor.y),
            )

    # -- escritura --------------------------------------------------------

    def send(self, data: bytes) -> bool:
        """Envía bytes si hay conexión; `False` si el socket ya no está."""
        if not data:
            return True
        with self._lock:
            sock = self._socket
        if sock is None:
            return False
        try:
            sock.sendall(data)
            return True
        except OSError:
            self._drop_socket()
            return False

    # -- ciclo del hilo ---------------------------------------------------

    def _connect(self) -> socket.socket | None:
        try:
            if not self.path.exists():
                return None
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(str(self.path))
            sock.setblocking(False)
            return sock
        except OSError:
            return None

    def _drop_socket(self) -> None:
        with self._lock:
            sock = self._socket
            self._socket = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _reader(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while not self._stop.is_set():
            sock = self._connect()
            if sock is None:
                self._stop.wait(RETRY_SECONDS)
                continue
            with self._lock:
                self._socket = sock
            self._read_until_closed(sock, decoder)
            decoder.reset()
            self._drop_socket()
        self._drop_socket()

    def _read_until_closed(
        self,
        sock: socket.socket,
        decoder: codecs.IncrementalDecoder,
    ) -> None:
        while not self._stop.is_set():
            try:
                readable, _, _ = select.select([sock], [], [], RETRY_SECONDS)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                data = sock.recv(4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                return
            if not data:
                return
            text = decoder.decode(data, final=False)
            self._feed_decoded(text, len(data))

    def stop(self) -> None:
        """Cierra el socket y une el hilo lector; idempotente."""
        self._stop.set()
        self._drop_socket()
        if self._thread.is_alive() and self._thread is not threading.current_thread():
            self._thread.join(timeout=STOP_JOIN_SECONDS)
