import asyncio
import datetime
import os
import tempfile
import time
import unittest

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetChat, GetMe, SendMessage, SendPhoto, SetChatPhoto
from aiogram.types import Chat, Message, Update, User

from anenji_monitor.alerts import AlertsConfig
from anenji_monitor.bot import (
    BotContext,
    build_router,
    build_unauthorized_router,
    publish_event,
    publish_recovery,
    refresh_chats,
    settings_keyboard,
    settings_text,
    sync_photos,
)
from anenji_monitor.config import Config, InverterConfig, TelegramConfig
from anenji_monitor.events import BatteryAlert, GridTransition
from anenji_monitor.poller import MonitorState, Reading
from anenji_monitor.store import Store


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[int, str]] = []
        self.captions: list[str] = []
        self.photos: list[int] = []
        self.titles: dict[int, str] = {}
        self.failing_chats: set[int] = set()

    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, GetMe):
            return User(id=1, is_bot=True, first_name="t", username="t_bot")
        if isinstance(method, SendMessage):
            self.sent.append((method.chat_id, method.text))
            return Message(
                message_id=1,
                date=datetime.datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
                text=method.text,
            )
        if isinstance(method, SendPhoto):
            self.captions.append(method.caption or "")
            return Message(
                message_id=1,
                date=datetime.datetime.now(),
                chat=Chat(id=method.chat_id, type="private"),
            )
        if isinstance(method, SetChatPhoto):
            self.photos.append(method.chat_id)
            return True
        if isinstance(method, GetChat):
            if method.chat_id in self.failing_chats or method.chat_id not in self.titles:
                raise RuntimeError("Bad Request: chat not found")
            return Chat(id=method.chat_id, type="supergroup", title=self.titles[method.chat_id])
        return True

    async def stream_content(self, *a, **k):
        yield b""

    async def close(self) -> None:
        pass


class BotTestCase(unittest.TestCase):
    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)
        self.session = RecordingSession()
        self.bot = Bot(token="1:test", session=self.session)

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)


class PublishEventTest(BotTestCase):
    def test_transition_respects_toggles_and_repaints_photos(self):
        self.store.add_chat(-1001, "both", None)
        self.store.add_chat(-1002, "lost only", None)
        self.store.toggle(-1002, "grid_restored")
        self.store.add_chat(-1003, "no photo", None)
        self.store.toggle(-1003, "sync_photo")

        asyncio.run(publish_event(self.bot, self.store, GridTransition(True, 0.0, 60.0)))
        self.assertEqual([chat for chat, _ in self.session.sent], [-1001, -1003])
        self.assertEqual(set(self.session.photos), {-1001, -1002})

        self.session.sent.clear()
        self.session.photos.clear()
        asyncio.run(publish_event(self.bot, self.store, GridTransition(False, 0.0, 60.0)))
        self.assertEqual([chat for chat, _ in self.session.sent], [-1001, -1002, -1003])
        self.assertEqual(set(self.session.photos), {-1001, -1002})

    def test_battery_alert_respects_battery_toggle(self):
        self.store.add_chat(-1001, "on", None)
        self.store.add_chat(-1002, "off", None)
        self.store.toggle(-1002, "battery")

        asyncio.run(publish_event(self.bot, self.store, BatteryAlert(50, 49.4, False)))
        self.assertEqual([chat for chat, _ in self.session.sent], [-1001])
        body = self.session.sent[0][1]
        self.assertIn("заряд 49%", body)
        self.assertIn("поріг 50%", body)
        self.assertEqual(self.session.photos, [])

    def test_transition_body_matches_screenshot_style(self):
        self.store.add_chat(-1001, "g", None)
        asyncio.run(publish_event(self.bot, self.store, GridTransition(False, 0.0, 19 * 3600 + 14 * 60)))
        self.assertIn("Світло зникло", self.session.sent[0][1])
        self.assertIn("Воно було 19год 14хв", self.session.sent[0][1])


