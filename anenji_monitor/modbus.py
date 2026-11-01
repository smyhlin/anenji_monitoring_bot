"""Modbus RTU client for Anenji / DessMonitor Wi-Fi dongles.

The dongle is normally a cloud client: it dials out to DessMonitor. To talk to
it locally we send a UDP datagram ``set>server=<ip>:<port>;`` to port 58899,
which makes it open a TCP connection back to us. Bare Modbus RTU frames then
travel over that socket (unit id 1, function 3, no MBAP header).
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time

log = logging.getLogger(__name__)

UDP_NOTIFY_PORT = 58899
UNIT_ID = 1
FUNCTION_READ_HOLDING = 3
MAX_REGISTERS_PER_READ = 125
# A session that the dongle dialled may already be dead (stale backlog entry); the
# first request on it answers fast when it is real, so give it a short deadline.
FIRST_REQUEST_TIMEOUT = 2.0
BACKOFF_CAP = 30.0


def backoff_delay(attempts: int, base: float = 1.0, cap: float = BACKOFF_CAP) -> float:
    """Delay before re-dialling after consecutive sessions that died with zero bytes."""
    if attempts <= 0:
        return 0.0
    return min(cap, base * 2 ** (attempts - 1))


class ModbusError(Exception):
    """Malformed or rejected Modbus exchange."""


class LinkDown(ModbusError):
    """The TCP link to the dongle is gone."""


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_read_request(start: int, count: int, unit_id: int = UNIT_ID) -> bytes:
    if not 1 <= count <= MAX_REGISTERS_PER_READ:
        raise ValueError(f"register count must be 1..{MAX_REGISTERS_PER_READ}, got {count}")
    if not 0 <= start <= 0xFFFF:
        raise ValueError(f"register address out of range: {start}")
    body = struct.pack(">BBHH", unit_id, FUNCTION_READ_HOLDING, start, count)
    return body + struct.pack("<H", crc16(body))


def to_signed(value: int) -> int:
    """Modbus holding registers carrying power flows are two's complement."""
    return value - 0x10000 if value >= 0x8000 else value


def decode_read_response(
    frame: bytes, start: int, count: int, unit_id: int = UNIT_ID
) -> dict[int, int]:
    if len(frame) < 5:
        raise ModbusError(f"response too short: {frame.hex(' ')}")
    expected_crc = struct.unpack("<H", frame[-2:])[0]
    if crc16(frame[:-2]) != expected_crc:
        raise ModbusError(f"CRC mismatch: {frame.hex(' ')}")
    if frame[0] != unit_id:
        raise ModbusError(f"unexpected unit id {frame[0]}")
    if frame[1] & 0x80:
        raise ModbusError(f"device returned exception code 0x{frame[2]:02X}")
    if frame[1] != FUNCTION_READ_HOLDING:
        raise ModbusError(f"unexpected function code {frame[1]}")
    byte_count = frame[2]
    if byte_count != 2 * count:
        raise ModbusError(f"expected {2 * count} data bytes, device sent {byte_count}")
    if len(frame) != 3 + byte_count + 2:
        raise ModbusError(f"response length {len(frame)} does not match byte count {byte_count}")
    payload = frame[3 : 3 + byte_count]
    return {
        start + index // 2: to_signed(struct.unpack(">h", payload[index : index + 2])[0])
        for index in range(0, byte_count, 2)
    }


