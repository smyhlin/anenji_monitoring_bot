"""Startup recovery: explain what happened while the bot was not running.

Thresholds come from config; the classification itself is pure so it can be
tested without a clock or a database.
"""

from __future__ import annotations

from dataclasses import dataclass

FIRST_START = "first_start"
QUICK_RESTART = "quick_restart"
RESTART = "restart"
LONG_OUTAGE = "long_outage"
DRAINED = "inverter_drained"

TITLES = {
    FIRST_START: "🟢 Моніторинг запущено",
    QUICK_RESTART: "🔄 Бот перезапустився",
    RESTART: "🔄 Бот був вимкнений",
    LONG_OUTAGE: "😴 Довга пауза моніторингу",
    DRAINED: "🪫 Інвертор вимкнувся — батарея розрядилась",
}


@dataclass(frozen=True)
class Recovery:
    kind: str
    gap: float
    last_powered: bool | None
    powered_now: bool | None

    @property
    def title(self) -> str:
        return TITLES.get(self.kind, TITLES[RESTART])

    @property
    def grid_changed(self) -> bool:
        return (
            self.last_powered is not None
            and self.powered_now is not None
            and self.last_powered != self.powered_now
        )

    @property
    def worth_reporting(self) -> bool:
        return self.kind != QUICK_RESTART


def classify(
    *,
    gap: float | None,
    last_powered: bool | None,
    powered_now: bool | None,
    reboot_after: float = 300.0,
    drained_after: float = 1800.0,
) -> Recovery:
    if gap is None:
        return Recovery(FIRST_START, 0.0, None, powered_now)
    if gap < reboot_after:
        return Recovery(QUICK_RESTART, gap, last_powered, powered_now)
    if (
        gap >= drained_after
        and last_powered is False
        and powered_now is True
    ):
        return Recovery(DRAINED, gap, last_powered, powered_now)
    if gap >= drained_after:
        return Recovery(LONG_OUTAGE, gap, last_powered, powered_now)
    return Recovery(RESTART, gap, last_powered, powered_now)
