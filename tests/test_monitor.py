import asyncio
import socket
import time
import unittest

from anenji_monitor import render
from anenji_monitor.alerts import Alerts
from anenji_monitor.battery import SOURCE_VOLTAGE, Curve, SocEstimator
from anenji_monitor.config import AlertsConfig, InverterConfig
from anenji_monitor.modbus import AnenjiLink, ModbusError, backoff_delay
from anenji_monitor.poller import MonitorState, Poller, Reading
from anenji_monitor.registers import (
    Register,
    RegisterMap,
    RegisterMapError,
    grid_present,
)
from anenji_monitor.simulator import FakeDongle

REGISTERS = RegisterMap.load("registers.toml")
GRID_UP = {202: 2300, 203: 5000}
GRID_DOWN = {202: 0, 203: 0}


def make_map(*registers: Register) -> RegisterMap:
    return RegisterMap(registers)


def reg(address: int, key: str, scale: float = 1, group: str = "grid", unit: str = "") -> Register:
    return Register(address, key, key.replace("_", " "), unit, scale, group)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def reading(raw: dict[int, int], timestamp: float) -> Reading:
    return Reading(timestamp=timestamp, values=REGISTERS.convert_all(raw), raw=raw)


def make_state(raw: dict[int, int]) -> MonitorState:
    state = MonitorState()
    state.latest = reading(raw, time.time())
    state.last_success = state.latest.timestamp
    return state


class RegisterMapTest(unittest.TestCase):
    def test_scales_raw_values(self):
        registers = make_map(reg(215, "battery_voltage", 0.1), reg(229, "battery_soc"))
        self.assertEqual(
            registers.convert_all({215: 260, 229: 75}),
            {"battery_voltage": 26.0, "battery_soc": 75.0},
        )

    def test_ignores_addresses_outside_the_profile(self):
        registers = make_map(reg(215, "battery_voltage", 0.1))
        self.assertEqual(registers.convert_all({215: 10, 999: 5}), {"battery_voltage": 1.0})

    def test_formats_with_scale_derived_precision(self):
        registers = make_map(
            reg(1, "volts", 0.1, unit="В"),
            reg(2, "centi", 0.01, unit="Гц"),
            reg(3, "watts", 10, unit="Вт"),
            reg(4, "plain", 1, unit="%"),
        )
        values = {"volts": 229.9, "centi": 50.0, "watts": 3880, "plain": 75.0}
        self.assertEqual(registers.format(registers.by_key["volts"], values), "229.9 В")
        self.assertEqual(registers.format(registers.by_key["centi"], values), "50.00 Гц")
        self.assertEqual(registers.format(registers.by_key["watts"], values), "3880 Вт")
        self.assertEqual(registers.format(registers.by_key["plain"], values), "75 %")

    def test_missing_value_renders_dash(self):
        registers = make_map(reg(1, "volts", 0.1, unit="В"))
        self.assertEqual(registers.format(registers.by_key["volts"], {}), "—")

    def test_unsigned_registers_keep_high_bit(self):
        registers = make_map(
            Register(200, "status_flags", "Прапорці", "", 1, "diagnostics", signed=False)
        )
        self.assertEqual(registers.convert_all({200: -20480}), {"status_flags": 45056.0})

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(RegisterMapError):
            make_map(reg(1, "same"), reg(2, "same"))

    def test_rejects_duplicate_addresses(self):
        with self.assertRaises(RegisterMapError):
            make_map(reg(1, "a"), reg(1, "b"))

    def test_loads_shipped_profile(self):
        self.assertGreater(len(REGISTERS.registers), 10)
        self.assertEqual(REGISTERS.by_key["battery_voltage"].address, 215)

    def test_merges_nearby_addresses_into_one_request(self):
        registers = make_map(*[reg(address, f"r{address}") for address in (215, 216, 217, 229)])
        self.assertEqual(registers.batches(max_gap=20, max_batch=50), ((215, 15),))

    def test_respects_max_gap_and_span(self):
        registers = make_map(*[reg(address, f"r{address}") for address in (100, 200, 210)])
        self.assertEqual(registers.batches(max_gap=20), ((100, 1), (200, 11)))
        self.assertEqual(registers.batches(max_gap=50, max_batch=5), ((100, 1), (200, 1), (210, 1)))