class AnenjiLink:
    """Serialised, self-healing Modbus connection to one inverter dongle."""

    def __init__(
        self,
        host: str,
        *,
        listen_port: int = 8899,
        notify_port: int = UDP_NOTIFY_PORT,
        connect_timeout: float = 30.0,
        request_timeout: float = 5.0,
        notify_interval: float = 5.0,
        reconnect_delay: float = 1.0,
    ) -> None:
        self.host = host
        self.listen_port = listen_port
        self.notify_port = notify_port
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.notify_interval = notify_interval
        self.reconnect_delay = reconnect_delay
        self._lock = threading.Lock()
        self._server: socket.socket | None = None
        self._conn: socket.socket | None = None
        self._local_ip: str | None = None
        self._closed = False
        self.sessions = 0
        self._immediate_closes = 0
        self._received = False

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def local_ip(self) -> str:
        if self._local_ip is None:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect((self.host, self.notify_port))
                self._local_ip = probe.getsockname()[0]
            except OSError:
                self._local_ip = socket.gethostbyname(socket.gethostname())
            finally:
                probe.close()
        return self._local_ip

    def notify(self) -> None:
        command = f"set>server={self.local_ip()}:{self.listen_port};".encode()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(1.0)
            sock.sendto(command, (self.host, self.notify_port))
            try:
                reply, addr = sock.recvfrom(256)
                log.debug("UDP reply from %s: %r", addr, reply)
            except TimeoutError:
                log.debug("no UDP reply from %s (normal)", self.host)

    def read(self, start: int, count: int) -> dict[int, int]:
        request = build_read_request(start, count)
        with self._lock:
            fresh = self._conn is None
            if fresh:
                self._open()
                self._conn.settimeout(min(self.request_timeout, FIRST_REQUEST_TIMEOUT))
            else:
                self._conn.settimeout(self.request_timeout)
            self._received = False
            try:
                self._conn.sendall(request)
                frame = self._read_frame()
            except LinkDown as exc:
                self._note_session_health()
                self._drop(str(exc))
                raise
            except OSError as exc:
                self._note_session_health()
                self._drop(f"{type(exc).__name__}: {exc}")
                raise LinkDown(f"link lost while reading {start}+{count}") from exc
        self._immediate_closes = 0
        try:
            return decode_read_response(frame, start, count)
        except ModbusError:
            log.warning("bad response for %d+%d: %s", start, count, frame.hex(" "))
            raise

    def reset(self) -> None:
        """Drop the current TCP session but keep the link usable."""
        with self._lock:
            self._drop("reset requested")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._drop("closing")
            if self._server is not None:
                try:
                    self._server.close()
                except OSError:
                    pass
                self._server = None

    def _open(self) -> None:
        if self._closed:
            raise LinkDown("link is closed")
        self._drop("reconnecting")
        # The dongle serves one session at a time and starts refusing the next one
        # when redialled immediately; each zero-byte session doubles the wait.
        delay = backoff_delay(self._immediate_closes, self.reconnect_delay)
        if delay:
            time.sleep(delay)
        if self._server is None:
            self._server = self._make_server()
        deadline = time.monotonic() + self.connect_timeout
        attempts = 0
        while True:
            attempts += 1
            self.notify()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LinkDown(f"{self.host} did not connect within {self.connect_timeout:g}s")
            try:
                self._server.settimeout(min(self.notify_interval, remaining))
                conn, addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError as exc:
                if self._closed:
                    raise LinkDown("link is closed") from exc
                self._drop(f"listen socket failed: {exc}")
                self._server = self._make_server()
                continue
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for option, value in (
                (getattr(socket, "TCP_KEEPIDLE", None), 30),
                (getattr(socket, "TCP_KEEPINTVL", None), 10),
                (getattr(socket, "TCP_KEEPCNT", None), 3),
            ):
                if option is not None:
                    try:
                        conn.setsockopt(socket.IPPROTO_TCP, option, value)
                    except OSError:
                        pass
            conn.settimeout(self.request_timeout)
            self._conn = conn
            self.sessions += 1
            log.info("inverter connected from %s:%d after %d notify attempt(s)", *addr, attempts)
            return

    def _make_server(self) -> socket.socket:
        if self._closed:
            raise LinkDown("link is closed")
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server.bind(("0.0.0.0", self.listen_port))
        except OSError as exc:
            server.close()
            raise LinkDown(f"cannot listen on port {self.listen_port}: {exc}") from exc
        server.listen(1)
        return server

    def _read_frame(self) -> bytes:
        head = self._recv_exact(3)
        if head[1] & 0x80:
            return head + self._recv_exact(2)
        return head + self._recv_exact(head[2] + 2)

    def _recv_exact(self, size: int) -> bytes:
        buffer = bytearray()
        while len(buffer) < size:
            chunk = self._conn.recv(size - len(buffer))
            if not chunk:
                raise LinkDown("dongle closed the connection")
            self._received = True
            buffer += chunk
        return bytes(buffer)

    def _note_session_health(self) -> None:
        self._immediate_closes = 0 if self._received else self._immediate_closes + 1

    def _drop(self, reason: str) -> None:
        if self._conn is not None:
            log.info("dropping inverter connection: %s", reason)
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
