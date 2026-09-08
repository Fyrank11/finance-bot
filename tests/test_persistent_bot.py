"""Production dispatcher composition and real FSM continuation after upgrades."""
import asyncio
from types import SimpleNamespace

from aiogram import Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
import pytest

from app import bot as app
from app import family, savings
from app.db import Database
from app.inputs import today
from app.release_runtime import InFlightUpdates, MENU_UPDATED_TEXT, mark_ui_seen
from app.savings_ui import SavingsForm
from app.session_store import SQLiteStorage
from test_bot import ChatHarness, Session


class PersistentChatHarness(ChatHarness):
    """Run main() with its actual dispatcher/middleware setup, replacing network IO."""

    @classmethod
    async def create(cls, path, monkeypatch, *, counter=0, message_handler=None):
        self = cls.__new__(cls)
        self.session = Session()
        self.bot = Bot("123456:UNIT_TEST_TOKEN_NOT_REAL", session=self.session)
        self.counter = counter

        async def no_network(*args, **kwargs):
            pass

        def make_dispatcher(**kwargs):
            self.dispatcher = Dispatcher(**kwargs)
            self.dispatcher.start_polling = no_network
            return self.dispatcher

        router = Router()
        router.message.outer_middleware(app.AccessMiddleware())
        router.callback_query.outer_middleware(app.AccessMiddleware())
        router.message.register(message_handler or app.handle_message)
        router.callback_query.register(app.handle_callback)
        settings = SimpleNamespace(
            bot_token="123456:UNIT_TEST_TOKEN_NOT_REAL", db_path=path,
            timezone="Europe/Moscow", allowed_user_ids=frozenset({1, 2}), public_signup=False,
        )
        # main() assigns these globals. Register restoration with monkeypatch too.
        monkeypatch.setattr(app, "db", Database(path), raising=False)
        monkeypatch.setattr(app, "allowed_user_ids", frozenset({1, 2}))
        monkeypatch.setattr(app, "public_signup", False)
        monkeypatch.setattr(app, "load_settings", lambda: settings)
        monkeypatch.setattr(app, "Dispatcher", make_dispatcher)
        monkeypatch.setattr(app, "Bot", lambda *args, **kwargs: self.bot)
        monkeypatch.setattr(app, "router", router)
        monkeypatch.setattr(app, "apply_text_profile", no_network)
        monkeypatch.setattr(self.bot, "set_my_commands", no_network)
        await app.main()
        self.db = app.db
        assert isinstance(self.dispatcher.storage, SQLiteStorage)
        return self

    def state(self, user=1):
        return FSMContext(
            self.dispatcher.storage,
            StorageKey(bot_id=self.bot.id, chat_id=user, user_id=user),
        )

    async def close(self):
        await self.dispatcher.emit_shutdown(bot=self.bot)
        await self.bot.session.close()

    async def restart(self, monkeypatch):
        await self.close()
        return await self.create(self.db.path, monkeypatch, counter=self.counter)


async def goal_draft(chat, *, user=1):
    await chat.text("/savings", user=user)
    await chat.tap(chat.button("Мои цели"), user=user)
    await chat.tap(chat.button("Новая цель"), user=user)
    await chat.text("Поездка", user=user)
    await chat.text("180000", user=user)


def test_expense_form_continues_after_restart_and_old_save_cannot_duplicate(tmp_path, monkeypatch):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("/start")
        await chat.text("➖ Расход")
        await chat.text("850,25")
        category = chat.button("Продукты")
        original_draft = await chat.state().get_data()
        assert await chat.state().get_state() == app.Form.category.state

        chat = await chat.restart(monkeypatch)
        assert await chat.state().get_data() == original_draft
        assert await chat.state().get_state() == app.Form.category.state
        await chat.tap(category)
        save = chat.button("Сохранить")
        await chat.tap(save)
        await chat.tap(save)
        chat = await chat.restart(monkeypatch)
        await chat.tap(save)

        rows = await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        assert len(rows) == 1
        assert rows[0]["amount_minor"] == 85025
        assert rows[0]["category"] == "продукты"
        assert await chat.state().get_state() is None
        await chat.close()

    asyncio.run(scenario())


