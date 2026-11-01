import unittest

from anenji_monitor import recovery


class ClassifyTest(unittest.TestCase):
    def classify(self, gap, last, now):
        return recovery.classify(gap=gap, last_powered=last, powered_now=now)

    def test_first_start(self):
        verdict = self.classify(None, None, True)
        self.assertEqual(verdict.kind, recovery.FIRST_START)
        self.assertTrue(verdict.worth_reporting)

    def test_quick_restart_is_silent(self):
        verdict = self.classify(120, True, True)
        self.assertEqual(verdict.kind, recovery.QUICK_RESTART)
        self.assertFalse(verdict.worth_reporting)

    def test_restart_between_thresholds(self):
        verdict = self.classify(400, True, True)
        self.assertEqual(verdict.kind, recovery.RESTART)
        self.assertTrue(verdict.worth_reporting)
        self.assertFalse(verdict.grid_changed)

    def test_restart_that_missed_a_grid_loss(self):
        verdict = self.classify(400, True, False)
        self.assertEqual(verdict.kind, recovery.RESTART)
        self.assertTrue(verdict.grid_changed)

    def test_drained_battery_when_grid_returns(self):
        verdict = self.classify(2000, False, True)
        self.assertEqual(verdict.kind, recovery.DRAINED)
        self.assertTrue(verdict.grid_changed)

    def test_long_outage_without_grid_return(self):
        self.assertEqual(self.classify(2000, False, False).kind, recovery.LONG_OUTAGE)
        self.assertEqual(self.classify(2000, True, True).kind, recovery.LONG_OUTAGE)
        self.assertEqual(self.classify(2000, True, False).kind, recovery.LONG_OUTAGE)

    def test_thresholds_are_inclusive(self):
        self.assertEqual(self.classify(300, True, True).kind, recovery.RESTART)
        self.assertEqual(self.classify(1800, False, True).kind, recovery.DRAINED)

    def test_only_a_cold_start_forces_a_photo_repaint(self):
        # A quick restart keeps the existing picture; anything past reboot_after may
        # renew it, because the chat may have been showing a stale state.
        quick = self.classify(120, True, True)
        restart = self.classify(400, True, True)
        first = self.classify(None, None, True)
        self.assertLess(quick.gap, 300)
        self.assertGreaterEqual(restart.gap, 300)
        self.assertEqual(first.kind, recovery.FIRST_START)

    def test_custom_thresholds(self):
        verdict = recovery.classify(
            gap=120, last_powered=True, powered_now=True, reboot_after=60, drained_after=100
        )
        self.assertEqual(verdict.kind, recovery.LONG_OUTAGE)


if __name__ == "__main__":
    unittest.main()
