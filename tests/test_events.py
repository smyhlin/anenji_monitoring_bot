import asyncio
import os
import tempfile
import time
import unittest

from anenji_monitor import events, text
from anenji_monitor.events import (
    BatteryWatcher,
    GridTransition,
    GridWatcher,
    SignalEngine,
)
from anenji_monitor.poller import MonitorState, Reading
from anenji_monitor.registers import RegisterMap
from anenji_monitor.store import Store

REGISTERS = RegisterMap.load("registers.toml")
GRID_UP = {202: 2300, 203: 5000}
GRID_DOWN = {202: 0, 203: 0}


def reading(raw: dict[int, int], timestamp: float) -> Reading:
    return Reading(timestamp=timestamp, values=REGISTERS.convert_all(raw), raw=raw)


class TextTest(unittest.TestCase):
    def test_duration_formats(self):
        self.assertEqual(text.format_duration(0), "0хв")
        self.assertEqual(text.format_duration(45), "менше хвилини")
        self.assertEqual(text.format_duration(25 * 60), "25хв")
        self.assertEqual(text.format_duration(19 * 3600 + 14 * 60), "19год 14хв")
        self.assertEqual(text.format_duration(2 * 3600), "2год")
        self.assertEqual(text.format_duration(2 * 86400 + 3 * 3600), "2д 3год")

    def test_grid_lost_message(self):
        at = time.mktime((2026, 11, 1, 11, 16, 0, 0, 1, -1))
        self.assertEqual(
            text.transition_message(False, at, 19 * 3600 + 14 * 60),
            "🔴 <b>11:16 Світло зникло</b>\n🕐 Воно було 19год 14хв",
        )

    def test_grid_restored_message(self):
        at = time.mktime((2026, 11, 1, 11, 42, 0, 0, 1, -1))
        self.assertEqual(
            text.transition_message(True, at, 25 * 60),
            "🟢 <b>11:42 Світло з'явилося</b>\n🕐 Його не було 25хв",
        )

    def test_battery_bar(self):
        self.assertEqual(text.battery_bar(0), "░" * 10)
        self.assertEqual(text.battery_bar(50), "█" * 5 + "░" * 5)
        self.assertEqual(text.battery_bar(75), "█" * 8 + "░" * 2)
        self.assertEqual(text.battery_bar(100), "█" * 10)

    def test_soc_icon_flips_below_quarter(self):
        self.assertEqual(text.soc_mark(75), "🔋")
        self.assertEqual(text.soc_mark(25), "🔋")
        self.assertEqual(text.soc_mark(24), "🪫")

    def test_battery_message_shows_both_socs(self):
        body = text.battery_message(50, 49.0, False, register_soc=72.0)
        self.assertIn("заряд 49%", body)
        self.assertIn("інв. 72%", body)

    def test_battery_message_marks_level(self):
        body = text.battery_message(25, 24.4, False)
        self.assertIn("🟠", body)
        self.assertIn("заряд 24%", body)
        self.assertIn("світла немає", body)

    def test_recovery_message_reports_change(self):
        body = text.recovery_message(
            title="🪫 Інвертор вимкнувся", gap=3600, powered_now=True, changed=True, soc=12
        )
        self.assertIn("Без моніторингу 1год", body)
        self.assertIn("Світло є", body)
        self.assertIn("з'явилося", body)
        self.assertIn("Батарея 12%", body)


class StoreTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_subscriptions_round_trip(self):
        self.assertTrue(self.store.add_chat(-1001, "Мрії 11-Д", 42))
        self.assertFalse(self.store.add_chat(-1001, "Мрії 11-Д", 42))
        chat = self.store.chat(-1001)
        self.assertEqual(chat.title, "Мрії 11-Д")
        self.assertTrue(chat.grid_lost and chat.grid_restored)
        self.assertTrue(chat.sync_photo and chat.battery)

    def test_toggle_and_remove(self):
        self.store.add_chat(-1001, "g", None)
        self.assertFalse(self.store.toggle(-1001, "grid_lost").grid_lost)
        self.assertFalse(self.store.toggle(-1001, "sync_photo").sync_photo)
        self.assertFalse(self.store.toggle(-1001, "battery").battery)
        self.assertTrue(self.store.chat(-1001).grid_restored)
        self.assertTrue(self.store.remove_chat(-1001))
        self.assertEqual(self.store.chats(), [])

    def test_unknown_flag_is_rejected(self):
        self.store.add_chat(-1001, "g", None)
        with self.assertRaises(ValueError):
            self.store.toggle(-1001, "bogus")

    def test_grid_state_and_events_survive_reopen(self):
        self.store.set_grid_state(False, 1000.0)
        self.store.set_last_seen(1234.0)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.grid_state(), (False, 1000.0))
        self.assertEqual(self.store.last_seen(), 1234.0)
        self.assertEqual(self.store.grid_events(0, 2000), [(1000.0, False)])

    def test_repeated_same_state_is_not_logged_twice(self):
        self.store.set_grid_state(False, 1000.0)
        self.store.set_grid_state(False, 2000.0)
        self.store.set_grid_state(False, 3000.0)
        self.assertEqual(self.store.grid_events(0, 5000), [(1000.0, False)])

    def test_soc_rollup(self):
        for offset, soc in ((0, 40.0), (60, 30.0), (120, 50.0)):
            self.store.record_soc(3600 + offset, soc)
        self.store.record_soc(7200, 10.0)
        summary = self.store.soc_summary(0, 10000)
        self.assertEqual(summary.minimum, 10.0)
        self.assertEqual(summary.maximum, 50.0)
        self.assertAlmostEqual(summary.average, 32.5)
        self.assertEqual(summary.samples, 4)

    def test_soc_summary_empty_window(self):
        self.store.record_soc(3600, 40.0)
        self.assertIsNone(self.store.soc_summary(99999, 100000))


class GridWatcherTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_first_observation_is_not_an_event(self):
        watcher = GridWatcher(self.store)
        self.assertIsNone(watcher.observe(reading(GRID_UP, 1000.0)))
        self.assertEqual(self.store.grid_state(), (True, 1000.0))

    def test_loss_reports_uptime(self):
        watcher = GridWatcher(self.store, confirm_samples=1)
        watcher.observe(reading(GRID_UP, 1000.0))
        transition = watcher.observe(reading(GRID_DOWN, 1000.0 + 19 * 3600 + 14 * 60))
        self.assertFalse(transition.powered)
        self.assertAlmostEqual(transition.previous_duration, 19 * 3600 + 14 * 60)

    def test_restore_reports_downtime(self):
        watcher = GridWatcher(self.store, confirm_samples=1)
        watcher.observe(reading(GRID_UP, 1000.0))
        watcher.observe(reading(GRID_DOWN, 2000.0))
        transition = watcher.observe(reading(GRID_UP, 2000.0 + 25 * 60))
        self.assertTrue(transition.powered)
        self.assertAlmostEqual(transition.previous_duration, 25 * 60)

    def test_transition_needs_two_samples_and_keeps_first_timestamp(self):
        watcher = GridWatcher(self.store, confirm_samples=2)
        watcher.observe(reading(GRID_UP, 1000.0))
        self.assertIsNone(watcher.observe(reading(GRID_DOWN, 1010.0)))
        transition = watcher.observe(reading(GRID_DOWN, 1020.0))
        self.assertEqual(transition.at, 1010.0)
        self.assertAlmostEqual(transition.previous_duration, 10.0)

    def test_flicker_shorter_than_confirmation_is_ignored(self):
        watcher = GridWatcher(self.store, confirm_samples=2)
        watcher.observe(reading(GRID_UP, 1000.0))
        self.assertIsNone(watcher.observe(reading(GRID_DOWN, 1010.0)))
        self.assertIsNone(watcher.observe(reading(GRID_UP, 1020.0)))
        self.assertEqual(self.store.grid_state(), (True, 1000.0))

    def test_restart_does_not_repeat_the_event(self):
        first = GridWatcher(self.store, confirm_samples=1)
        first.observe(reading(GRID_DOWN, 1000.0))
        second = GridWatcher(self.store, confirm_samples=1)
        self.assertIsNone(second.observe(reading(GRID_DOWN, 2000.0)))
        transition = second.observe(reading(GRID_UP, 1000.0 + 25 * 60))
        self.assertAlmostEqual(transition.previous_duration, 25 * 60)

    def test_rebase_adopts_state_without_an_event(self):
        watcher = GridWatcher(self.store, confirm_samples=1)
        watcher.observe(reading(GRID_UP, 1000.0))
        watcher.rebase(reading(GRID_DOWN, 2000.0))
        self.assertEqual(self.store.grid_state(), (False, 2000.0))
        self.assertIsNone(watcher.observe(reading(GRID_DOWN, 2010.0)))

    def test_rebase_keeps_the_transition_time_when_state_is_unchanged(self):
        watcher = GridWatcher(self.store, confirm_samples=1)
        watcher.observe(reading(GRID_DOWN, 1000.0))
        watcher.rebase(reading(GRID_DOWN, 5000.0))
        self.assertEqual(self.store.grid_state(), (False, 1000.0))

    def test_transition_counts_into_history(self):
        watcher = GridWatcher(self.store, confirm_samples=1)
        watcher.observe(reading(GRID_UP, 1000.0))
        watcher.observe(reading(GRID_DOWN, 2000.0))
        self.assertEqual(self.store.grid_events(0, 5000), [(1000.0, True), (2000.0, False)])


