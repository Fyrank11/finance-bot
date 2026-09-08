import asyncio
from datetime import datetime, timezone
import sqlite3

import pytest
from aiogram.types import CallbackQuery, Chat, Message, User

from app.db import Database
from app.keyboards import MAIN_MENU
from app import release_runtime as runtime


class FormState:
    def __init__(self, state=None):
        self.value = state

    async def get_state(self):
        return self.value


def message(user_id=101, text="Привет", *, chat_type="private", chat_id=None):
    return Message(
        message_id=1, date=datetime.now(timezone.utc), text=text,
        from_user=User(id=user_id, is_bot=False, first_name="Пользователь"),
        chat=Chat(id=chat_id if chat_id is not None else user_id, type=chat_type),
    )


def seen_rows(db):
    with sqlite3.connect(db.path) as connection:
        return connection.execute("SELECT user_id,version FROM bot_ui_seen ORDER BY user_id").fetchall()


def test_release_identifier_accepts_only_a_complete_commit_sha(monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "A1" * 20)
    assert runtime.get_release_version() == "a1" * 6
    for value in ("", "abc123", "a" * 41, "z" * 40, "a" * 40 + "\n", "unsafe release text"):
        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", value)
        assert runtime.get_release_version() == runtime.APP_VERSION
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA")
    assert runtime.get_release_version() == runtime.APP_VERSION


def test_refresh_deferred_until_form_finishes_and_persists_after_restart(tmp_path, monkeypatch):
    sent = []

    async def answer(self, text, **kwargs):
        sent.append((self.chat.id, text, kwargs))

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)

    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await runtime.init_runtime(db)
        form = FormState("Form:amount")
        assert not await runtime.refresh_menu_if_needed(db, message(), form, MAIN_MENU)
        assert seen_rows(db) == []
        assert sent == []
        form.value = None
        assert await runtime.refresh_menu_if_needed(db, message(), form, MAIN_MENU)
        assert len(sent) == 1
        assert sent[0][2]["reply_markup"] is MAIN_MENU

        restarted = Database(db.path)
        await restarted.init()
        await runtime.init_runtime(restarted)
        assert not await runtime.refresh_menu_if_needed(restarted, message(), FormState(), MAIN_MENU)
        assert len(sent) == 1
        assert seen_rows(db) == [(101, runtime.APP_VERSION)]

    asyncio.run(scenario())


def test_menu_versions_are_per_sender_and_refresh_for_new_release(tmp_path, monkeypatch):
    sent = []

    async def answer(self, text, **kwargs):
        sent.append(self.chat.id)

    monkeypatch.setattr(Message, "answer", answer)
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "a" * 40)

    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        # Callback message.from_user is the bot; its sender is callback.from_user.
        bot_message = message(999, chat_id=101)
        event = CallbackQuery(
            id="callback", from_user=message(101).from_user, chat_instance="test",
            message=bot_message, data="menu",
        )
        assert await runtime.refresh_menu_if_needed(db, event, FormState(), MAIN_MENU)
        assert await runtime.refresh_menu_if_needed(db, message(202), FormState(), MAIN_MENU)
        assert seen_rows(db) == [(101, "a" * 12), (202, "a" * 12)]

        monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "b" * 40)
        assert await runtime.refresh_menu_if_needed(db, event, FormState(), MAIN_MENU)
        assert seen_rows(db) == [(101, "b" * 12), (202, "a" * 12)]
        assert sent == [101, 202, 101]

    asyncio.run(scenario())


def test_failed_delivery_is_not_marked_seen_and_can_retry(tmp_path, monkeypatch):
    attempts = []

    async def answer(self, text, **kwargs):
        attempts.append(text)
        if len(attempts) == 1:
            raise ConnectionError("Telegram unavailable")

    monkeypatch.setattr(Message, "answer", answer)

    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        with pytest.raises(ConnectionError):
            await runtime.refresh_menu_if_needed(db, message(), FormState(), MAIN_MENU)
        assert seen_rows(db) == []
        assert await runtime.refresh_menu_if_needed(db, message(), FormState(), MAIN_MENU)
        assert len(attempts) == 2

    asyncio.run(scenario())