class SyncPhotoTest(BotTestCase):
    def test_skips_chats_that_disabled_photos(self):
        self.store.add_chat(-1001, "on", None)
        self.store.add_chat(-1002, "off", None)
        self.store.toggle(-1002, "sync_photo")
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.assertEqual(self.session.photos, [-1001])

    def test_records_the_state_it_applied(self):
        self.store.add_chat(-1001, "g", None)
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.assertIs(self.store.chat(-1001).photo_powered, False)

    def test_does_not_repaint_when_the_photo_already_matches(self):
        self.store.add_chat(-1001, "g", None)
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.session.photos.clear()
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.assertEqual(self.session.photos, [], "a repeated call must not touch the chat")

    def test_repaints_when_the_state_changed(self):
        self.store.add_chat(-1001, "g", None)
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.session.photos.clear()
        asyncio.run(sync_photos(self.bot, self.store, True))
        self.assertEqual(self.session.photos, [-1001])
        self.assertIs(self.store.chat(-1001).photo_powered, True)

    def test_force_repaints_even_when_it_matches(self):
        self.store.add_chat(-1001, "g", None)
        asyncio.run(sync_photos(self.bot, self.store, True))
        self.session.photos.clear()
        asyncio.run(sync_photos(self.bot, self.store, True, force=True))
        self.assertEqual(self.session.photos, [-1001])

    def test_failure_keeps_the_state_unknown_for_a_retry(self):
        self.store.add_chat(-1001, "g", None)
        original = self.session.make_request

        async def failing(bot, method, timeout=None):
            if isinstance(method, SetChatPhoto):
                raise TelegramBadRequest(method=method, message="not enough rights")
            return await original(bot, method, timeout)

        self.session.make_request = failing
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.assertIsNone(self.store.chat(-1001).photo_powered)
        self.session.make_request = original
        asyncio.run(sync_photos(self.bot, self.store, False))
        self.assertEqual(self.session.photos, [-1001])


class PublishRecoveryTest(BotTestCase):
    def test_goes_to_chats(self):
        self.store.add_chat(-1001, "g", None)
        asyncio.run(
            publish_recovery(
                self.bot,
                self.store,
                (42,),
                title="🔄 Бот був вимкнений",
                gap=900,
                powered_now=False,
                changed=True,
                soc=41.0,
            )
        )
        self.assertEqual([chat for chat, _ in self.session.sent], [-1001])
        body = self.session.sent[0][1]
        self.assertIn("Бот був вимкнений", body)
        self.assertIn("Без моніторингу 15хв", body)
        self.assertIn("Світла немає", body)

    def test_falls_back_to_admins_without_chats(self):
        asyncio.run(
            publish_recovery(
                self.bot,
                self.store,
                (42, 43),
                title="🟢 Моніторинг запущено",
                gap=0,
                powered_now=True,
                changed=False,
                soc=None,
            )
        )
        self.assertEqual([chat for chat, _ in self.session.sent], [42, 43])


class RefreshTest(BotTestCase):
    def test_refresh_resolves_titles_and_repaints(self):
        self.store.add_chat(-1001, "", None)
        self.session.titles[-1001] = "Мрії 11-Д"
        notes = asyncio.run(refresh_chats(self.bot, self.store, False))
        self.assertEqual(self.store.chat(-1001).title, "Мрії 11-Д")
        self.assertEqual(self.session.photos, [-1001])
        self.assertIn("назва й фото оновлені", notes[0])

    def test_refresh_reports_unreachable_chat(self):
        self.store.add_chat(-1002, "", None)
        self.session.failing_chats.add(-1002)
        notes = asyncio.run(refresh_chats(self.bot, self.store, False))
        self.assertIn("chat not found", notes[0])
        self.assertEqual(self.session.photos, [])

    def test_refresh_skips_photo_when_disabled(self):
        self.store.add_chat(-1001, "", None)
        self.store.toggle(-1001, "sync_photo")
        self.session.titles[-1001] = "Мрії 11-Д"
        notes = asyncio.run(refresh_chats(self.bot, self.store, True))
        self.assertEqual(self.session.photos, [])
        self.assertIn("назва оновлена", notes[0])


def make_update(text, chat_id, chat_type="private", from_user=None):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.datetime.now(),
            chat=Chat(id=chat_id, type=chat_type),
            from_user=from_user or User(id=999, is_bot=False, first_name="Stranger"),
            text=text,
        ),
    )