class GridDetectionTest(unittest.TestCase):
    def test_present_on_mains_frequency(self):
        self.assertTrue(grid_present({"grid_frequency": 50.0, "grid_voltage": 229.9}))

    def test_absent_when_both_read_zero(self):
        self.assertFalse(grid_present({"grid_frequency": 0.0, "grid_voltage": 0.0}))

    def test_present_from_voltage_when_no_frequency_register(self):
        self.assertTrue(grid_present({"grid_voltage": 230.0}))

    def test_none_without_grid_registers(self):
        self.assertIsNone(grid_present({"battery_soc": 75.0}))


class PollerTest(unittest.TestCase):
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
        self.poller = Poller(
            self.link, InverterConfig(host="127.0.0.1", poll_interval=0.01), REGISTERS
        )

    def tearDown(self):
        self.link.close()
        self.dongle.stop()

    def test_poll_once_populates_scaled_snapshot(self):
        self.assertTrue(asyncio.run(self.poller.poll_once()))
        state = self.poller.state
        self.assertAlmostEqual(state.latest.values["battery_voltage"], 26.0)
        self.assertAlmostEqual(state.latest.values["battery_current"], -16.8)
        self.assertAlmostEqual(state.latest.values["battery_soc"], 75.0)
        self.assertEqual(state.latest.raw[217], -436)

    def test_poll_once_applies_voltage_calibration(self):
        poller = Poller(
            self.link,
            InverterConfig(host="127.0.0.1", poll_interval=0.01),
            REGISTERS,
            SocEstimator(source=SOURCE_VOLTAGE, curve=Curve(((25.0, 20), (26.0, 80)))),
        )
        self.dongle._values[215] = 259  # 25.9 V -> 74 % on this curve
        self.dongle._values[229] = 75   # register disagrees
        self.assertTrue(asyncio.run(poller.poll_once()))
        self.assertEqual(poller.state.latest.values["soc"], 74)
        self.assertEqual(poller.state.latest.values["battery_soc"], 75)

    def test_poll_once_records_failure_without_raising(self):
        self.dongle.stop()
        self.link.connect_timeout = 0.2
        self.assertFalse(asyncio.run(self.poller.poll_once()))
        self.assertIsNotNone(self.poller.state.last_error)

    def test_backoff_grows_and_is_capped(self):
        self.assertEqual(backoff_delay(0), 0.0)
        self.assertEqual(backoff_delay(1, base=1.0), 1.0)
        self.assertEqual(backoff_delay(3, base=1.0), 4.0)
        self.assertEqual(backoff_delay(9, base=1.0), 30.0)

    def test_successful_read_resets_session_health(self):
        self.link.read(229, 1)
        self.assertEqual(self.link._immediate_closes, 0)

    def test_refused_session_counts_towards_backoff(self):
        self.link.read(229, 1)
        self.dongle.stop()
        self.link.connect_timeout = 0.2
        self.link.request_timeout = 0.2
        with self.assertRaises(ModbusError):
            self.link.read(229, 1)
        self.assertEqual(self.link._immediate_closes, 1)

    def test_read_cycle_retries_once_when_dongle_drops_mid_cycle(self):
        self.assertTrue(asyncio.run(self.poller.poll_once()))
        self.dongle.stop()
        self.link.request_timeout = 0.2
        self.link.connect_timeout = 0.3
        with self.assertRaises(ModbusError):
            self.poller.read_cycle()

    def test_read_registers_reads_outside_profile(self):
        self.assertEqual(
            asyncio.run(self.poller.read_registers(500, 3)), {500: 0, 501: 0, 502: 0}
        )


