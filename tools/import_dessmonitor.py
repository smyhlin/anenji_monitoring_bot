#!/usr/bin/env python3
"""Import a DessMonitor XLSX export into the bot's state.db.

The export samples the inverter every ~5 minutes. Grid transitions are derived
from the grid-voltage column, and the hourly SOC rollup is rebuilt from the
battery-voltage column through the same calibration curve the live bot uses, so
history and live samples stay one series.

    python tools/import_dessmonitor.py "export.xlsx" --shift-minutes -60

`--shift-minutes` corrects the inverter clock: the export timestamps come from the
device, which runs an hour ahead of local time here. `--dry-run` reports what would
be written without touching the database.
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anenji_monitor.battery import Curve, SocEstimator
from anenji_monitor.config import load
from anenji_monitor.store import Store

GRID_ON_VOLTS = 100.0
CELL_RE = re.compile(r"<c ([^>]*?)(?:/>|>(.*?)</c>)", re.DOTALL)
ROW_RE = re.compile(r"<row[^>]*>(.*?)</row>", re.DOTALL)
REF_RE = re.compile(r'r="([A-Z]+)\d+"')
TYPE_RE = re.compile(r't="(\w+)"')
VALUE_RE = re.compile(r"<v>(.*?)</v>", re.DOTALL)
TEXT_RE = re.compile(r"<t[^>]*>(.*?)</t>", re.DOTALL)


@dataclass(frozen=True)
class Sample:
    timestamp: float
    grid_volts: float
    battery_volts: float


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        raw = archive.read("xl/sharedStrings.xml").decode("utf-8")
    except KeyError:
        return []
    return ["".join(TEXT_RE.findall(item)) for item in re.findall(r"<si>(.*?)</si>", raw, re.DOTALL)]


def _column_letter(ref: str) -> str:
    match = REF_RE.search(f'r="{ref}"')
    return match.group(1) if match else ""


def read_rows(path: str | Path) -> list[dict[str, str]]:
    """Sheet rows as {header: value} dicts, using the first row as the header."""
    with zipfile.ZipFile(path) as archive:
        shared = _shared_strings(archive)
        sheet = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")

    raw_rows: list[dict[str, str]] = []
    for row_xml in ROW_RE.findall(sheet):
        values: dict[str, str] = {}
        for attrs, body in CELL_RE.findall(row_xml):
            ref = REF_RE.search(attrs)
            if ref is None:
                continue
            column = ref.group(1)
            kind = TYPE_RE.search(attrs)
            kind = kind.group(1) if kind else "n"
            body = body or ""
            if kind == "inlineStr":
                values[column] = "".join(TEXT_RE.findall(body))
            else:
                match = VALUE_RE.search(body)
                text = match.group(1) if match else ""
                if kind == "s" and text:
                    text = shared[int(text)]
                values[column] = text
        if values:
            raw_rows.append(values)

    if not raw_rows:
        return []
    header = raw_rows[0]
    return [
        {header.get(column, column): value for column, value in row.items()}
        for row in raw_rows[1:]
    ]


def _number(row: dict[str, str], *names: str) -> float | None:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def samples_from_rows(rows: list[dict[str, str]], shift: timedelta) -> list[Sample]:
    samples = []
    for row in rows:
        stamp = row.get("timestamp", "").strip()
        if not stamp:
            continue
        try:
            moment = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S") + shift
        except ValueError:
            continue
        grid = _number(row, "Grid  Voltage(V)", "Grid Voltage(V)")
        battery = _number(row, "Battery Voltage(V)")
        if grid is None or battery is None:
            continue
        samples.append(Sample(moment.timestamp(), grid, battery))
    samples.sort(key=lambda sample: sample.timestamp)
    return samples


def grid_transitions(samples: list[Sample]) -> list[tuple[float, bool]]:
    transitions: list[tuple[float, bool]] = []
    previous: bool | None = None
    for sample in samples:
        powered = sample.grid_volts >= GRID_ON_VOLTS
        if powered != previous:
            transitions.append((sample.timestamp, powered))
            previous = powered
    return transitions


def hourly_soc(samples: list[Sample], estimator: SocEstimator) -> dict[int, tuple[float, float, float, int]]:
    buckets: dict[int, tuple[float, float, float, int]] = {}
    for sample in samples:
        soc = estimator.estimate({"battery_voltage": sample.battery_volts})
        if soc is None:
            continue
        hour = int(sample.timestamp) // 3600 * 3600
        low, high, total, count = buckets.get(hour, (soc, soc, 0.0, 0))
        buckets[hour] = (min(low, soc), max(high, soc), total + soc, count + 1)
    return buckets


def import_file(
    path: str | Path,
    *,
    db_path: str | Path,
    shift: timedelta,
    curve: Curve | None,
    dry_run: bool,
) -> dict[str, object]:
    rows = read_rows(path)
    samples = samples_from_rows(rows, shift)
    if not samples:
        raise SystemExit("no usable rows in the export")

    estimator = SocEstimator(curve=curve)
    transitions = grid_transitions(samples)
    hours = hourly_soc(samples, estimator)

    store = Store(db_path)
    try:
        event_floor, hour_floor = store.earliest_history()
        imported_events = 0 if dry_run else store.add_grid_events(transitions)
        # An hour that already holds live samples keeps them: merging partial
        # buckets from two sources would double-count the same readings.
        fresh_hours = {
            hour: values
            for hour, values in hours.items()
            if hour_floor is None or hour < hour_floor
        }
        imported_hours = 0 if dry_run else store.add_soc_hours(fresh_hours)

        compacted = 0 if dry_run else store.compact_grid_events()
        state, since = store.grid_state()
        last_event = transitions[-1] if transitions else None
        reconciled = False
        if (
            not dry_run
            and last_event is not None
            and state is not None
            and last_event[1] == state
            and (since is None or last_event[0] < since)
        ):
            store.set_grid_state(state, last_event[0])
            reconciled = True
    finally:
        store.close()

    return {
        "rows": len(rows),
        "samples": len(samples),
        "first": samples[0].timestamp,
        "last": samples[-1].timestamp,
        "transitions": len(transitions),
        "imported_events": imported_events,
        "hours": len(hours),
        "fresh_hours": len(fresh_hours),
        "imported_hours": imported_hours,
        "existing_event_floor": event_floor,
        "compacted": compacted,
        "reconciled_since": reconciled,
        "dry_run": dry_run,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", help="DessMonitor .xlsx export")
    parser.add_argument("--db", default=None, help="state.db path (default: from config)")
    parser.add_argument("--config", default="config.toml", help="config for db path and curve")
    parser.add_argument(
        "--shift-minutes",
        type=float,
        default=-60.0,
        help="shift applied to export timestamps (default -60: device clock runs 1 h ahead)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    config = load(args.config)
    db_path = Path(args.db or config.storage.path)
    curve = Curve(config.battery.curve) if config.battery.curve else None

    stats = import_file(
        args.export,
        db_path=db_path,
        shift=timedelta(minutes=args.shift_minutes),
        curve=curve,
        dry_run=args.dry_run,
    )
    stamp = lambda value: datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")
    print(f"db:          {db_path}")
    print(f"rows:        {stats['rows']} (usable samples: {stats['samples']})")
    print(f"range:       {stamp(stats['first'])} -> {stamp(stats['last'])}")
    print(f"transitions: {stats['transitions']} (inserted {stats['imported_events']})")
    print(f"soc hours:   {stats['hours']} (fresh {stats['fresh_hours']}, inserted {stats['imported_hours']})")
    if stats["compacted"]:
        print(f"compacted:   {stats['compacted']} repeated same-state event row(s) removed")
    if stats["reconciled_since"]:
        print("grid state anchor moved back to the last imported transition")
    if stats["dry_run"]:
        print("dry run: nothing written")


if __name__ == "__main__":
    main()