def test_concurrent_refreshes_send_once(tmp_path, monkeypatch):
    sent = []

    async def answer(self, text, **kwargs):
        await asyncio.sleep(0)
        sent.append(text)

    monkeypatch.setattr(Message, "answer", answer)

    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        results = await asyncio.gather(*(
            runtime.refresh_menu_if_needed(db, message(), FormState(), MAIN_MENU)
            for _ in range(5)
        ))
        assert results.count(True) == 1
        assert len(sent) == 1

    asyncio.run(scenario())


def test_start_and_menu_own_their_keyboard_and_require_explicit_mark(tmp_path, monkeypatch):
    async def unexpected_answer(self, text, **kwargs):
        raise AssertionError("No automatic notice for explicit menu commands")

    monkeypatch.setattr(Message, "answer", unexpected_answer)

    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await runtime.init_runtime(db)
        for text in ("/start", "/start invite", "/menu@my_finance_advisor_bot", "/menu  "):
            assert not await runtime.refresh_menu_if_needed(db, message(text=text), FormState(), MAIN_MENU)
        assert seen_rows(db) == []
        await runtime.mark_ui_seen(db, 101)
        assert seen_rows(db) == [(101, runtime.get_release_version())]

    asyncio.run(scenario())


def test_nonprivate_inline_and_missing_state_cannot_trigger_a_notice(tmp_path, monkeypatch):
    async def unexpected_answer(self, text, **kwargs):
        raise AssertionError("No keyboard outside an authenticated private conversation")

    monkeypatch.setattr(Message, "answer", unexpected_answer)

    async def scenario():
        db = Database(tmp_path / "does-not-exist" / "budget.db")
        events = [
            message(chat_type="group", chat_id=-100),
            CallbackQuery(id="inline", from_user=message().from_user, chat_instance="test", inline_message_id="x"),
            object(),
        ]
        for event in events:
            assert not await runtime.refresh_menu_if_needed(db, event, FormState(), MAIN_MENU)
        assert not await runtime.refresh_menu_if_needed(db, message(), None, MAIN_MENU)
        assert not db.path.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("user_id", [True, 0, -1, "101", 2**63])
def test_ui_seen_rejects_nonuser_identifiers(tmp_path, user_id):
    async def scenario():
        db = Database(tmp_path / "does-not-exist" / "budget.db")
        with pytest.raises(ValueError, match="Telegram ID"):
            await runtime.mark_ui_seen(db, user_id)
        assert not db.path.exists()

    asyncio.run(scenario())


def test_drain_waits_for_handlers_including_lock_waiters():
    async def scenario():
        tracker = runtime.InFlightUpdates()
        lock = asyncio.Lock()
        entered = asyncio.Event()
        release = asyncio.Event()
        finished = []

        async def handler(event, data):
            async with lock:
                entered.set()
                await release.wait()
                finished.append(event)
                return event

        first = asyncio.create_task(tracker(handler, "first", {}))
        await entered.wait()
        second = asyncio.create_task(tracker(handler, "second", {}))
        await asyncio.sleep(0)
        draining = asyncio.create_task(tracker.drain(timeout=1))
        await asyncio.sleep(0)
        assert not draining.done()
        release.set()
        await draining
        assert await first == "first"
        assert await second == "second"
        assert finished == ["first", "second"]
        assert tracker._tasks == set()

    asyncio.run(scenario())


def test_drain_timeout_cancels_and_awaits_handler_cleanup():
    async def scenario():
        tracker = runtime.InFlightUpdates()
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def handler(event, data):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        pending = asyncio.create_task(tracker(handler, None, {}))
        await entered.wait()
        await tracker.drain(timeout=0)
        assert pending.cancelled()
        assert cleaned.is_set()
        assert tracker._tasks == set()

    asyncio.run(scenario())


def test_drain_does_not_wait_for_itself_and_exceptions_unregister():
    async def scenario():
        tracker = runtime.InFlightUpdates()

        async def handler(event, data):
            await tracker.drain(timeout=0)
            raise ValueError("handler failed")

        with pytest.raises(ValueError, match="handler failed"):
            await tracker(handler, None, {})
        assert tracker._tasks == set()
        await tracker.shutdown(dispatcher=object())

    asyncio.run(scenario())
