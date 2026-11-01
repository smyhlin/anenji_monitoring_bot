"""Signal engine: grid transitions and battery thresholds, persisted and streamed."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from . import text
from .poller import MonitorState, Reading
from .registers import grid_present
from .store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GridTransition:
    powered: bool
    at: float
    previous_duration: float

    def render(self) -> str:
        return text.transition_message(self.powered, self.at, self.previous_duration)


@dataclass(frozen=True)
class BatteryAlert:
    level: int
    soc: float
    powered: bool | None
    register_soc: float | None = None

    def render(self) -> str:
        return text.battery_message(self.level, self.soc, self.powered, self.register_soc)


Event = GridTransition | BatteryAlert


class GridWatcher:
    """Turns polled readings into confirmed transitions, persisting the state."""

    def __init__(self, store: Store, *, confirm_samples: int = 2) -> None:
        self._store = store
        self._confirm = max(1, confirm_samples)
        self._pending: bool | None = None
        self._pending_since = 0.0
        self._pending_count = 0

    def current(self) -> tuple[bool | None, float | None]:
        return self._store.grid_state()

    def rebase(self, reading: Reading) -> None:
        """Adopt the current state silently (used right after the startup report).

        An unchanged state keeps its stored transition time, otherwise every
        restart would reset the "no power for X" counter to zero.
        """
        powered = grid_present(reading.values)
        state, _ = self._store.grid_state()
        if powered is not None and powered != state:
            # Changed while the bot was down: the best known bound is the moment we
            # last saw the old state, which is what last_seen holds.
            anchor = self._store.last_seen() or reading.timestamp
            self._store.set_grid_state(powered, min(anchor, reading.timestamp))
        self._forget_pending()

    def observe(self, reading: Reading) -> GridTransition | None:
        powered = grid_present(reading.values)
        if powered is None:
            return None

        state, since = self._store.grid_state()
        if state is None:
            self._store.set_grid_state(powered, reading.timestamp)
            return None
        if powered == state:
            self._forget_pending()
            return None

        if self._pending != powered:
            self._pending = powered
            self._pending_since = reading.timestamp
            self._pending_count = 1
        else:
            self._pending_count += 1
        if self._pending_count < self._confirm:
            return None

        changed_at = self._pending_since
        self._forget_pending()
        self._store.set_grid_state(powered, changed_at)
        previous_duration = 0.0 if since is None else max(0.0, changed_at - since)
        log.info(
            "grid %s at %s after %s",
            "restored" if powered else "lost",
            text.clock(changed_at),
            text.format_duration(previous_duration),
        )
        return GridTransition(powered, changed_at, previous_duration)

    def _forget_pending(self) -> None:
        self._pending = None
        self._pending_since = 0.0
        self._pending_count = 0


class BatteryWatcher:
    """Fires once when SOC crosses each configured level downwards, re-arming on recovery."""

    def __init__(self, levels: tuple[int, ...], *, recovery_margin: float = 3.0) -> None:
        self._levels = tuple(sorted(levels, reverse=True))
        self._margin = recovery_margin
        self._armed: set[int] | None = None

    def observe(self, reading: Reading) -> list[BatteryAlert]:
        soc = reading.values.get("soc", reading.values.get("battery_soc"))
        if soc is None:
            return []
        powered = grid_present(reading.values)
        if self._armed is None:
            # Levels already crossed while the bot was not running stay silent;
            # the startup report carries that information instead.
            self._armed = {level for level in self._levels if soc > level}
            return []

        register_soc = reading.values.get("battery_soc")
        alerts = []
        for level in self._levels:
            if soc <= level and level in self._armed:
                self._armed.discard(level)
                alerts.append(BatteryAlert(level, soc, powered, register_soc))
            elif soc >= level + self._margin:
                self._armed.add(level)
        return alerts


class SignalEngine:
    """Persists every polled sample and yields the events that come out of it."""

    def __init__(self, store: Store, grid: GridWatcher, battery: BatteryWatcher) -> None:
        self._store = store
        self._grid = grid
        self._battery = battery

    @property
    def grid(self) -> GridWatcher:
        return self._grid

    def observe(self, reading: Reading) -> list[Event]:
        self._store.set_last_seen(reading.timestamp)
        soc = reading.values.get("soc", reading.values.get("battery_soc"))
        if soc is not None:
            self._store.record_soc(reading.timestamp, soc)

        events: list[Event] = []
        transition = self._grid.observe(reading)
        if transition is not None:
            events.append(transition)
        events.extend(self._battery.observe(reading))
        return events


async def stream(
    state: MonitorState,
    engine: SignalEngine,
    deliver: Callable[[Event], Awaitable[None]],
    *,
    interval: float = 1.0,
) -> None:
    """Watch the poller snapshot and deliver each new event exactly once."""
    seen = 0.0
    while True:
        reading = state.latest
        if reading is not None and reading.timestamp > seen:
            seen = reading.timestamp
            for event in engine.observe(reading):
                try:
                    await deliver(event)
                except Exception:  # delivery must never kill the stream
                    log.exception("failed to deliver %s", type(event).__name__)
        await asyncio.sleep(interval)
