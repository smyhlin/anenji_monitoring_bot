"""SQLite persistence: chat subscriptions, signal history, SOC rollups, recovery metadata.

Everything the bot needs to survive a restart lives here — the outage durations it
posts are measured backwards from stored timestamps, and the statistics commands
read the same history.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

FLAG_FIELDS = ("grid_lost", "grid_restored", "sync_photo", "battery")


@dataclass(frozen=True)
class ChatSubscription:
    chat_id: int
    title: str
    grid_lost: bool
    grid_restored: bool
    sync_photo: bool
    battery: bool
    photo_powered: bool | None = None

    def wants(self, powered: bool) -> bool:
        return self.grid_restored if powered else self.grid_lost


@dataclass(frozen=True)
class SocSummary:
    minimum: float
    maximum: float
    average: float
    samples: int


class Store:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id       INTEGER PRIMARY KEY,
                title         TEXT    NOT NULL DEFAULT '',
                grid_lost     INTEGER NOT NULL DEFAULT 1,
                grid_restored INTEGER NOT NULL DEFAULT 1,
                sync_photo    INTEGER NOT NULL DEFAULT 1,
                battery       INTEGER NOT NULL DEFAULT 1,
                added_by      INTEGER,
                added_at      REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS grid_events (
                ts      REAL    NOT NULL,
                powered INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS grid_events_ts ON grid_events (ts);
            CREATE UNIQUE INDEX IF NOT EXISTS grid_events_ts_unique ON grid_events (ts);
            CREATE TABLE IF NOT EXISTS soc_hourly (
                hour    INTEGER PRIMARY KEY,
                min_soc REAL    NOT NULL,
                max_soc REAL    NOT NULL,
                sum_soc REAL    NOT NULL,
                count   INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(chats)")}
        for column, definition in (
            ("sync_photo", "INTEGER NOT NULL DEFAULT 1"),
            ("battery", "INTEGER NOT NULL DEFAULT 1"),
            ("photo_powered", "INTEGER"),
        ):
            if column not in columns:
                self._conn.execute(f"ALTER TABLE chats ADD COLUMN {column} {definition}")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- chats

    def add_chat(self, chat_id: int, title: str, added_by: int | None) -> bool:
        cursor = self._conn.execute(
            "INSERT OR IGNORE INTO chats (chat_id, title, added_by, added_at) VALUES (?, ?, ?, ?)",
            (chat_id, title, added_by, time.time()),
        )
        self._conn.commit()
        return cursor.rowcount > 0

    def remove_chat(self, chat_id: int) -> bool:
        cursor = self._conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))
        self._conn.commit()
        return cursor.rowcount > 0

    def set_photo_state(self, chat_id: int, powered: bool | None) -> None:
        """Record the grid state the chat picture actually shows."""
        value = None if powered is None else int(powered)
        self._conn.execute(
            "UPDATE chats SET photo_powered = ? WHERE chat_id = ?", (value, chat_id)
        )
        self._conn.commit()

    def set_title(self, chat_id: int, title: str) -> None:
        self._conn.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id))
        self._conn.commit()

    def chats(self) -> list[ChatSubscription]:
        rows = self._conn.execute(
            "SELECT chat_id, title, grid_lost, grid_restored, sync_photo, battery, "
            "photo_powered FROM chats ORDER BY added_at"
        )
        return [_row_to_chat(row) for row in rows]

    def chat(self, chat_id: int) -> ChatSubscription | None:
        row = self._conn.execute(
            "SELECT chat_id, title, grid_lost, grid_restored, sync_photo, battery, "
            "photo_powered FROM chats WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return None if row is None else _row_to_chat(row)

    def toggle(self, chat_id: int, field: str) -> ChatSubscription | None:
        if field not in FLAG_FIELDS:
            raise ValueError(f"unknown flag {field!r}")
        self._conn.execute(
            f"UPDATE chats SET {field} = 1 - {field} WHERE chat_id = ?", (chat_id,)
        )
        self._conn.commit()
        return self.chat(chat_id)

    # ----------------------------------------------------------- grid state

    def grid_state(self) -> tuple[bool | None, float | None]:
        rows = dict(self._conn.execute("SELECT key, value FROM meta").fetchall())
        powered = rows.get("grid_powered")
        since = rows.get("grid_since")
        if powered is None or since is None:
            return None, None
        return powered == "1", float(since)

    def set_grid_state(self, powered: bool, since: float) -> None:
        self._conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (("grid_powered", "1" if powered else "0"), ("grid_since", repr(float(since)))),
        )
        # Only transitions belong in the history; re-anchoring the same state
        # would break outage counting (each repeat looks like a new outage).
        last = self._conn.execute(
            "SELECT powered FROM grid_events ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if last is None or bool(last["powered"]) != powered:
            self._conn.execute(
                "INSERT OR IGNORE INTO grid_events (ts, powered) VALUES (?, ?)",
                (since, int(powered)),
            )
        self._conn.commit()

    def last_seen(self) -> float | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'last_seen'").fetchone()
        return None if row is None else float(row["value"])

    def set_last_seen(self, timestamp: float) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES ('last_seen', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (repr(float(timestamp)),),
        )
        self._conn.commit()

    # -------------------------------------------------------------- history

    def grid_events(self, since: float, until: float) -> list[tuple[float, bool]]:
        """Transitions inside the window, plus the newest one before it."""
        previous = self._conn.execute(
            "SELECT ts, powered FROM grid_events WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
            (since,),
        ).fetchone()
        rows = self._conn.execute(
            "SELECT ts, powered FROM grid_events WHERE ts > ? AND ts <= ? ORDER BY ts",
            (since, until),
        )
        events = [] if previous is None else [(previous["ts"], bool(previous["powered"]))]
        events.extend((row["ts"], bool(row["powered"])) for row in rows)
        return events

    def add_grid_events(self, events: list[tuple[float, bool]]) -> int:
        """Insert historical transitions; duplicates by timestamp are ignored."""
        cursor = self._conn.executemany(
            "INSERT OR IGNORE INTO grid_events (ts, powered) VALUES (?, ?)",
            [(timestamp, int(powered)) for timestamp, powered in events],
        )
        self._conn.commit()
        return cursor.rowcount

    def add_soc_hours(
        self, hours: dict[int, tuple[float, float, float, int]]
    ) -> int:
        """Insert hourly rollups, leaving hours that already hold live samples alone."""
        cursor = self._conn.executemany(
            "INSERT OR IGNORE INTO soc_hourly (hour, min_soc, max_soc, sum_soc, count) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (hour, low, high, total, samples)
                for hour, (low, high, total, samples) in sorted(hours.items())
            ],
        )
        self._conn.commit()
        return cursor.rowcount

    def compact_grid_events(self) -> int:
        """Keep one row per state change; repeated same-state rows inflate outage counts."""
        rows = self._conn.execute(
            "SELECT ts, powered FROM grid_events ORDER BY ts"
        ).fetchall()
        keep: list[tuple[float, int]] = []
        for row in rows:
            if not keep or keep[-1][1] != row["powered"]:
                keep.append((row["ts"], row["powered"]))
        if len(keep) == len(rows):
            return 0
        self._conn.execute("DELETE FROM grid_events")
        self._conn.executemany(
            "INSERT INTO grid_events (ts, powered) VALUES (?, ?)", keep
        )
        self._conn.commit()
        return len(rows) - len(keep)

    def record_soc(self, timestamp: float, soc: float) -> None:
        hour = int(timestamp) // 3600 * 3600
        self._conn.execute(
            "INSERT INTO soc_hourly (hour, min_soc, max_soc, sum_soc, count) VALUES (?, ?, ?, ?, 1) "
            "ON CONFLICT(hour) DO UPDATE SET "
            "min_soc = MIN(min_soc, excluded.min_soc), "
            "max_soc = MAX(max_soc, excluded.max_soc), "
            "sum_soc = sum_soc + excluded.sum_soc, "
            "count = count + excluded.count",
            (hour, soc, soc, soc),
        )
        self._conn.commit()

    def soc_summary(self, since: float, until: float) -> SocSummary | None:
        row = self._conn.execute(
            "SELECT MIN(min_soc) AS lo, MAX(max_soc) AS hi, "
            "SUM(sum_soc) AS total, SUM(count) AS samples "
            "FROM soc_hourly WHERE hour >= ? AND hour <= ?",
            (int(since) // 3600 * 3600, int(until)),
        ).fetchone()
        if row is None or not row["samples"]:
            return None
        return SocSummary(
            minimum=row["lo"],
            maximum=row["hi"],
            average=row["total"] / row["samples"],
            samples=int(row["samples"]),
        )


def _row_to_chat(row: sqlite3.Row) -> ChatSubscription:
    return ChatSubscription(
        chat_id=row["chat_id"],
        title=row["title"],
        grid_lost=bool(row["grid_lost"]),
        grid_restored=bool(row["grid_restored"]),
        sync_photo=bool(row["sync_photo"]),
        battery=bool(row["battery"]),
        photo_powered=None if row["photo_powered"] is None else bool(row["photo_powered"]),
    )