def test_savings_goal_continues_after_restart_without_creating_money_movements(tmp_path, monkeypatch):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await goal_draft(chat)
        assert await chat.state().get_state() == SavingsForm.goal_saved.state
        original = await chat.state().get_data()

        chat = await chat.restart(monkeypatch)
        assert await chat.state().get_data() == original
        await chat.text("0")
        await chat.text("18")
        await chat.text("6000")
        save = chat.button("Сохранить")
        await chat.tap(save)
        chat = await chat.restart(monkeypatch)
        await chat.tap(save)

        goals = await savings.list_goals(chat.db, 1)
        assert len(goals) == 1
        assert goals[0]["name"] == "Поездка"
        assert goals[0]["target_minor"] == 18000000
        assert goals[0]["monthly_minor"] == 600000
        assert not await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        await chat.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("draft_kind", ["expense", "goal"])
def test_family_exit_invalidates_persisted_confirmation_after_restart(tmp_path, monkeypatch, draft_kind):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        shared = await family.create_household(chat.db, 1)
        code = await family.create_invite(chat.db, 1)
        await family.join_household(chat.db, 2, code)
        if draft_kind == "expense":
            await chat.text("кофе 350", user=2)
        else:
            await goal_draft(chat, user=2)
            await chat.text("0", user=2)
            await chat.text("18", user=2)
            await chat.text("6000", user=2)
        save = chat.button("Сохранить")
        assert (await chat.state(2).get_data())["_scope"].startswith(f"{shared}:")
        await chat.close()
        await family.leave_household(chat.db, 2)

        chat = await PersistentChatHarness.create(chat.db.path, monkeypatch, counter=chat.counter)
        await chat.tap(save, user=2)
        assert any("доступ изменился" in (getattr(call, "text", None) or "") for call in chat.session.calls)
        assert await chat.state(2).get_state() is None
        for budget_id in (shared, 2):
            assert not await savings.list_goals(chat.db, budget_id)
            assert not await chat.db.transactions(budget_id, today(chat.db.timezone).strftime("%Y-%m"))
        await chat.text("/savings", user=2)
        assert "личный" in chat.session.calls[-1].text
        await chat.close()

    asyncio.run(scenario())


def test_release_menu_refresh_waits_for_form_completion_without_start(tmp_path, monkeypatch):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("/start")
        await chat.text("➖ Расход")
        await mark_ui_seen(chat.db, 1, version="older-release")
        chat = await chat.restart(monkeypatch)

        await chat.text("123")
        assert await chat.state().get_state() == app.Form.category.state
        assert not any(getattr(call, "text", None) == MENU_UPDATED_TEXT for call in chat.session.calls)
        await chat.tap(chat.button("Продукты"))
        await chat.tap(chat.button("Сохранить"))
        notices = [call for call in chat.session.calls if getattr(call, "text", None) == MENU_UPDATED_TEXT]
        assert len(notices) == 1
        assert notices[0].reply_markup == app.MAIN_MENU
        await chat.text("📊 Мой бюджет")
        assert sum(getattr(call, "text", None) == MENU_UPDATED_TEXT for call in chat.session.calls) == 1
        assert await chat.state().get_state() is None
        await chat.close()

    asyncio.run(scenario())


def test_production_tracker_includes_events_waiting_for_fsm_lock(tmp_path, monkeypatch):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        async def message_handler(message, state):
            assert isinstance(state, FSMContext)
            calls.append(message.text)
            entered.set()
            await release.wait()
            await state.update_data(**{message.text: True})

        chat = await PersistentChatHarness.create(
            tmp_path / "bot.db", monkeypatch, message_handler=message_handler,
        )
        tracker = next(middleware for middleware in chat.dispatcher.update.outer_middleware
                       if isinstance(middleware, InFlightUpdates))
        closed_with_tasks = []
        original_close = SQLiteStorage.close

        async def observe_close(storage):
            closed_with_tasks.append(len(tracker._tasks))
            await original_close(storage)

        monkeypatch.setattr(SQLiteStorage, "close", observe_close)
        first = asyncio.create_task(chat.text("first"))
        await entered.wait()
        second = asyncio.create_task(chat.text("second"))
        # feed_update reaches the outer tracker before awaiting the occupied FSM lock.
        for _ in range(3):
            await asyncio.sleep(0)
        assert len(tracker._tasks) == 2
        assert calls == ["first"]
        draining = asyncio.create_task(chat.dispatcher.emit_shutdown(bot=chat.bot))
        await asyncio.sleep(0)
        assert not draining.done()
        assert closed_with_tasks == []
        release.set()
        await asyncio.gather(first, second, draining)
        assert calls == ["first", "second"]
        assert tracker._tasks == set()
        assert closed_with_tasks == [0]
        assert (await chat.state().get_data()) == {"first": True, "second": True, "_scope": "1:0"}
        await chat.bot.session.close()

    asyncio.run(scenario())