class RoleTest(unittest.TestCase):
    """Moderators read data; admin commands stay with the admin."""

    MODERATOR = 111111111
    ADMIN = 42
    GROUP = -1001234567890

    def setUp(self):
        from anenji_monitor.events import BatteryWatcher, GridWatcher, SignalEngine
        from anenji_monitor.registers import RegisterMap

        handle, self.path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        self.store = Store(self.path)
        self.store.add_chat(self.GROUP, "є свіло?", None)
        registers = RegisterMap.load("registers.toml")

        class PollerStub:
            state = MonitorState()

            async def read_registers(self, start, count):
                return {}

        poller = PollerStub()
        poller.state.latest = Reading(
            timestamp=time.time(), values=registers.convert_all({202: 0, 203: 0, 229: 75}), raw={}
        )
        engine = SignalEngine(
            self.store, GridWatcher(self.store), BatteryWatcher((50, 25))
        )
        token = "1:test"
        self.config = Config(
            telegram=TelegramConfig(
                token=token,
                admin_ids=(self.ADMIN,),
                moderator_ids=(self.MODERATOR,),
            ),
            inverter=InverterConfig(host="127.0.0.1"),
            alerts=AlertsConfig(),
        )
        self.ctx = BotContext(self.config, poller, registers, self.store, engine, 60)
        self.session = RecordingSession()
        self.bot = Bot(token=token, session=self.session)
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.ctx))
        self.dp.include_router(build_unauthorized_router(self.config.telegram.admin_ids))

    def tearDown(self):
        self.store.close()
        os.unlink(self.path)

    def ask(self, text: str, user_id: int, chat_type: str = "private") -> list[str]:
        self.session.sent.clear()
        self.session.captions.clear()
        chat_id = user_id if chat_type == "private" else self.GROUP
        asyncio.run(
            self.dp.feed_update(
                self.bot,
                Update(
                    update_id=1,
                    message=Message(
                        message_id=1,
                        date=datetime.datetime.now(),
                        chat=Chat(id=chat_id, type=chat_type),
                        from_user=User(id=user_id, is_bot=False, first_name="U"),
                        text=text,
                    ),
                ),
            )
        )
        return [body for _, body in self.session.sent] + self.session.captions

    def test_moderator_gets_info(self):
        replies = self.ask("/status", self.MODERATOR)
        self.assertEqual(len(replies), 1)
        self.assertIn("Світла немає", replies[0])

    def test_moderator_gets_stats(self):
        replies = self.ask("/stats", self.MODERATOR)
        self.assertEqual(len(replies), 1)
        self.assertIn("Статистика", replies[0])

    def test_moderator_is_refused_admin_commands(self):
        replies = self.ask("/reconnect", self.MODERATOR)
        self.assertEqual(len(replies), 1)
        self.assertIn("лише адміністратору", replies[0])

    def test_moderator_cannot_toggle_settings(self):
        replies = self.ask("/chats", self.MODERATOR)
        self.assertEqual(len(replies), 1)
        self.assertIn("лише адміністратору", replies[0])

    def test_admin_keeps_full_access(self):
        replies = self.ask("/chats", self.ADMIN)
        self.assertEqual(len(replies), 1)
        self.assertIn("Трансляція", replies[0])

    def test_stranger_is_silent_in_group(self):
        self.assertEqual(self.ask("/status", 111, chat_type="supergroup"), [])

    def test_moderator_works_in_group_too(self):
        replies = self.ask("/battery", self.MODERATOR, chat_type="supergroup")
        self.assertEqual(len(replies), 1)
        self.assertIn("Батарея", replies[0])


class UnauthorizedRouterTest(unittest.TestCase):
    def setUp(self):
        self.session = RecordingSession()
        self.bot = Bot(token="1:test", session=self.session)
        self.dp = Dispatcher()
        self.dp.include_router(build_unauthorized_router((42,)))

    def feed(self, update):
        asyncio.run(self.dp.feed_update(self.bot, update))

    def test_replies_in_private_chat(self):
        self.feed(make_update("/status", 999))
        self.assertEqual(len(self.session.sent), 1)
        self.assertIn("приватний", self.session.sent[0][1])

    def test_stays_silent_in_group(self):
        self.feed(make_update("/status", -1001234567890, chat_type="supergroup"))
        self.assertEqual(self.session.sent, [])

    def test_ignores_service_messages_from_bots(self):
        bot_user = User(id=777000111, is_bot=True, first_name="Є світло?")
        self.feed(make_update(None, 999, from_user=bot_user))
        self.assertEqual(self.session.sent, [])


class SettingsUiTest(unittest.TestCase):
    def test_keyboard_exposes_every_toggle(self):
        store = Store(":memory:")
        try:
            store.add_chat(-1001, "Мрії 11-Д", None)
            keyboard = settings_keyboard(store.chats())
            data = [button.callback_data for row in keyboard.inline_keyboard for button in row]
            self.assertIn("ev:lost:-1001", data)
            self.assertIn("ev:on:-1001", data)
            self.assertIn("ev:photo:-1001", data)
            self.assertIn("ev:batt:-1001", data)
            self.assertIn("ev:del:-1001", data)
            self.assertIn("ev:refresh:0", data)
        finally:
            store.close()

    def test_text_lists_flags(self):
        store = Store(":memory:")
        try:
            store.add_chat(-1001, "Мрії 11-Д", None)
            body = settings_text(store.chats())
            self.assertIn("Мрії 11-Д", body)
            self.assertIn("зникнення ✅", body)
            self.assertIn("фото ✅", body)
            self.assertIn("батарея ✅", body)
        finally:
            store.close()

    def test_empty_state_hint(self):
        self.assertIn("немає підписаних чатів", settings_text([]))

    def test_unknown_title_shows_hint(self):
        store = Store(":memory:")
        try:
            store.add_chat(-1001, "", None)
            self.assertIn("назва невідома", settings_text(store.chats()))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
