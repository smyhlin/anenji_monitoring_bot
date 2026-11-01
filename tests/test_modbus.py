import socket
import struct
import unittest

from anenji_monitor.modbus import (
    AnenjiLink,
    LinkDown,
    ModbusError,
    build_read_request,
    crc16,
    decode_read_response,
    to_signed,
)
from anenji_monitor.simulator import FakeDongle


def frame(payload: bytes, unit: int = 1, function: int = 3) -> bytes:
    body = struct.pack(">BBB", unit, function, len(payload)) + payload
    return body + struct.pack("<H", crc16(body))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class CrcTest(unittest.TestCase):
    def test_matches_published_modbus_vector(self):
        # 01 04 00 00 00 01 -> CRC 0xCA31, sent little endian as 31 CA
        self.assertEqual(struct.pack("<H", crc16(bytes.fromhex("010400000001"))).hex(), "31ca")
        self.assertEqual(struct.pack("<H", crc16(bytes.fromhex("010300000001"))).hex(), "840a")


class RequestTest(unittest.TestCase):
    def test_builds_read_holding_request(self):
        self.assertEqual(build_read_request(277, 5).hex(" "), "01 03 01 15 00 05 95 f1")

    def test_rejects_impossible_counts(self):
        with self.assertRaises(ValueError):
            build_read_request(0, 0)
        with self.assertRaises(ValueError):
            build_read_request(0, 126)
        with self.assertRaises(ValueError):
            build_read_request(0x10000, 1)


class SignTest(unittest.TestCase):
    def test_two_complement(self):
        self.assertEqual(to_signed(0xFFFF), -1)
        self.assertEqual(to_signed(0xFFF9), -7)
        self.assertEqual(to_signed(0x8000), -32768)
        self.assertEqual(to_signed(0x7FFF), 32767)
        self.assertEqual(to_signed(0x0000), 0)


class DecodeTest(unittest.TestCase):
    def test_decodes_batch_offset_from_start(self):
        values = decode_read_response(frame(struct.pack(">HHH", 523, 0xFFF9, 1)), 277, 3)
        self.assertEqual(values, {277: 523, 278: -7, 279: 1})

    def test_rejects_bad_crc(self):
        corrupted = bytearray(frame(struct.pack(">H", 523)))
        corrupted[-1] ^= 0xFF
        with self.assertRaises(ModbusError):
            decode_read_response(bytes(corrupted), 277, 1)

    def test_rejects_exception_response(self):
        body = struct.pack(">BBB", 1, 0x83, 0x02)
        with self.assertRaises(ModbusError):
            decode_read_response(body + struct.pack("<H", crc16(body)), 277, 1)

    def test_rejects_short_payload(self):
        with self.assertRaises(ModbusError):
            decode_read_response(frame(struct.pack(">H", 523)), 277, 2)

    def test_rejects_wrong_unit(self):
        with self.assertRaises(ModbusError):
            decode_read_response(frame(struct.pack(">H", 523), unit=7), 277, 1)


class LinkTest(unittest.TestCase):
    def setUp(self):
        self.notify_port = free_port()
        self.dongle = FakeDongle(notify_port=self.notify_port).start()
        self.link = AnenjiLink(
            "127.0.0.1",
            listen_port=free_port(),
            notify_port=self.notify_port,
            connect_timeout=5.0,
            notify_interval=0.2,
            request_timeout=2.0,
        )

    def tearDown(self):
        self.link.close()
        self.dongle.stop()

    def test_reads_batch_over_dial_back_socket(self):
        values = self.link.read(215, 5)
        self.assertEqual(values, {215: 260, 216: -168, 217: -436, 218: 3605, 219: 149})
        self.assertTrue(self.dongle.notifications)

    def test_second_read_reuses_connection(self):
        self.link.read(203, 1)
        self.link.read(205, 1)
        self.assertEqual(len(self.dongle.notifications), 1)

    def test_reset_drops_session_but_keeps_link_usable(self):
        self.link.read(229, 1)
        self.link.reset()
        self.assertFalse(self.link.connected)
        self.assertEqual(self.link.read(229, 1), {229: 75})
        self.assertEqual(len(self.dongle.notifications), 2)

    def test_close_is_final(self):
        self.link.read(229, 1)
        self.link.close()
        with self.assertRaises(LinkDown):
            self.link.read(229, 1)

    def test_reconnects_after_dongle_drops(self):
        self.assertEqual(self.link.read(229, 1), {229: 75})
        self.dongle.stop()
        with self.assertRaises(LinkDown):
            self.link.read(229, 1)
        self.assertFalse(self.link.connected)
        self.link.close()


if __name__ == "__main__":
    unittest.main()
