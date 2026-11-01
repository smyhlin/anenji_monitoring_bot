import datetime
import os
import sys
import tempfile
import unittest
import zipfile
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

from import_dessmonitor import (
    grid_transitions,
    hourly_soc,
    import_file,
    read_rows,
    samples_from_rows,
)

from anenji_monitor.battery import Curve, SocEstimator
from anenji_monitor.store import Store

HEADER = ["timestamp", "Grid  Voltage(V)", "Battery Voltage(V)"]
COLUMNS = "AFQ"


def write_xlsx(path: str, rows: list[list[str]]) -> None:
    def cell(column: str, index: int, value: str) -> str:
        if value and not value.replace(".", "", 1).isdigit():
            return f'<c r="{column}{index}" t="inlineStr"><is><t>{value}</t></is></c>'
        return f'<c r="{column}{index}"><v>{value}</v></c>'

    body = []
    for index, row in enumerate([HEADER] + rows, start=1):
        cells = "".join(cell(COLUMNS[position], index, value) for position, value in enumerate(row))
        body.append(f'<row r="{index}">{cells}</row>')
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<sheetData>{"".join(body)}</sheetData></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", sheet)


def sample_rows() -> list[list[str]]:
    return [
        ["2026-11-01 10:00:00", "230.0", "26.4"],
        ["2026-11-01 10:05:00", "229.8", "26.3"],
        ["2026-11-01 10:10:00", "0.0", "26.2"],
        ["2026-11-01 10:15:00", "0.0", "26.1"],
        ["2026-11-01 10:20:00", "231.0", "26.5"],
        ["2026-11-01 10:25:00", "", ""],
    ]


class ParserTest(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".xlsx")
        os.close(handle)
        write_xlsx(self.path, sample_rows())

    def tearDown(self):
        os.unlink(self.path)

    def test_reads_rows_by_header(self):
        rows = read_rows(self.path)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[0]["timestamp"], "2026-11-01 10:00:00")
        self.assertEqual(rows[0]["Grid  Voltage(V)"], "230.0")

    def test_shift_is_applied(self):
        rows = read_rows(self.path)
        samples = samples_from_rows(rows, datetime.timedelta(minutes=-60))
        first = datetime.datetime.fromtimestamp(samples[0].timestamp)
        self.assertEqual(first.strftime("%H:%M"), "09:00")

    def test_rows_without_numbers_are_skipped(self):
        rows = read_rows(self.path)
        self.assertEqual(len(samples_from_rows(rows, datetime.timedelta())), 5)

    def test_transitions_follow_voltage(self):
        samples = samples_from_rows(read_rows(self.path), datetime.timedelta())
        transitions = grid_transitions(samples)
        self.assertEqual([powered for _, powered in transitions], [True, False, True])

    def test_hourly_soc_uses_the_calibrated_curve(self):
        samples = samples_from_rows(read_rows(self.path), datetime.timedelta())
        estimator = SocEstimator(curve=Curve(((26.0, 40), (27.0, 100))))
        buckets = hourly_soc(samples, estimator)
        self.assertEqual(len(buckets), 1)
        low, high, _total, count = next(iter(buckets.values()))
        self.assertEqual(count, 5)
        self.assertAlmostEqual(low, 46.0, places=0)   # 26.1 V on that curve
        self.assertAlmostEqual(high, 70.0, places=0)  # 26.5 V


class ImportFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.xlsx = os.path.join(self.dir, "export.xlsx")
        self.db = os.path.join(self.dir, "state.db")
        write_xlsx(self.xlsx, sample_rows())
        self.shift = datetime.timedelta(minutes=-60)

    def tearDown(self):
        for name in ("export.xlsx", "state.db", "state.db-wal", "state.db-shm"):
            path = os.path.join(self.dir, name)
            if os.path.exists(path):
                os.unlink(path)
        os.rmdir(self.dir)

    def run_import(self, **kwargs):
        return import_file(self.xlsx, db_path=self.db, shift=self.shift, curve=None, **kwargs)

    def test_import_writes_events_and_hours(self):
        stats = self.run_import(dry_run=False)
        self.assertEqual(stats["samples"], 5)
        self.assertEqual(stats["imported_events"], 3)
        store = Store(self.db)
        try:
            self.assertEqual(len(store.grid_events(0, 9e9)), 3)
            self.assertIsNotNone(store.soc_summary(0, 9e9))
        finally:
            store.close()

    def test_import_is_idempotent(self):
        self.run_import(dry_run=False)
        second = self.run_import(dry_run=False)
        self.assertEqual(second["imported_events"], 0)
        self.assertEqual(second["imported_hours"], 0)

    def test_dry_run_writes_nothing(self):
        stats = self.run_import(dry_run=True)
        store = Store(self.db)
        try:
            self.assertEqual(store.grid_events(0, 9e9), [])
        finally:
            store.close()
        self.assertEqual(stats["imported_events"], 0)

    def test_live_rows_win_over_imported_hours(self):
        store = Store(self.db)
        hour = int(datetime.datetime(2026, 11, 1, 9, 0).timestamp()) // 3600 * 3600
        store.record_soc(hour + 60, 77.0)
        store.close()
        stats = self.run_import(dry_run=False)
        self.assertEqual(stats["imported_hours"], 0)

    def test_same_state_rows_are_compacted(self):
        store = Store(self.db)
        base = int(datetime.datetime(2026, 11, 1, 9, 0).timestamp())
        store.add_grid_events([(base, True), (base + 60, True), (base + 120, False)])
        store.close()
        self.run_import(dry_run=False)
        store = Store(self.db)
        try:
            states = [powered for _, powered in store.grid_events(0, 9e9)]
            for previous, current in pairwise(states):
                self.assertNotEqual(previous, current, "same-state rows must be compacted")
        finally:
            store.close()

    def test_grid_anchor_moves_back_to_last_transition(self):
        store = Store(self.db)
        store.set_grid_state(True, int(datetime.datetime(2026, 11, 1, 12, 0).timestamp()))
        store.close()
        self.run_import(dry_run=False)
        store = Store(self.db)
        try:
            powered, since = store.grid_state()
            self.assertTrue(powered)
            self.assertLess(since, datetime.datetime(2026, 11, 1, 12, 0).timestamp())
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
