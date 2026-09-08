"""Feed actual aiogram updates through middleware, FSM and handlers; no Telegram network."""
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, SendDocument, SendMessage, SendPhoto
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app import bot as app
from app.db import Database


class Session(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        assert isinstance(method, (SendMessage, SendDocument, SendPhoto)), type(method)
        return Message(message_id=10000 + len(self.calls), date=datetime.now(timezone.utc),
                       chat=Chat(id=int(method.chat_id), type="private"), text=getattr(method, "text", ""))

    async def stream_content(self, *args, **kwargs):
        yield b""


class ChatHarness:
    def __init__(self):
        self.session = Session()
        self.bot = Bot("123456:UNIT_TEST_TOKEN_NOT_REAL", session=self.session)
        self.dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
        router = Router()
        router.message.outer_middleware(app.AccessMiddleware())
        router.callback_query.outer_middleware(app.AccessMiddleware())
        router.message.register(app.handle_message)
        router.callback_query.register(app.handle_callback)
        self.dispatcher.include_router(router)
        self.counter = 0

    async def text(self, text, user=1, chat_type="private"):
        self.counter += 1
        message = Message(message_id=self.counter, date=datetime.now(timezone.utc),
                          chat=Chat(id=user, type=chat_type), from_user=User(id=user, is_bot=False, first_name="Test"), text=text)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.counter, message=message))

    async def tap(self, data, user=1):
        self.counter += 1
        message = Message(message_id=9999, date=datetime.now(timezone.utc), chat=Chat(id=user, type="private"))
        callback = CallbackQuery(id=str(self.counter), from_user=User(id=user, is_bot=False, first_name="Test"),
                                 chat_instance="unit-test", message=message, data=data)
        await self.dispatcher.feed_update(self.bot, Update(update_id=self.counter, callback_query=callback))

    def button(self, label):
        for call in reversed(self.session.calls):
            markup = getattr(call, "reply_markup", None)
            for row in getattr(markup, "inline_keyboard", []):
                for button in row:
                    if label in button.text:
                        return button.callback_data
        raise AssertionError(f"Button not found: {label}")


def test_mobile_flow_cancellation_dates_edit_delete_export_and_limits(tmp_path, monkeypatch):
    async def scenario():
        db = Database(tmp_path / "bot.db")
        await db.init()
        monkeypatch.setattr(app, "db", db, raising=False)
        monkeypatch.setattr(app, "allowed_user_ids", frozenset({1}))
        chat = ChatHarness()
        await chat.text("/start")
        await chat.text("➖ Расход")
        await chat.text("1 250,50")
        await chat.tap(chat.button("Продукты"))
        await chat.tap(chat.button("Дата"))
        await chat.text("01.09.2025")
        save = chat.button("Сохранить")
        await chat.tap(save)
        await chat.tap(save)
        assert len(await db.transactions(1, "2025-09")) == 1
        assert (await db.summary(1, "2025-09"))["expense"] == 1250.50
        await chat.tap("month:2025-09")
        await chat.text("📝 История")
        await chat.tap(chat.button("продукты"))
        await chat.tap(chat.button("Изменить"))
        await chat.tap(chat.button("Сумма"))
        await chat.text("100,25")
        await chat.tap(chat.button("Сохранить"))
        assert (await db.summary(1, "2025-09"))["expense"] == 100.25
        await chat.text("🎯 Лимиты")
        await chat.tap(chat.button("Задать"))
        await chat.tap(chat.button("Продукты"))
        await chat.text("200")
        assert (await db.budget_report(1, "2025-09"))[0]["limit"] == 200
        await chat.text("📁 Скачать Excel")
        assert any(isinstance(call, SendDocument) and call.document.filename == "budget_2025-09.xlsx" for call in chat.session.calls)
        await chat.text("➕ Доход")
        await chat.text("1000")
        await chat.text("📝 История")  # Menu switches work even while waiting for a category.
        await chat.tap(chat.button("продукты"))
        await chat.tap(chat.button("Удалить"))
        assert len(await db.transactions(1, "2025-09")) == 1
        await chat.tap(chat.button("Да, удалить"))
        assert not await db.transactions(1, "2025-09")
        await chat.text("продукты 850")
        old_save = chat.button("Сохранить")
        await chat.text("/cancel")
        await chat.tap(old_save)
        assert (await db.summary(1))["expense"] == 0
        await chat.bot.session.close()
        await chat.dispatcher.storage.close()
    asyncio.run(scenario())


def test_access_gate_covers_every_message_and_callback(tmp_path, monkeypatch):
    async def scenario():
        db = Database(tmp_path / "private.db")
        await db.init()
        monkeypatch.setattr(app, "db", db, raising=False)
        monkeypatch.setattr(app, "allowed_user_ids", frozenset({1}))
        chat = ChatHarness()
        await chat.text("продукты 850", user=2)
        await chat.tap("month:2025-09", user=2)
        await chat.text("📁 Скачать Excel", user=2)
        assert not await db.transactions(2, "2025-09")
        assert not any(isinstance(call, SendDocument) for call in chat.session.calls)
        assert all("нет доступа" in call.text for call in chat.session.calls)
        await chat.text("📊 Мой бюджет", user=1, chat_type="group")
        assert "личный чат" in chat.session.calls[-1].text
    asyncio.run(scenario())