class GridRelaySettleTest(unittest.TestCase):
    """A dip shorter than the inverter's relay delay must not be published."""

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)
        self.watcher = GridWatcher(self.store, confirm_samples=1, off_settle=120)
        self.watcher.observe(reading(GRID_UP, 1000.0))

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_short_outage_is_not_published(self):
        self.assertIsNone(self.watcher.observe(reading(GRID_DOWN, 1010.0)))
        self.assertIsNone(self.watcher.observe(reading(GRID_DOWN, 1060.0)))
        self.assertIsNone(self.watcher.observe(reading(GRID_UP, 1070.0)))
        self.assertEqual(self.store.grid_state(), (True, 1000.0))

    def test_outage_published_once_the_relay_delay_elapses(self):
        self.assertIsNone(self.watcher.observe(reading(GRID_DOWN, 1010.0)))
        self.assertIsNone(self.watcher.observe(reading(GRID_DOWN, 1120.0)))
        transition = self.watcher.observe(reading(GRID_DOWN, 1130.0))
        self.assertIsNotNone(transition)
        self.assertFalse(transition.powered)
        self.assertEqual(transition.at, 1010.0, "timestamp stays the first off sample")
        self.assertEqual(transition.relay_at, 1130.0)
        self.assertIn("Реле вимкнулось о", transition.render())

    def test_restore_is_published_without_the_delay(self):
        self.watcher.observe(reading(GRID_DOWN, 1010.0))
        outage = self.watcher.observe(reading(GRID_DOWN, 1130.0))
        self.assertIsNotNone(outage)
        restore = self.watcher.observe(reading(GRID_UP, 1140.0))
        self.assertIsNotNone(restore)
        self.assertTrue(restore.powered)
        self.assertIsNone(restore.relay_at)
        self.assertAlmostEqual(restore.previous_duration, 130.0)

    def test_display_state_hides_duration_while_unconfirmed(self):
        self.assertEqual(self.watcher.display_state(), (True, 1000.0))
        self.watcher.observe(reading(GRID_DOWN, 1010.0))
        self.assertEqual(self.watcher.display_state(), (False, None))
        self.watcher.observe(reading(GRID_DOWN, 1130.0))
        self.assertEqual(self.watcher.display_state(), (False, 1010.0))

    def test_no_relay_line_without_a_settle_window(self):
        watcher = GridWatcher(self.store, confirm_samples=1)
        watcher.observe(reading(GRID_UP, 2000.0))
        transition = watcher.observe(reading(GRID_DOWN, 2010.0))
        self.assertIsNone(transition.relay_at)
        self.assertNotIn("Реле", transition.render())


