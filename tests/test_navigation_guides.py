"""Use the production dispatcher to exercise navigation and help during input."""

import asyncio

import pytest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto

from app import bot as app
from app import family, guides, savings
from app.inputs import today
from app.keyboards import MAIN_MENU
from app.savings_ui import SavingsForm
from test_persistent_bot import PersistentChatHarness


@pytest.fixture
def guide_assets(tmp_path, monkeypatch):
    # Rendering is covered by test_guide_art. These tests exercise real Telegram
    # method construction and delivery bookkeeping with an in-memory session.
    poster = tmp_path / "welcome-poster.png"
    poster.write_bytes(b"welcome fixture")
    monkeypatch.setattr(guides, "WELCOME_POSTER", poster)
    monkeypatch.setattr(guides, "render_guide", lambda topic: b"guide fixture")
    return poster


def photos(chat, topic=None, *, user=None):
    filename = None if topic is None else ("welcome-poster.png" if topic == "welcome" else f"guide-{topic}.png")
    return [call for call in chat.session.calls if isinstance(call, SendPhoto)
            and (filename is None or getattr(call.photo, "filename", None) == filename)
            and (user is None or int(call.chat_id) == user)]


def buttons_in(call):
    return [button for row in getattr(getattr(call, "reply_markup", None), "inline_keyboard", []) for button in row]


def last_message(chat):
    return next(call for call in reversed(chat.session.calls) if isinstance(call, SendMessage))


def last_buttons(chat):
    return {button.callback_data for button in buttons_in(last_message(chat))}


def test_start_welcome_survives_restart_and_is_per_user_with_manual_replay(tmp_path, monkeypatch, guide_assets):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("/start")
        await chat.text("/start")
        assert len(photos(chat, "welcome", user=1)) == 1
        assert {button.callback_data for button in buttons_in(photos(chat, "welcome")[0])} == {"nav:begin", "nav:help"}

        chat = await chat.restart(monkeypatch)
        await chat.text("/start")
        assert not photos(chat, "welcome", user=1)
        await chat.text("/start", user=2)
        assert len(photos(chat, "welcome", user=2)) == 1
        await chat.tap("guide:welcome", user=1)
        assert len(photos(chat, "welcome", user=1)) == 1
        before = len(chat.session.calls)
        await chat.tap("nav:welcome", user=1)
        delivered = chat.session.calls[before:]
        assert next(i for i, call in enumerate(delivered) if isinstance(call, AnswerCallbackQuery)) < next(i for i, call in enumerate(delivered) if isinstance(call, SendPhoto))
        await chat.tap("nav:begin", user=1)
        assert last_message(chat).reply_markup == MAIN_MENU
        assert not await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        await chat.close()

    asyncio.run(scenario())


def test_six_main_buttons_keep_capture_visible_and_budget_leaf_guides_are_once(tmp_path, monkeypatch, guide_assets):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("/menu")
        reply = last_message(chat).reply_markup
        assert sum(len(row) for row in reply.keyboard) == 6
        assert {button.text for button in reply.keyboard[0]} == {"➕ Доход", "➖ Расход"}
        await chat.text("📊 Мой бюджет")
        assert "Расходы:" in last_message(chat).text
        assert len(photos(chat, "budget")) == 1
        history_route = chat.button("История")
        assert history_route.startswith("scope:") and history_route.endswith(":nav:history")
        assert "guide:history" in last_buttons(chat)
        before = len(chat.session.calls)
        await chat.tap(history_route)
        delivered = chat.session.calls[before:]
        assert next(i for i, call in enumerate(delivered) if isinstance(call, AnswerCallbackQuery)) < next(i for i, call in enumerate(delivered) if isinstance(call, SendPhoto))
        assert len(photos(chat, "history")) == 1
        assert "guide:history" in last_buttons(chat)
        await chat.text("📝 История")
        await chat.text("📊 Сводка")
        assert len(photos(chat, "history")) == 1
        assert len(photos(chat, "budget")) == 1

        chat = await chat.restart(monkeypatch)
        await chat.text("📝 История")
        assert not photos(chat, "history")
        await chat.tap("guide:history")
        assert len(photos(chat, "history")) == 1
        await chat.close()

    asyncio.run(scenario())


