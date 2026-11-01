"""Offline stand-in for the inverter dongle: UDP notify + Modbus RTU responder.

Run it to exercise the bot without hardware:

    python -m anenji_monitor.simulator --notify-port 58899

then point ``[inverter] host`` at 127.0.0.1.
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import struct
import threading
from typing import Self

from .modbus import FUNCTION_READ_HOLDING, crc16

log = logging.getLogger(__name__)

NOTIFY_PATTERN = re.compile(rb"set>server=([0-9.]+):(\d+);")

DEFAULT_VALUES: dict[int, int] = {
    200: 30724,
    201: 3,
    202: 0,
    203: 0,
    204: 0,
    205: 2299,
    206: 22,
    207: 5000,
    208: 424,
    210: 2299,
    211: 18,
    212: 5000,
    213: 388,
    214: 413,
    215: 260,
    216: -168,
    217: -436,
    218: 3605,
    219: 149,
    226: 21,
    227: 26,
    229: 75,
}


class FakeDongle:
    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        notify_port: int = 58899,
        values: dict[int, int] | None = None,
    ) -> None:
        self._values = dict(DEFAULT_VALUES)
        if values:
            self._values.update(values)
        self._udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._udp.bind((host, notify_port))
        self._udp.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="fake-dongle", daemon=True)
        self.notifications: list[tuple[str, int]] = []
        self.requests_served = 0
        self.connected = threading.Event()
        self._conn: socket.socket | None = None

    def start(self) -> FakeDongle:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._close_conn()
        self._thread.join(timeout=3)
        try:
            self._udp.close()
        except OSError:
            pass

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
            self.connected.clear()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    def _serve(self) -> None:
        conn: socket.socket | None = None
        buffer = bytearray()
        while not self._stop.is_set():
            if conn is None:
                try:
                    datagram, _ = self._udp.recvfrom(256)
                except TimeoutError:
                    continue
                except OSError:
                    return
                match = NOTIFY_PATTERN.search(datagram)
                if not match:
                    log.warning("ignoring unexpected datagram %r", datagram)
                    continue
                target = (match.group(1).decode(), int(match.group(2)))
                self.notifications.append(target)
                log.info("notified, dialling back to %s:%d", *target)
                try:
                    conn = socket.create_connection(target, timeout=2)
                except OSError as exc:
                    log.warning("dial back failed: %s", exc)
                    continue
                conn.settimeout(0.2)
                self._conn = conn
                buffer.clear()
                self.connected.set()
                continue

            try:
                chunk = conn.recv(64)
            except TimeoutError:
                continue
            except OSError:
                self._close_conn()
                conn = None
                continue
            if not chunk:
                self._close_conn()
                conn = None
                continue
            buffer += chunk
            while len(buffer) >= 8:
                request = bytes(buffer[:8])
                if crc16(request[:6]) != struct.unpack("<H", request[6:8])[0]:
                    log.warning("bad request CRC: %s", request.hex(" "))
                    buffer.clear()
                    self._close_conn()
                    conn = None
                    break
                buffer = buffer[8:]
                conn.sendall(self._respond(request))
                self.requests_served += 1

    def _respond(self, request: bytes) -> bytes:
        unit, function, start, count = struct.unpack(">BBHH", request[:6])
        if function != FUNCTION_READ_HOLDING:
            return self._exception(unit, function, 0x01)
        payload = b"".join(
            struct.pack(">h" if self._values.get(start + offset, 0) < 0 else ">H", self._values.get(start + offset, 0))
            for offset in range(count)
        )
        body = struct.pack(">BBB", unit, function, len(payload)) + payload
        return body + struct.pack("<H", crc16(body))

    def _exception(self, unit: int, function: int, code: int) -> bytes:
        body = struct.pack(">BBB", unit, function | 0x80, code)
        return body + struct.pack("<H", crc16(body))


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulated Anenji dongle")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--notify-port", type=int, default=58899)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(message)s")
    with FakeDongle(host=args.host, notify_port=args.notify_port):
        log.info("fake dongle listening for notify on %s:%d", args.host, args.notify_port)
        threading.Event().wait()


if __name__ == "__main__":
    main()
