import os
import tempfile
import unittest

from anenji_monitor import stats
from anenji_monitor.store import Store


class ParseWindowTest(unittest.TestCase):
    def test_default_is_one_day(self):
        seconds, label = stats.parse_window(None)
        self.assertEqual(seconds, stats.DAY)
        self.assertEqual(label, "остання доба")

    def test_single_day(self):
        self.assertEqual(stats.parse_window("day")[1], "остання доба")

    def test_days_with_count(self):
        seconds, label = stats.parse_window("7 days")
        self.assertEqual(seconds, 7 * stats.DAY)
        self.assertEqual(label, "останні 7 днів")

    def test_compact_units(self):
        self.assertEqual(stats.parse_window("3d")[0], 3 * stats.DAY)
        self.assertEqual(stats.parse_window("2w")[0], 14 * stats.DAY)
        self.assertEqual(stats.parse_window("6m")[0], 180 * stats.DAY)
        self.assertEqual(stats.parse_window("2y")[0], 730 * stats.DAY)

    def test_ukrainian_words(self):
        self.assertEqual(stats.parse_window("2 тижні")[1], "останні 2 тижні")
        self.assertEqual(stats.parse_window("3 місяці")[1], "останні 3 місяці")

    def test_plural_forms(self):
        self.assertEqual(stats.parse_window("2 days")[1], "останні 2 дні")
        self.assertEqual(stats.parse_window("5 days")[1], "останні 5 днів")
        self.assertEqual(stats.parse_window("21 days")[1], "останні 21 день")

    def test_rejects_unknown_period(self):
        with self.assertRaises(ValueError):
            stats.parse_window("fortnight")

    def test_rejects_zero(self):
        with self.assertRaises(ValueError):
            stats.parse_window("0 days")


class SummarizeTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def test_uptime_segments_inside_window(self):
        self.store.set_grid_state(True, 8000.0)
        self.store.set_grid_state(False, 9200.0)
        self.store.set_grid_state(True, 9500.0)
        result = stats.summarize(self.store, 1000, now=10000.0)
        self.assertAlmostEqual(result.powered, 700.0)
        self.assertAlmostEqual(result.unpowered, 300.0)
        self.assertEqual(result.outages, 1)
        self.assertAlmostEqual(result.longest_outage, 300.0)
        self.assertAlmostEqual(result.average_outage, 300.0)

    def test_multiple_outages(self):
        self.store.set_grid_state(True, 5000.0)
        self.store.set_grid_state(False, 5100.0)
        self.store.set_grid_state(True, 5200.0)
        self.store.set_grid_state(False, 5300.0)
        self.store.set_grid_state(True, 5500.0)
        result = stats.summarize(self.store, 500, now=5500.0)
        self.assertEqual(result.outages, 2)
        self.assertAlmostEqual(result.longest_outage, 200.0)
        self.assertAlmostEqual(result.unpowered, 300.0)

    def test_window_without_history(self):
        result = stats.summarize(self.store, 3600, now=10000.0)
        self.assertEqual(result.powered, 0.0)
        self.assertEqual(result.outages, 0)
        self.assertIsNone(result.soc)

    def test_includes_soc_summary(self):
        self.store.set_grid_state(True, 9000.0)
        self.store.record_soc(9100.0, 60.0)
        self.store.record_soc(9200.0, 40.0)
        result = stats.summarize(self.store, 1000, now=10000.0)
        self.assertEqual(result.soc.minimum, 40.0)
        self.assertEqual(result.soc.maximum, 60.0)


class RenderStatsTest(unittest.TestCase):
    def test_renders_percentages_and_durations(self):
        body = stats.render_stats(
            stats.Stats(
                since=0.0,
                until=1000.0,
                powered=700.0,
                unpowered=300.0,
                outages=2,
                longest_outage=200.0,
                soc=None,
            ),
            "останні 7 днів",
        )
        self.assertIn("📊 <b>Статистика · останні 7 днів</b>", body)
        self.assertIn("🟢 Світло було <b>70%</b> (11хв)", body)
        self.assertIn("🔴 Без світла <b>30%</b> (5хв)", body)
        self.assertIn("⚡ Відключень: <b>2</b>", body)
        self.assertIn("⏱ Найдовше: <b>3хв</b>", body)
        self.assertIn("<blockquote>", body)

    def test_marks_partial_coverage(self):
        body = stats.render_stats(
            stats.Stats(
                since=0.0,
                until=86400.0,
                powered=0.0,
                unpowered=180.0,
                outages=1,
                longest_outage=180.0,
                soc=None,
                first_event=86220.0,
            ),
            "остання доба",
        )
        self.assertIn("покриття 3хв", body)
        self.assertIn("🔴 Без світла <b>100%</b> (3хв)", body)

    def test_renders_stable_soc_as_single_value(self):
        summary = stats.SocSummary(minimum=78.0, maximum=78.4, average=78.1, samples=10)
        body = stats.render_stats(
            stats.Stats(0.0, 1000.0, 900.0, 100.0, 1, 100.0, summary), "остання доба"
        )
        self.assertIn("🔋 Батарея: 78% (стабільно)", body)

    def test_renders_no_outages(self):
        body = stats.render_stats(
            stats.Stats(0.0, 1000.0, 1000.0, 0.0, 0, 0.0, None), "остання доба"
        )
        self.assertIn("Відключень не було", body)


if __name__ == "__main__":
    unittest.main()
