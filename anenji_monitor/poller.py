"""Background polling of the inverter with a cached snapshot for the bot."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from .battery import SOURCE_REGISTER, SocEstimator
from .config import InverterConfig
from .modbus import AnenjiLink, LinkDown, ModbusError
from .registers import RegisterMap

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reading:
    timestamp: float
    values: dict[str, float]
    raw: dict[int, int]


@dataclass
class MonitorState:
    latest: Reading | None = None
    last_success: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    batches: tuple[tuple[int, int], ...] = field(default=())

    def age(self) -> float | None:
        return None if self.last_success is None else time.time() - self.last_success


class Poller:
    def __init__(
        self,
        link: AnenjiLink,
        config: InverterConfig,
        register_map: RegisterMap,
        estimator: SocEstimator | None = None,
    ) -> None:
        self._link = link
        self._config = config
        self._registers = register_map
        self._estimator = estimator or SocEstimator(source=SOURCE_REGISTER)
        self._state = MonitorState(
            batches=register_map.batches(max_gap=config.max_gap, max_batch=config.max_batch)
        )

    @property
    def state(self) -> MonitorState:
        return self._state

    @property
    def link(self) -> AnenjiLink:
        return self._link

    @property
    def registers(self) -> RegisterMap:
        return self._registers

    async def run(self) -> None:
        log.info(
            "polling %d registers in %d batch(es) every %gs",
            len(self._registers.registers),
            len(self._state.batches),
            self._config.poll_interval,
        )
        while True:
            started = time.monotonic()
            await self.poll_once()
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.0, self._config.poll_interval - elapsed))

    async def poll_once(self) -> bool:
        try:
            reading = await asyncio.to_thread(self.read_cycle)
        except ModbusError as exc:
            self._record_failure(exc)
            return False
        except Exception as exc:  # the poller must survive unexpected failures
            log.exception("unexpected polling failure")
            self._record_failure(exc)
            return False
        self._record_success(reading)
        return True

    async def read_registers(self, start: int, count: int) -> dict[int, int]:
        return await asyncio.to_thread(self._read, start, count)

    async def reconnect(self) -> None:
        await asyncio.to_thread(self._link.reset)
        self._state.last_error = None
        log.info("connection dropped on request; poller will reconnect")

    def read_cycle(self) -> Reading:
        raw: dict[int, int] = {}
        batches = self._state.batches
        index = 0
        retried = False
        while index < len(batches):
            start, count = batches[index]
            try:
                raw.update(self._read(start, count))
            except LinkDown:
                if not raw or retried:
                    raise
                # The dongle closes some sessions after serving data; one retry
                # finishes the cycle instead of throwing the whole poll away.
                log.info("session dropped after %d batch(es), continuing once", index)
                retried = True
                continue
            index += 1
        values = self._registers.convert_all(raw)
        soc = self._estimator.estimate(values)
        if soc is not None:
            values["soc"] = soc
        return Reading(timestamp=time.time(), values=values, raw=raw)

    def _read(self, start: int, count: int) -> dict[int, int]:
        return self._link.read(start, count)

    def _record_success(self, reading: Reading) -> None:
        if self._state.last_error is not None:
            log.info("inverter back online after %d failure(s)", self._state.consecutive_failures)
        self._state.latest = reading
        self._state.last_success = reading.timestamp
        self._state.last_error = None
        self._state.consecutive_failures = 0

    def _record_failure(self, exc: Exception) -> None:
        self._state.consecutive_failures += 1
        self._state.last_error = f"{type(exc).__name__}: {exc}"
        level = logging.WARNING if self._state.consecutive_failures == 1 else logging.DEBUG
        log.log(level, "poll failed (%d in a row): %s", self._state.consecutive_failures, self._state.last_error)