class BatteryWatcherTest(unittest.TestCase):
    def observe(self, watcher: BatteryWatcher, soc: float, ts: float, powered: bool = False):
        raw = dict(GRID_UP if powered else GRID_DOWN)
        raw[229] = int(soc)
        return watcher.observe(reading(raw, ts))

    def test_first_observation_arms_only_levels_above_current_soc(self):
        watcher = BatteryWatcher((50, 25), settle_seconds=0)
        self.assertEqual(self.observe(watcher, 40, 1000.0), [])
        alerts = self.observe(watcher, 24, 1010.0)
        self.assertEqual([alert.level for alert in alerts], [25])

    def test_crossing_level_fires_once(self):
        watcher = BatteryWatcher((50, 25), settle_seconds=0)
        self.observe(watcher, 60, 1000.0)
        alerts = self.observe(watcher, 49, 1010.0)
        self.assertEqual([alert.level for alert in alerts], [50])
        self.assertEqual(self.observe(watcher, 48, 1020.0), [])

    def test_level_rearms_after_recovery(self):
        watcher = BatteryWatcher((50, 25), recovery_margin=3, settle_seconds=0)
        self.observe(watcher, 60, 1000.0)
        self.observe(watcher, 49, 1010.0)
        self.observe(watcher, 54, 1020.0)
        alerts = self.observe(watcher, 50, 1030.0)
        self.assertEqual([alert.level for alert in alerts], [50])

    def test_level_does_not_rearm_inside_margin(self):
        watcher = BatteryWatcher((50, 25), recovery_margin=3, settle_seconds=0)
        self.observe(watcher, 60, 1000.0)
        self.observe(watcher, 49, 1010.0)
        self.observe(watcher, 52, 1020.0)
        self.assertEqual(self.observe(watcher, 49, 1030.0), [])

    def test_both_levels_fire_on_a_steep_drop(self):
        watcher = BatteryWatcher((50, 25), settle_seconds=0)
        self.observe(watcher, 60, 1000.0)
        alerts = self.observe(watcher, 20, 1010.0)
        self.assertEqual([alert.level for alert in alerts], [50, 25])

    def test_alert_waits_for_the_settle_window(self):
        watcher = BatteryWatcher((50, 25), settle_seconds=600)
        self.observe(watcher, 60, 1000.0)
        self.assertEqual(self.observe(watcher, 49, 1010.0), [])
        self.assertEqual(self.observe(watcher, 49, 1010.0 + 599), [])
        alerts = self.observe(watcher, 49, 1010.0 + 600)
        self.assertEqual([alert.level for alert in alerts], [50])

    def test_transient_dip_does_not_post(self):
        watcher = BatteryWatcher((50, 25), settle_seconds=600)
        self.observe(watcher, 60, 1000.0)
        self.assertEqual(self.observe(watcher, 49, 1010.0), [])
        self.assertEqual(self.observe(watcher, 51, 1300.0), [])   # load dropped, SOC recovered
        self.assertEqual(self.observe(watcher, 49, 1600.0), [])   # timer restarted
        self.assertEqual(self.observe(watcher, 49, 1600.0 + 599), [])
        alerts = self.observe(watcher, 49, 1600.0 + 600)
        self.assertEqual([alert.level for alert in alerts], [50])

    def test_settle_applies_again_after_recovery(self):
        watcher = BatteryWatcher((50, 25), recovery_margin=3, settle_seconds=600)
        self.observe(watcher, 60, 0.0)
        self.observe(watcher, 49, 10.0)
        self.assertEqual(len(self.observe(watcher, 49, 610.0)), 1)
        self.observe(watcher, 54, 700.0)
        self.assertEqual(self.observe(watcher, 49, 800.0), [], "settle restarts after re-arm")
        self.assertEqual(len(self.observe(watcher, 49, 1400.0)), 1)

    def test_both_levels_need_their_own_window(self):
        watcher = BatteryWatcher((50, 25), settle_seconds=600)
        self.observe(watcher, 60, 0.0)
        self.observe(watcher, 20, 10.0)      # both start their timers here
        self.assertEqual([a.level for a in self.observe(watcher, 20, 610.0)], [50, 25])


class SignalEngineTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)
        self.engine = SignalEngine(
            self.store,
            GridWatcher(self.store, confirm_samples=1),
            BatteryWatcher((50, 25), settle_seconds=0),
        )

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_observe_persists_last_seen_and_soc(self):
        raw = dict(GRID_UP)
        raw[229] = 75
        self.engine.observe(reading(raw, 5000.0))
        self.assertEqual(self.store.last_seen(), 5000.0)
        self.assertEqual(self.store.soc_summary(0, 10_000).maximum, 75.0)

    def test_observe_emits_transition_and_battery_alerts(self):
        self.engine.observe(reading({**GRID_UP, 229: 60}, 1000.0))
        events_out = self.engine.observe(reading({**GRID_DOWN, 229: 49}, 1010.0))
        kinds = [type(event).__name__ for event in events_out]
        self.assertEqual(kinds, ["GridTransition", "BatteryAlert"])
        self.assertFalse(events_out[0].powered)


class StreamTest(unittest.TestCase):
    def test_delivers_each_event_once(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        store = Store(path)
        try:
            engine = SignalEngine(
                store, GridWatcher(store, confirm_samples=1), BatteryWatcher((50, 25))
            )
            state = MonitorState()
            state.latest = reading({**GRID_UP, 229: 60}, 1000.0)
            delivered = []

            async def deliver(event) -> None:
                delivered.append(event)

            async def run() -> None:
                task = asyncio.create_task(events.stream(state, engine, deliver, interval=0.01))
                await asyncio.sleep(0.05)
                state.latest = reading({**GRID_DOWN, 229: 60}, 1100.0)
                await asyncio.sleep(0.05)
                state.latest = reading({**GRID_DOWN, 229: 60}, 1200.0)
                await asyncio.sleep(0.05)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

            asyncio.run(run())
            self.assertEqual(len(delivered), 1)
            self.assertIsInstance(delivered[0], GridTransition)
            self.assertAlmostEqual(delivered[0].previous_duration, 100.0)
        finally:
            store.close()
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