def test_plan_subsections_and_help_index_reach_all_instruction_topics(tmp_path, monkeypatch, guide_assets):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("🧭 Планы")
        assert {"guide:payments", "guide:savings", "guide:goals", "guide:forecast", "guide:debts"} <= last_buttons(chat)
        await chat.tap(chat.button("Накопления"))
        assert len(photos(chat, "savings")) == 1
        assert "guide:savings" in last_buttons(chat)
        goal_route = chat.button("Мои цели")
        budget_route = chat.button("Возможности бюджета")
        await chat.tap(chat.button("Резерв"))
        assert len(photos(chat, "reserve")) == 1
        assert "guide:reserve" in last_buttons(chat)
        await chat.tap(goal_route)
        assert "guide:goals" in last_buttons(chat)
        await chat.tap(budget_route)
        assert "guide:savings_budget" in last_buttons(chat)
        await chat.text("⚙️ Настройки и помощь")
        assert {"guide:settings", "guide:family"} <= last_buttons(chat)
        await chat.tap(chat.button("Настройки бюджета"))
        assert {"guide:opening", "guide:month", "guide:settings"} <= last_buttons(chat)

        await chat.text("/help")
        reachable = set()
        for _ in range(10):
            current = last_buttons(chat)
            reachable.update(value.removeprefix("guide:") for value in current if value.startswith("guide:"))
            forward = next((button.callback_data for button in buttons_in(last_message(chat)) if button.text == "Далее ›"), None)
            if forward is None:
                break
            await chat.tap(forward)
        else:
            raise AssertionError("Help index did not terminate")
        assert reachable == set(guides.GUIDE_CATALOG) | {"welcome"}
        await chat.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("quick", [False, True])
def test_expense_help_preserves_draft_scope_and_token_then_saves_once(tmp_path, monkeypatch, guide_assets, quick):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        if quick:
            await chat.text("продукты 125,50")
            expected_state = app.Form.confirm.state
        else:
            await chat.text("➖ Расход")
            await chat.text("125,50")
            expected_state = app.Form.category.state
        continuing_button = chat.button("Сохранить" if quick else "Продукты")
        saved_state = await chat.state().get_data()
        assert saved_state["_help_topic"] == "expense"
        assert saved_state["_scope"]
        before = len(photos(chat, "expense"))
        await chat.text("ℹ️ Как это работает")
        assert len(photos(chat, "expense")) == before + 1
        await chat.tap("guide:expense")
        await chat.tap("nav:help")
        await chat.text("/help expense")
        assert await chat.state().get_state() == expected_state
        assert await chat.state().get_data() == saved_state

        chat = await chat.restart(monkeypatch)
        assert await chat.state().get_data() == saved_state
        if not quick:
            await chat.tap(continuing_button)
            save = chat.button("Сохранить")
        else:
            # The callback remains valid after a help card and server restart.
            save = continuing_button
        await chat.tap(save)
        await chat.tap(save)
        recorded = await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        assert len(recorded) == 1 and recorded[0]["amount_minor"] == 12550
        assert await chat.state().get_state() is None
        await chat.close()

    asyncio.run(scenario())


