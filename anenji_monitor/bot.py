"""aiogram routers: device commands, chat subscription, PM settings, streaming."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import render, stats, text
from .config import Config
from .events import BatteryAlert, Event, GridTransition, SignalEngine
from .modbus import ModbusError
from .poller import Poller
from .registers import RegisterMap, grid_present
from .store import ChatSubscription, Store

log = logging.getLogger(__name__)

MAX_CAPTION = 1000
CHAT_TYPES = {"group", "supergroup", "channel"}
TOGGLE_FIELDS = {
    "lost": "grid_lost",
    "on": "grid_restored",
    "photo": "sync_photo",
    "batt": "battery",
}

HELP = """<b>Монітор інвертора</b>

/status — світло, батарея, вихід, навантаження
/stats — статистика (day, 7 days, 3m, 2 years)
/battery — деталі батареї
/pv — PV входи
/regs — профіль регістрів
/help — ця довідка

<i>Адміністратор також має:</i>
/all, /raw, /reconnect, /chats, /subscribe"""

ADMIN_HINT = "Ця команда доступна лише адміністратору бота."
ADMIN_COMMANDS = {"all", "raw", "reconnect", "chats", "subscribe"}

SUBSCRIBE_HELP = "Надішліть /subscribe у групі або /subscribe &lt;chat_id&gt; тут."


@dataclass(frozen=True)
class BotContext:
    config: Config
    poller: Poller
    registers: RegisterMap
    store: Store
    engine: SignalEngine
    stale_after: float


class FromIDs(BaseFilter):
    """Matches messages and callback queries sent by any of the given user ids."""

    def __init__(self, user_ids: tuple[int, ...]) -> None:
        self._user_ids = frozenset(user_ids)

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = getattr(event, "from_user", None)
        return user is not None and user.id in self._user_ids


# ------------------------------------------------------------------ settings UI


def settings_keyboard(chats: list[ChatSubscription]) -> InlineKeyboardMarkup:
    rows = []
    for chat in chats:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🔴 зникло {'✅' if chat.grid_lost else '⛔'}",
                    callback_data=f"ev:lost:{chat.chat_id}",
                ),
                InlineKeyboardButton(
                    text=f"🟢 з'явилось {'✅' if chat.grid_restored else '⛔'}",
                    callback_data=f"ev:on:{chat.chat_id}",
                ),
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🖼 фото {'✅' if chat.sync_photo else '⛔'}",
                    callback_data=f"ev:photo:{chat.chat_id}",
                ),
                InlineKeyboardButton(
                    text=f"🔋 батарея {'✅' if chat.battery else '⛔'}",
                    callback_data=f"ev:batt:{chat.chat_id}",
                ),
                InlineKeyboardButton(text="🗑", callback_data=f"ev:del:{chat.chat_id}"),
            ]
        )
    if rows:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Оновити назви й фото", callback_data="ev:refresh:0"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(chats: list[ChatSubscription]) -> str:
    if not chats:
        return (
            "<b>Трансляція сигналів</b>\n"
            "Поки що немає підписаних чатів.\n"
            "Додайте бота в групу та надішліть там /subscribe, "
            "або /subscribe &lt;chat_id&gt; тут."
        )
    lines = ["<b>Трансляція сигналів</b>", "Натисніть кнопку, щоб увімкнути або вимкнути подію."]
    for chat in chats:
        lines.append(
            f"<code>{chat.chat_id}</code> <b>{escape(chat.title or 'назва невідома')}</b>"
            f"{'' if chat.title else ' — додайте бота в чат і натисніть 🔄'}\n"
            f"📣 зникнення {'✅' if chat.grid_lost else '⛔'} · "
            f"поява {'✅' if chat.grid_restored else '⛔'} · "
            f"🖼 фото {'✅' if chat.sync_photo else '⛔'} · "
            f"🔋 батарея {'✅' if chat.battery else '⛔'}"
        )
    return "\n".join(lines)


# ------------------------------------------------------------------- streaming


async def sync_photos(bot: Bot, store: Store, powered: bool, *, force: bool = False) -> None:
    """Repaint chat pictures that do not already show this grid state.

    Only a state change (or a start after a reboot) may repaint: renewing the photo
    on every restart, deploy or dongle retry would spam the chat with
    "updated group photo" service messages.
    """
    path = render.bulb_path(powered)
    if path is None:
        return
    for chat in store.chats():
        if not chat.sync_photo:
            continue
        if not force and chat.photo_powered is powered:
            continue
        try:
            await bot.set_chat_photo(chat.chat_id, photo=FSInputFile(path))
        except Exception as exc:  # noqa: BLE001 - needs admin rights; keep the stream alive
            log.warning("cannot set photo for %s: %s", chat.chat_id, exc)
            continue
        store.set_photo_state(chat.chat_id, powered)


async def _send(bot: Bot, chat_id: int, body: str) -> bool:
    try:
        await bot.send_message(chat_id, body)
        return True
    except Exception as exc:  # noqa: BLE001 - a chat may have dropped the bot
        log.warning("failed to post to %s: %s", chat_id, exc)
        return False


async def refresh_chats(bot: Bot, store: Store, powered: bool | None) -> list[str]:
    """Re-resolve titles and repaint photos; returns one note per chat."""
    notes = []
    path = render.bulb_path(powered) if powered is not None else None
    for chat in store.chats():
        name = chat.title or str(chat.chat_id)
        try:
            resolved = (await bot.get_chat(chat.chat_id)).title or ""
        except Exception as exc:  # noqa: BLE001 - bot may not be in the chat
            notes.append(f"⚠️ {name}: {exc}")
            continue
        if resolved and resolved != chat.title:
            store.set_title(chat.chat_id, resolved)
            name = resolved
        if path is None or not chat.sync_photo:
            notes.append(f"✅ {name}: назва оновлена")
            continue
        try:
            await bot.set_chat_photo(chat.chat_id, photo=FSInputFile(path))
            store.set_photo_state(chat.chat_id, powered)
            notes.append(f"✅ {name}: назва й фото оновлені")
        except Exception as exc:  # noqa: BLE001 - needs change-info rights
            notes.append(f"⚠️ {name}: фото не змінено ({exc})")
    return notes


async def publish_event(bot: Bot, store: Store, event: Event) -> None:
    if isinstance(event, GridTransition):
        await sync_photos(bot, store, event.powered)
    body = event.render()
    for chat in store.chats():
        if isinstance(event, GridTransition) and not chat.wants(event.powered):
            continue
        if isinstance(event, BatteryAlert) and not chat.battery:
            continue
        await _send(bot, chat.chat_id, body)


async def publish_recovery(
    bot: Bot,
    store: Store,
    admin_ids: tuple[int, ...],
    *,
    title: str,
    gap: float,
    powered_now: bool | None,
    changed: bool,
    soc: float | None,
) -> None:
    body = text.recovery_message(
        title=title, gap=gap, powered_now=powered_now, changed=changed, soc=soc
    )
    chats = store.chats()
    targets = [chat.chat_id for chat in chats] or list(admin_ids)
    for chat_id in targets:
        await _send(bot, chat_id, body)


# ----------------------------------------------------------------------- routes


def build_subscribe_router(ctx: BotContext) -> Router:
    router = Router(name="subscribe")

    async def may_manage(message: Message, chat_id: int) -> bool:
        user = message.from_user
        if user is None:
            return False
        if user.id in ctx.config.telegram.admin_ids:
            return True
        if message.chat.id != chat_id:
            return False
        try:
            member = await message.bot.get_chat_member(chat_id, user.id)
        except Exception:  # noqa: BLE001 - the bot may not be allowed to inspect members
            return False
        return member.status in {"administrator", "creator"}

    @router.message(Command("subscribe"))
    async def subscribe(message: Message, command: CommandObject) -> None:
        arguments = (command.args or "").split()
        if message.chat.type in CHAT_TYPES:
            chat_id = message.chat.id
        elif arguments:
            try:
                chat_id = int(arguments[0])
            except ValueError:
                await message.answer(SUBSCRIBE_HELP)
                return
        else:
            await message.answer(SUBSCRIBE_HELP)
            return

        if not await may_manage(message, chat_id):
            await message.answer("Лише адміністратори чату можуть змінювати трансляцію.")
            return

        title = message.chat.title or ""
        if chat_id != message.chat.id:
            try:
                title = (await message.bot.get_chat(chat_id)).title or ""
            except Exception:  # noqa: BLE001 - title is cosmetic; the id is what matters
                title = ""

        added = ctx.store.add_chat(
            chat_id, title, message.from_user.id if message.from_user else None
        )
        if title:
            ctx.store.set_title(chat_id, title)
        photo_note = ""
        powered, _ = ctx.engine.grid.current()
        if powered is None and ctx.poller.state.latest is not None:
            powered = grid_present(ctx.poller.state.latest.values)
        path = render.bulb_path(powered) if powered is not None else None
        if path is not None:
            try:
                await message.bot.set_chat_photo(chat_id, photo=FSInputFile(path))
                ctx.store.set_photo_state(chat_id, powered)
            except Exception as exc:  # noqa: BLE001 - needs change-info rights
                log.warning("cannot set photo for %s: %s", chat_id, exc)
                photo_note = "\n⚠️ Немає права змінювати фото чату — дайте боту «Зміна інформації чату»."

        if chat_id == message.chat.id:
            await message.answer(
                "🔔 <b>Трансляцію увімкнено</b>\n"
                "Повідомлення про зникнення та появу світла.\n"
                "Налаштування — в особистих повідомленнях: /chats" + photo_note
            )
        elif added:
            await message.answer(f"Підписано <code>{chat_id}</code>")
        else:
            await message.answer(f"Вже підписано: <code>{chat_id}</code>")

    return router


def build_router(ctx: BotContext) -> Router:
    router = Router(name="anenji")
    admin = FromIDs(ctx.config.telegram.admin_ids)
    member = FromIDs(ctx.config.telegram.member_ids)

    @router.message(Command("start", "help"), member)
    async def help_handler(message: Message) -> None:
        await message.answer(HELP)

    @router.message(Command("status"), member)
    async def status_handler(message: Message) -> None:
        _, since = ctx.engine.grid.display_state()
        body = render.status(
            ctx.poller.state, ctx.registers, stale_after=ctx.stale_after, grid_since=since
        )
        reading = ctx.poller.state.latest
        bulb = render.bulb_for(reading.values) if reading else None
        if bulb is None or len(body) > MAX_CAPTION:
            await message.answer(body)
            return
        await message.answer_photo(FSInputFile(bulb), caption=body)

    @router.message(Command("stats"), member)
    async def stats_handler(message: Message, command: CommandObject) -> None:
        try:
            seconds, label = stats.parse_window(command.args)
        except ValueError:
            await message.answer(
                "Період: <code>day</code>, <code>7 days</code>, <code>2 weeks</code>, "
                "<code>3m</code>, <code>2 years</code>"
            )
            return
        summary = stats.summarize(ctx.store, seconds)
        await message.answer(stats.render_stats(summary, label))

    @router.message(Command("battery"), member)
    async def battery_handler(message: Message) -> None:
        await message.answer(
            render.group(ctx.poller.state, ctx.registers, "battery", stale_after=ctx.stale_after)
        )

    @router.message(Command("pv"), member)
    async def pv_handler(message: Message) -> None:
        await message.answer(
            render.group(ctx.poller.state, ctx.registers, "pv", stale_after=ctx.stale_after)
        )

    @router.message(Command("all"), admin)
    async def all_handler(message: Message) -> None:
        await message.answer(
            render.all_registers(ctx.poller.state, ctx.registers, stale_after=ctx.stale_after)
        )

    @router.message(Command("regs"), member)
    async def regs_handler(message: Message) -> None:
        await message.answer(render.register_list(ctx.registers))

    @router.message(Command("chats"), admin)
    async def chats_handler(message: Message) -> None:
        chats = ctx.store.chats()
        await message.answer(
            settings_text(chats),
            reply_markup=settings_keyboard(chats) if chats else None,
        )

    @router.callback_query(F.data.startswith("ev:"), admin)
    async def settings_callback(query: CallbackQuery) -> None:
        parts = (query.data or "").split(":")
        if len(parts) != 3:
            await query.answer()
            return
        action, raw_chat_id = parts[1], parts[2]
        try:
            chat_id = int(raw_chat_id)
        except ValueError:
            await query.answer()
            return

        if action == "refresh":
            powered, _ = ctx.engine.grid.current()
            if powered is None and ctx.poller.state.latest is not None:
                powered = grid_present(ctx.poller.state.latest.values)
            notes = await refresh_chats(query.bot, ctx.store, powered)
            chats = ctx.store.chats()
            await query.answer("Оновлено")
            if isinstance(query.message, Message):
                try:
                    await query.message.edit_text(
                        settings_text(chats) + "\n\n" + "\n".join(notes),
                        reply_markup=settings_keyboard(chats) if chats else None,
                    )
                except Exception:  # message may be too old to edit
                    log.debug("settings message not editable", exc_info=True)
            return

        if action == "del":
            ctx.store.remove_chat(chat_id)
        elif action in TOGGLE_FIELDS:
            ctx.store.toggle(chat_id, TOGGLE_FIELDS[action])
        else:
            await query.answer()
            return

        chats = ctx.store.chats()
        await query.answer("Збережено")
        if isinstance(query.message, Message):
            try:
                await query.message.edit_text(
                    settings_text(chats),
                    reply_markup=settings_keyboard(chats) if chats else None,
                )
            except Exception:
                log.debug("settings message not editable", exc_info=True)

    @router.message(Command("raw"), admin)
    async def raw_handler(message: Message, command: CommandObject) -> None:
        args = (command.args or "").split()
        if not args:
            await message.answer("Використання: <code>/raw 215</code> або <code>/raw 215 5</code>")
            return
        try:
            start = int(args[0], 0)
            count = int(args[1], 0) if len(args) > 1 else 1
        except ValueError:
            await message.answer("Адреса та кількість мають бути числами.")
            return
        if not 0 <= start <= 0xFFFF or not 1 <= count <= 125:
            await message.answer("Адреса 0..65535, кількість 1..125.")
            return
        try:
            values = await ctx.poller.read_registers(start, count)
        except ModbusError as exc:
            await message.answer(f"Помилка читання: <code>{escape(str(exc))}</code>")
            return
        await message.answer(render.raw_block(start, values, ctx.registers))

    @router.message(Command("reconnect"), admin)
    async def reconnect_handler(message: Message) -> None:
        await ctx.poller.reconnect()
        await message.answer("З'єднання скинуто, опитування перепідключиться.")

    @router.message(F.text.startswith("/"), member)
    async def fallback_handler(message: Message) -> None:
        command = (message.text or "").split()[0].lstrip("/").split("@")[0]
        if command in ADMIN_COMMANDS:
            await message.answer(ADMIN_HINT)
            return
        await message.answer("Невідома команда. Спробуйте /help.")

    return router


def build_unauthorized_router(admin_ids: tuple[int, ...]) -> Router:
    router = Router(name="unauthorized")

    @router.message(F.chat.type == "private")
    async def deny(message: Message) -> None:
        user = message.from_user
        if user is None or user.is_bot:
            # Service messages (e.g. "updated group photo") are not requests.
            return
        log.warning("unauthorized access from %s", user.id)
        await message.answer("Цей бот приватний.")

    return router


async def announce(bot: Bot, chat_ids: tuple[int, ...], body: str) -> None:
    for chat_id in chat_ids:
        await _send(bot, chat_id, body)