class RenderTest(unittest.TestCase):
    def test_status_reports_absent_grid(self):
        body = render.status(make_state({202: 0, 203: 0, 229: 75}), REGISTERS, stale_after=60)
        self.assertIn("🔴 <b>Світла немає</b>", body)

    def test_status_reports_present_grid(self):
        body = render.status(make_state(GRID_UP), REGISTERS, stale_after=60)
        self.assertIn("🟢 <b>Світло є</b>", body)
        self.assertIn("💡 230.0 В", body)
        self.assertNotIn("Світла немає", body)

    def test_status_groups_values_by_section(self):
        body = render.status(
            make_state({215: 260, 216: -168, 217: -436, 229: 75, 213: 388}),
            REGISTERS,
            stale_after=60,
        )
        self.assertIn("<blockquote>", body)
        battery_line = next(line for line in body.splitlines() if "🔋 " in line)
        self.assertIn("75% ", battery_line)
        self.assertIn("███", battery_line)
        self.assertIn("26.0 В", battery_line)
        self.assertIn("-16.8 А", battery_line)
        load_line = next(line for line in body.splitlines() if "🏠 " in line)
        self.assertIn("388 Вт", load_line)

    def test_status_shows_calibrated_and_inverter_soc(self):
        state = make_state({202: 0, 203: 0, 215: 259, 229: 78})
        state.latest.values["soc"] = 55
        body = render.status(state, REGISTERS, stale_after=60)
        self.assertIn("🔋 55% ", body)
        self.assertIn("інв. 78%", body)

    def test_battery_view_shows_register_for_comparison(self):
        state = make_state({215: 259, 229: 78})
        state.latest.values["soc"] = 55
        body = render.group(state, REGISTERS, "battery", stale_after=60)
        self.assertIn("Заряд (калібрування): <b>55 %</b>", body)
        self.assertIn("регістр 78 %", body)

    def test_low_battery_uses_empty_icon(self):
        body = render.status(make_state({202: 0, 203: 0, 229: 9}), REGISTERS, stale_after=60)
        self.assertIn("🪫 9% ", body)

    def test_status_shows_grid_state_duration(self):
        body = render.status(
            make_state(GRID_DOWN),
            REGISTERS,
            stale_after=60,
            grid_since=time.time() - (23 * 3600 + 5 * 60),
        )
        self.assertIn("Світла немає</b> · 23год 5хв", body)

    def test_status_flags_stale_data(self):
        state = make_state(GRID_UP)
        state.latest = reading(GRID_UP, time.time() - 300)
        self.assertIn("дані застаріли", render.status(state, REGISTERS, stale_after=60))

    def test_empty_state_reports_error(self):
        state = MonitorState()
        state.last_error = "LinkDown: dongle closed"
        body = render.status(state, REGISTERS, stale_after=60)
        self.assertIn("Даних ще немає", body)
        self.assertIn("LinkDown", body)

    def test_group_without_registers_explains_itself(self):
        body = render.group(make_state(GRID_UP), REGISTERS, "pv", stale_after=60)
        self.assertIn("немає цих регістрів", body)

    def test_battery_group_is_labelled(self):
        body = render.group(make_state({215: 260, 229: 75}), REGISTERS, "battery", stale_after=60)
        self.assertIn("Напруга батареї", body)
        self.assertIn("26.0 В", body)

    def test_all_registers_shows_raw_and_scaled(self):
        body = render.all_registers(make_state({215: 260, 229: 75}), REGISTERS, stale_after=60)
        self.assertIn("battery_voltage", body)
        self.assertIn("26.0 В", body)
        self.assertIn("—", body)

    def test_raw_block_flags_known_addresses(self):
        body = render.raw_block(215, {215: 260, 999: -1}, REGISTERS)
        self.assertIn("215=battery_voltage", body)
        self.assertIn("0x0104", body)


class BulbTest(unittest.TestCase):
    def test_returns_off_bulb_without_grid(self):
        self.assertEqual(render.bulb_for(REGISTERS.convert_all(GRID_DOWN)).name, "bulb_off.png")

    def test_returns_on_bulb_with_grid(self):
        self.assertEqual(render.bulb_for(REGISTERS.convert_all(GRID_UP)).name, "bulb_on.png")

    def test_returns_nothing_without_grid_registers(self):
        self.assertIsNone(render.bulb_for({"battery_soc": 75.0}))


class OfflineAlertTest(unittest.TestCase):
    def setUp(self):
        self.sent: list[str] = []
        self.state = MonitorState()
        self.alerts = Alerts(self.state, AlertsConfig(offline_after=180), self._send)

    async def _send(self, body: str) -> None:
        self.sent.append(body)

    def run_checks(self, *moments: float) -> None:
        for moment in moments:
            asyncio.run(self.alerts.check(now=moment))

    def test_offline_announced_once_then_recovery(self):
        start = self.alerts._started
        self.run_checks(start + 10, start + 200, start + 400)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Немає зв'язку", self.sent[0])

        self.state.latest = reading(GRID_UP, time.time())
        self.state.last_success = time.time()
        self.run_checks(time.time())
        self.assertEqual(len(self.sent), 2)
        self.assertIn("відновлено", self.sent[1])


if __name__ == "__main__":
    unittest.main()