def test_goal_help_preserves_creation_and_confirmation_across_restart(tmp_path, monkeypatch, guide_assets):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("/savings")
        await chat.tap(chat.button("Мои цели"))
        await chat.tap(chat.button("Новая цель"))
        await chat.text("Поездка")
        before = await chat.state().get_data()
        assert await chat.state().get_state() == SavingsForm.goal_target.state
        count = len(photos(chat, "goals"))
        await chat.text("ℹ️ Как это работает")
        assert len(photos(chat, "goals")) == count + 1
        await chat.tap("guide:goals")
        await chat.tap("nav:help")
        assert await chat.state().get_data() == before
        chat = await chat.restart(monkeypatch)
        assert await chat.state().get_data() == before
        for value in ("120000", "10000", "12", "5000"):
            await chat.text(value)
        confirm = chat.button("Сохранить")
        final_draft = await chat.state().get_data()
        await chat.text("/help goals")
        assert await chat.state().get_state() == SavingsForm.confirm.state
        assert await chat.state().get_data() == final_draft
        await chat.tap(confirm)
        await chat.tap(confirm)
        goals = await savings.list_goals(chat.db, 1)
        assert len(goals) == 1 and goals[0]["name"] == "Поездка"
        assert not await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        await chat.close()

    asyncio.run(scenario())


def test_copied_and_stale_financial_navigation_rejects_but_public_help_is_safe(tmp_path, monkeypatch, guide_assets):
    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        await chat.text("📊 Мой бюджет", user=1)
        personal_history = chat.button("История")
        start = len(chat.session.calls)
        await chat.tap(personal_history, user=2)
        new_calls = chat.session.calls[start:]
        assert any(isinstance(call, AnswerCallbackQuery) and "другого бюджета" in (call.text or "") for call in new_calls)
        assert not any(isinstance(call, SendPhoto) for call in new_calls)
        await chat.tap("guide:history", user=2)
        assert len(photos(chat, "history", user=2)) == 1

        shared = await family.create_household(chat.db, 1)
        await chat.text("📊 Мой бюджет")
        shared_history = chat.button("История")
        assert shared_history.startswith(f"scope:{shared}:")
        await family.switch_budget(chat.db, 1, shared=False)
        start = len(chat.session.calls)
        await chat.tap(shared_history)
        new_calls = chat.session.calls[start:]
        assert any(isinstance(call, AnswerCallbackQuery) and "изменился" in (call.text or "") for call in new_calls)
        assert not any(isinstance(call, SendPhoto) for call in new_calls)
        await chat.tap("guide:history")
        assert len(photos(chat, "history", user=1)) == 1
        # A generic card is still safe as the very first action after a scope
        # change; it cannot revive a draft belonging to the previous budget.
        await chat.text("кофе 350")
        await family.switch_budget(chat.db, 1, shared=True)
        await chat.tap("guide:history")
        assert len(photos(chat, "history", user=1)) == 2
        assert await chat.state().get_state() is None
        assert not await chat.db.transactions(shared, today(chat.db.timezone).strftime("%Y-%m"))
        await chat.close()

    asyncio.run(scenario())


def test_missing_poster_and_failed_card_render_leave_core_feature_usable(tmp_path, monkeypatch, guide_assets):
    def broken(topic):
        raise OSError("test render unavailable")

    async def scenario():
        chat = await PersistentChatHarness.create(tmp_path / "bot.db", monkeypatch)
        monkeypatch.setattr(guides, "WELCOME_POSTER", tmp_path / "not-present.png")
        await chat.text("/start")
        assert not photos(chat, "welcome")
        assert last_message(chat).reply_markup == MAIN_MENU
        monkeypatch.setattr(guides, "render_guide", broken)
        await chat.text("➖ Расход")
        assert await chat.state().get_state() == app.Form.amount.state
        await chat.text("500 продукты")
        await chat.tap(chat.button("Сохранить"))
        recorded = await chat.db.transactions(1, today(chat.db.timezone).strftime("%Y-%m"))
        assert len(recorded) == 1 and recorded[0]["amount_minor"] == 50000
        assert not photos(chat, "expense")
        monkeypatch.setattr(guides, "render_guide", lambda topic: b"recovered image")
        await chat.text("➖ Расход")
        assert len(photos(chat, "expense")) == 1
        await chat.close()

    asyncio.run(scenario())
