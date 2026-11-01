"""Uptime and battery statistics over selectable windows."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .store import SocSummary, Store
from .text import day_label, format_duration, moment, percent

DAY = 86400.0
UNITS = {
    "day": (DAY, ("день", "дні", "днів")),
    "week": (7 * DAY, ("тиждень", "тижні", "тижнів")),
    "month": (30 * DAY, ("місяць", "місяці", "місяців")),
    "year": (365 * DAY, ("рік", "роки", "років")),
}
ALIASES = {
    "day": "day", "days": "day", "день": "day", "дні": "day", "днів": "day",
    "week": "week", "weeks": "week", "тиждень": "week", "тижні": "week", "тижнів": "week",
    "month": "month", "months": "month", "місяць": "month", "місяці": "month", "місяців": "month",
    "year": "year", "years": "year", "рік": "year", "роки": "year", "років": "year",
}
COMPACT = {"d": "day", "w": "week", "m": "month", "y": "year"}


def plural(count: int, forms: tuple[str, str, str]) -> str:
    if count % 10 == 1 and count % 100 != 11:
        return forms[0]
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return forms[1]
    return forms[2]


def parse_window(argument: str | None) -> tuple[float, str]:
    """'7 days', '3d', '2 months', 'week' → (seconds, label)."""
    text = (argument or "").strip().lower().replace(" ", "")
    if not text:
        return DAY, "остання доба"

    digits = ""
    unit_text = ""
    for char in text:
        if char.isdigit():
            digits += char
        else:
            unit_text += char
    count = int(digits) if digits else 1
    if count <= 0:
        raise ValueError("period must be positive")

    if unit_text in COMPACT:
        unit = COMPACT[unit_text]
    elif unit_text in ALIASES:
        unit = ALIASES[unit_text]
    else:
        raise ValueError(f"unknown period {argument!r}")

    seconds, forms = UNITS[unit]
    if count == 1:
        return seconds, f"останній {forms[0]}" if unit != "day" else "остання доба"
    return seconds * count, f"останні {count} {plural(count, forms)}"


@dataclass(frozen=True)
class Stats:
    since: float
    until: float
    powered: float
    unpowered: float
    outages: int
    longest_outage: float
    soc: SocSummary | None
    first_event: float | None = None

    @property
    def window(self) -> float:
        return max(1e-9, self.until - self.since)

    @property
    def covered(self) -> float:
        return self.powered + self.unpowered

    @property
    def total(self) -> float:
        return max(1e-9, self.covered)

    @property
    def average_outage(self) -> float:
        return self.unpowered / self.outages if self.outages else 0.0


def summarize(store: Store, seconds: float, now: float | None = None) -> Stats:
    until = time.time() if now is None else now
    since = until - seconds
    events = store.grid_events(since, until)
    if not events:
        return Stats(since, until, 0.0, 0.0, 0, 0.0, store.soc_summary(since, until))

    segments: list[tuple[bool, float, float]] = []
    state = events[0][1]
    cursor = max(since, events[0][0]) if events[0][0] < since else events[0][0]
    for timestamp, powered in events[1:]:
        if timestamp > cursor:
            segments.append((state, cursor, timestamp))
        state, cursor = powered, timestamp
    if cursor < until:
        segments.append((state, cursor, until))

    powered_seconds = sum(end - start for state, start, end in segments if state)
    unpowered_seconds = sum(end - start for state, start, end in segments if not state)
    outages = sum(1 for state, start, end in segments if not state and end > start)
    longest = max((end - start for state, start, end in segments if not state), default=0.0)
    return Stats(
        since=since,
        until=until,
        powered=powered_seconds,
        unpowered=unpowered_seconds,
        outages=outages,
        longest_outage=longest,
        soc=store.soc_summary(since, until),
        first_event=segments[0][1] if segments else None,
    )


def render_stats(stats: Stats, label: str) -> str:
    total = stats.total
    lines = [
        f"📊 <b>Статистика · {label}</b>",
        f"🟢 Світло було <b>{percent(stats.powered, total)}</b> ({format_duration(stats.powered)})",
        f"🔴 Без світла <b>{percent(stats.unpowered, total)}</b> ({format_duration(stats.unpowered)})",
    ]
    if stats.outages:
        lines.append(
            f"<blockquote>⚡ Відключень: <b>{stats.outages}</b>\n"
            f"⏱ Найдовше: <b>{format_duration(stats.longest_outage)}</b>\n"
            f"📉 У середньому: <b>{format_duration(stats.average_outage)}</b></blockquote>"
        )
    else:
        lines.append("<blockquote>⚡ Відключень не було</blockquote>")
    soc = stats.soc
    if soc is not None:
        if round(soc.minimum) == round(soc.maximum):
            lines.append(f"🔋 Батарея: {soc.average:.0f}% (стабільно)")
        else:
            lines.append(
                f"🔋 Батарея: {soc.minimum:.0f}–{soc.maximum:.0f}% "
                f"(сер. {soc.average:.0f}%)"
            )
    if stats.first_event is not None and stats.covered < stats.window * 0.95:
        lines.append(
            f"ℹ️ Дані з {moment(stats.first_event)} · "
            f"покриття {format_duration(stats.covered)}"
        )
    lines.append(
        f"<i>{day_label(stats.since)} → {day_label(stats.until)}</i>"
    )
    return "\n".join(lines)
