#!/usr/bin/env python3
"""Run the Anenji inverter monitoring Telegram bot."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from anenji_monitor import config as config_module
from anenji_monitor import events, recovery
from anenji_monitor.alerts import Alerts
from anenji_monitor.battery import SocEstimator, curve_from_config
from anenji_monitor.bot import (
    BotContext,
    announce,
    build_router,
    build_subscribe_router,
    build_unauthorized_router,
    publish_event,
    publish_recovery,
    sync_photos,
)
from anenji_monitor.events import BatteryWatcher, GridWatcher, SignalEngine
from anenji_monitor.modbus import AnenjiLink
from anenji_monitor.poller import Poller
from anenji_monitor.registers import RegisterMap, RegisterMapError, grid_present
from anenji_monitor.store import Store

log = logging.getLogger("anenji_monitor")

COMMANDS = [
    BotCommand(command="status", description="Стан інвертора"),
    BotCommand(command="stats", description="Статистика за період"),
    BotCommand(command="battery", description="Батарея"),
    BotCommand(command="pv", description="PV входи"),
    BotCommand(command="all", description="Усі регістри профілю"),
    BotCommand(command="raw", description="Читати регістри"),
    BotCommand(command="regs", description="Профіль регістрів"),
    BotCommand(command="reconnect", description="Перепідключити донгл"),
    BotCommand(command="subscribe", description="Трансляція в цей чат"),
    BotCommand(command="chats", description="Налаштування трансляції"),
    BotCommand(command="help", description="Довідка"),
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml", help="path to config.toml")
    parser.add_argument("--log-level", default="INFO", help="DEBUG, INFO, WARNING ...")
    return parser.parse_args()


async def wait_for_reading(poller: Poller, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if poller.state.latest is not None:
            return poller.state.latest
        await asyncio.sleep(0.2)
    return None


async def photo_guard(
    bot: Bot, store: Store, engine: SignalEngine, interval: float = 1800.0
) -> None:
    """Repaint chat photos periodically: rights may be granted late, Telegram may rate-limit."""
    while True:
        await asyncio.sleep(interval)
        powered, _ = engine.grid.current()
        if powered is None:
            continue
        try:
            await sync_photos(bot, store, powered)
        except Exception:  # a guard must never die
            log.exception("photo refresh failed")


async def register_default_chats(bot: Bot, store: Store, chat_ids: tuple[int, ...]) -> None:
    for chat_id in chat_ids:
        existing = store.chat(chat_id)
        title = ""
        try:
            title = (await bot.get_chat(chat_id)).title or ""
        except Exception:  # noqa: BLE001 - bot may not be in the chat yet
            log.warning("cannot resolve title for %s", chat_id)
        if existing is None:
            store.add_chat(chat_id, title, None)
            log.info("registered default chat %s (%s)", chat_id, title or "no title")
        elif title and title != existing.title:
            store.set_title(chat_id, title)
            log.info("refreshed title for %s: %s", chat_id, title)


async def startup_report(
    bot: Bot,
    store: Store,
    config: config_module.Config,
    poller: Poller,
    engine: SignalEngine,
    started_at: float,
    last_seen: float | None,
    last_powered: bool | None,
) -> None:
    """Explain the downtime once, then silently adopt the observed state."""
    reading = await wait_for_reading(poller, timeout=max(45.0, config.inverter.connect_timeout))
    powered_now = grid_present(reading.values) if reading else None
    gap = None if last_seen is None else max(0.0, started_at - last_seen)
    verdict = recovery.classify(
        gap=gap,
        last_powered=last_powered,
        powered_now=powered_now,
        reboot_after=config.recovery.reboot_after,
        drained_after=config.recovery.drained_after,
    )
    soc = reading.values.get("battery_soc") if reading else None

    if verdict.worth_reporting:
        await publish_recovery(
            bot,
            store,
            config.telegram.admin_ids,
            title=verdict.title,
            gap=verdict.gap,
            powered_now=powered_now,
            changed=verdict.grid_changed,
            soc=soc,
        )
        log.info("startup report: %s (gap %.0fs)", verdict.kind, verdict.gap)

    if reading is not None:
        engine.grid.rebase(reading)
        if powered_now is not None:
            # Only a cold start (no history, or downtime past reboot_after) repaints
            # unconditionally; a quick restart leaves an already-correct photo alone.
            cold_start = verdict.gap >= config.recovery.reboot_after
            await sync_photos(
                bot, store, powered_now, force=cold_start or verdict.kind == recovery.FIRST_START
            )
    store.set_last_seen(reading.timestamp if reading else started_at)


async def run(args: argparse.Namespace) -> None:
    config = config_module.load(args.config)
    inverter = config.inverter
    started_at = time.time()

    link = AnenjiLink(
        inverter.host,
        listen_port=inverter.listen_port,
        connect_timeout=inverter.connect_timeout,
        request_timeout=inverter.request_timeout,
    )
    registers = RegisterMap.load(inverter.register_map)
    estimator = SocEstimator(
        source=config.battery.soc_source,
        curve=curve_from_config(list(config.battery.curve)) if config.battery.curve else None,
    )
    poller = Poller(link, inverter, registers, estimator)

    store = Store(config.storage.path)
    last_seen = store.last_seen()
    last_powered, _ = store.grid_state()
    grid_watcher = GridWatcher(
        store,
        confirm_samples=config.events.confirm_samples,
        off_settle=config.events.grid_off_settle,
    )
    engine = SignalEngine(
        store,
        grid_watcher,
        BatteryWatcher(config.alerts.soc_levels, settle_seconds=config.alerts.soc_settle),
    )
    ctx = BotContext(
        config=config,
        poller=poller,
        registers=registers,
        store=store,
        engine=engine,
        stale_after=max(60.0, inverter.poll_interval * 4),
    )

    bot = Bot(
        config.telegram.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(build_subscribe_router(ctx))
    dispatcher.include_router(build_router(ctx))
    dispatcher.include_router(build_unauthorized_router(config.telegram.admin_ids))

    await register_default_chats(bot, store, config.telegram.default_chats)

    background = [asyncio.create_task(poller.run(), name="poller")]
    polling = asyncio.create_task(dispatcher.start_polling(bot), name="dispatcher")

    me = await bot.get_me()
    await bot.set_my_commands(COMMANDS)
    log.info("bot @%s ready, polling %s:%d", me.username, inverter.host, inverter.listen_port)

    # Commands answer immediately; the downtime story is posted once the first
    # reading is in, and only then does the event stream take over.
    await startup_report(
        bot, store, config, poller, engine, started_at, last_seen, last_powered
    )
    background.append(
        asyncio.create_task(
            events.stream(
                poller.state,
                engine,
                lambda event: publish_event(bot, store, event),
            ),
            name="events",
        )
    )
    background.append(
        asyncio.create_task(photo_guard(bot, store, engine), name="photo-guard")
    )
    if config.alerts.enabled:
        alerts = Alerts(
            poller.state,
            config.alerts,
            lambda body: announce(bot, config.telegram.admin_ids, body),
        )
        background.append(asyncio.create_task(alerts.run(), name="alerts"))

    try:
        await polling
    finally:
        link.close()
        for task in [*background, polling]:
            task.cancel()
        await asyncio.gather(*background, polling, return_exceptions=True)
        store.close()
        await bot.session.close()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        asyncio.run(run(args))
    except (config_module.ConfigError, RegisterMapError) as exc:
        raise SystemExit(f"config error: {exc}") from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
