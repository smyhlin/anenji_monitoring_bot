"""State-transition alerts pushed to the admin chat."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .config import AlertsConfig
from .poller import MonitorState

log = logging.getLogger(__name__)

class Alerts:
    def __init__(
        self,
        state: MonitorState,
        config: AlertsConfig,
        send: Callable[[str], Awaitable[None]],
    ) -> None:
        self._state = state
        self._config = config
        self._send = send
        self._offline_after = config.offline_after
        self._started = time.time()
        self._offline_announced = False

    async def run(self, check_interval: float = 10.0) -> None:
        while True:
            try:
                await self.check()
            except asyncio.CancelledError:
                raise
            except Exception:  # alerts must never kill the bot
                log.exception("alert check failed")
            await asyncio.sleep(check_interval)

    async def check(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        await self._check_offline(now)

    async def _check_offline(self, now: float) -> None:
        reference = self._state.last_success or self._started
        silence = now - reference
        if not self._offline_announced and silence > self._offline_after:
            self._offline_announced = True
            reason = self._state.last_error or "немає даних від інвертора"
            await self._send(
                f"🔌 <b>Немає зв'язку з інвертором</b>\n"
                f"🕐 {silence / 60:.0f} хв без даних\n"
                f"<i>{reason}</i>"
            )
        elif self._offline_announced and self._state.age() is not None:
            if self._state.age() < self._offline_after:
                self._offline_announced = False
                await self._send("✅ <b>Зв'язок з інвертором відновлено</b>")
