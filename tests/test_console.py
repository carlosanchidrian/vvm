"""Tests de la consola serie: pyte puro, socket real y tabla de teclas."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("pyte")

from vvm.console import SerialConsole, key_to_bytes, span_style  # noqa: E402


def _wait(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("no se cumplió la condición")
        time.sleep(0.01)


def _text(console: SerialConsole) -> str:
    snapshot = console.snapshot()
    return "\n".join(
        "".join(span.text for span in row) for row in snapshot.rows
    )


class _DrainServer:
    """Acepta la conexión, tarda en leer y luego consume todo el flujo."""

    def __init__(self, path: Path, expected: int, delay: float = 0.2) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.path = path
        self.expected = expected
        self.delay = delay
        self.received = bytearray()
        self.done = threading.Event()
        self.connected = threading.Event()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        self.socket.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        self.connected.set()
        with connection:
            time.sleep(self.delay)
            while len(self.received) < self.expected:
                try:
                    chunk = connection.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                self.received.extend(chunk)
            self.done.set()

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass
        self.thread.join(timeout=2)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class _EchoServer:
    def __init__(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.path = path
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        self.socket.listen(1)
        self._stop = threading.Event()
        self._connection: socket.socket | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        self._connection = connection
        with connection:
            while not self._stop.is_set():
                try:
                    data = connection.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                try:
                    connection.sendall(data)
                except OSError:
                    return

    def close(self) -> None:
        self._stop.set()
        connection = self._connection
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        try:
            self.socket.close()
        except OSError:
            pass
        self.thread.join(timeout=2)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class _SplitServer:
    """Envía un carácter UTF-8 en dos escrituras separadas."""

    def __init__(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        self.path = path
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.bind(str(path))
        self.socket.listen(1)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        with connection:
            try:
                connection.sendall(b"\xc3")
                time.sleep(0.1)
                connection.sendall(b"\xa1")
                time.sleep(0.2)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self.socket.close()
        except OSError:
            pass
        self.thread.join(timeout=2)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def test_key_to_bytes_cubre_la_tabla_y_utf8():
    assert key_to_bytes("enter") == b"\r"
    assert key_to_bytes("tab") == b"\t"
    assert key_to_bytes("shift+tab") == b"\x1b[Z"
    assert key_to_bytes("backspace") == b"\x7f"
    assert key_to_bytes("escape") == b"\x1b"
    assert key_to_bytes("up") == b"\x1b[A"
    assert key_to_bytes("down") == b"\x1b[B"
    assert key_to_bytes("right") == b"\x1b[C"
    assert key_to_bytes("left") == b"\x1b[D"
    assert key_to_bytes("home") == b"\x1b[H"
    assert key_to_bytes("end") == b"\x1b[F"
    assert key_to_bytes("pageup") == b"\x1b[5~"
    assert key_to_bytes("pagedown") == b"\x1b[6~"
    assert key_to_bytes("delete") == b"\x1b[3~"
    assert key_to_bytes("f1") == b"\x1bOP"
    assert key_to_bytes("f12") == b"\x1b[24~"
    assert key_to_bytes("ctrl+c") == b"\x03"
    assert key_to_bytes("ctrl+a") == b"\x01"
    assert key_to_bytes("ctrl+space") == b"\x00"
    assert key_to_bytes("a") == b"a"
    assert key_to_bytes("á") == "á".encode()
    assert key_to_bytes("á", "á") == "á".encode()
    assert key_to_bytes("desconocida") == b""


def test_feed_interpreta_la_pantalla_y_conserva_estilos(tmp_path: Path):
    console = SerialConsole(tmp_path / "no-existe.sock", cols=20, rows=4)
    try:
        console.feed(b"~ # ls\r\n")
        assert "~ # ls" in _text(console)
        assert console.snapshot().cursor == (0, 1)

        console.feed(b"\x1b[31mrojo\x1b[0m")
        snapshot = console.snapshot()
        spans = [span for row in snapshot.rows for span in row]
        red = [span for span in spans if "rojo" in span.text]
        assert red
        assert red[0].fg == "red"
        assert "red" in span_style(red[0])
    finally:
        console.stop()


def test_feed_utf8_partido_no_tumba_el_hilo(tmp_path: Path):
    console = SerialConsole(tmp_path / "no-existe.sock")
    data = "aá".encode("utf-8")
    try:
        console.feed(data[:2])
        console.feed(data[2:])
        assert "aá" in _text(console)
        assert console.bytes_received == len(data)
    finally:
        console.stop()


def test_socket_entrega_utf8_partido_sin_tumbar_el_hilo(tmp_path: Path):
    path = tmp_path / "serie.sock"
    console = SerialConsole(path)
    server = _SplitServer(path)
    try:
        _wait(lambda: console.bytes_received >= 2)
        assert "á" in _text(console)
        assert "�" not in _text(console)
    finally:
        console.stop()
        server.close()


def test_eco_visible_en_menos_de_0_2s(tmp_path: Path):
    path = tmp_path / "serie.sock"
    console = SerialConsole(path)
    server = _EchoServer(path)
    try:
        _wait(lambda: console.connected)
        started = time.monotonic()
        assert console.send(b"a")
        while "a" not in _text(console):
            if time.monotonic() - started > 0.2:
                raise AssertionError("el eco tardó más de 0,2 s")
            time.sleep(0.01)
        assert time.monotonic() - started <= 0.2
    finally:
        console.stop()
        server.close()


def test_socket_eco_y_stop_une_el_hilo(tmp_path: Path):
    path = tmp_path / "serie.sock"
    baseline = threading.active_count()
    console = SerialConsole(path)
    server = _EchoServer(path)
    try:
        _wait(lambda: console.connected)
        assert console.send(b"hola")
        _wait(lambda: console.bytes_received >= 4)
        assert "hola" in _text(console)
    finally:
        console.stop()
        server.close()

    assert threading.active_count() <= baseline
    assert not console.connected


def test_reconecta_tras_cerrar_el_servidor(tmp_path: Path):
    path = tmp_path / "serie.sock"
    console = SerialConsole(path)
    first = _EchoServer(path)
    second = None
    try:
        _wait(lambda: console.connected)
        assert console.send(b"uno")
        _wait(lambda: console.bytes_received >= 3)

        first.close()
        _wait(lambda: not console.connected)

        second = _EchoServer(path)
        _wait(lambda: console.connected)
        sent = console.bytes_received
        assert console.send(b"dos")
        _wait(lambda: console.bytes_received >= sent + 3)
        assert "dos" in _text(console)
    finally:
        console.stop()
        first.close()
        if second is not None:
            second.close()


def test_envio_grande_no_bloqueante_conserva_los_bytes(tmp_path: Path):
    payload = b"x" * (1024 * 1024)
    path = tmp_path / "serie.sock"
    server = _DrainServer(path, len(payload))
    console = SerialConsole(path)
    try:
        _wait(lambda: console.connected)
        assert server.connected.wait(2)
        assert console.send(payload) is True
        time.sleep(0.1)
        assert console.connected is True
        assert server.done.wait(5)
        assert bytes(server.received) == payload
    finally:
        console.stop()
        server.close()


def test_send_sin_conexion_devuelve_false(tmp_path: Path):
    console = SerialConsole(tmp_path / "no-existe.sock")
    try:
        assert console.send(b"x") is False
        assert console.send(b"") is True
        assert console.bytes_received == 0
    finally:
        console.stop()
        console.stop()
