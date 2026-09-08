"""Real dispatcher flows for enrollment and personal/family access boundaries."""
import asyncio
from datetime import date
import sqlite3

import pytest

from app import bot as app, family
from app.access import is_registered
from app.db import Database
from app.rate_limit import RateLimiter
from test_bot import ChatHarness


async def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / "signup.db")
    await db.init()
    monkeypatch.setattr(app, "db", db, raising=False)
    monkeypatch.setattr(app, "public_signup", True)
    monkeypatch.setattr(app, "allowed_user_ids", frozenset({1}))
    monkeypatch.setattr(app, "rate_limiter", RateLimiter())
    monkeypatch.setattr(app, "today", lambda timezone: date(2025, 9, 8))
    return db, ChatHarness()


def response_text(chat, since=0):
    texts = []
    for call in chat.session.calls[since:]:
        texts.append(getattr(call, "text", "") or "")
        markup = getattr(call, "reply_markup", None)
        for row in getattr(markup, "inline_keyboard", []):
            texts.extend(button.text for button in row)
    return "\n".join(texts)


async def close(chat):
    await chat.bot.session.close()
    await chat.dispatcher.storage.close()


def test_start_prompt_and_privacy_do_not_enroll_until_start(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        for action in ("кофе 350", "/tips", "📁 Скачать Excel"):
            await chat.text(action, user=2)
            assert "/start" in chat.session.calls[-1].text
            assert not await is_registered(db, 2)
        await chat.tap("month:2025-09", user=2)
        assert "/start" in chat.session.calls[-1].text
        assert not await is_registered(db, 2)
        await chat.text("/privacy", user=2)
        privacy = chat.session.calls[-1].text
        assert "Telegram ID" in privacy and "Администратор" in privacy
        assert not await is_registered(db, 2)
        await chat.text("/whoami", user=2)
        assert "Telegram ID: 2" in chat.session.calls[-1].text
        assert "/start" in chat.session.calls[-1].text
        assert not await is_registered(db, 2)
        await chat.text("/start", user=2)
        assert await is_registered(db, 2)
        assert "Личный бюджет" in chat.session.calls[-1].text
        assert await db.transactions(2, "2025-09") == []
        await close(chat)

    asyncio.run(scenario())


def test_start_uses_sender_and_family_join_requires_separate_confirmation(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        shared = await family.create_household(db, 1)
        code = await family.create_invite(db, 1)
        await chat.text("/start 1", user=2)
        await chat.text(f"/start {code}", user=3)
        assert await is_registered(db, 2)
        assert await is_registered(db, 3)
        assert not await is_registered(db, 1)  # Payload never registers its ID.
        for user in (2, 3):
            assert await family.active_budget_id(db, user) == user
            assert await family.household_info(db, user) is None
        assert len((await family.household_info(db, 1))["members"]) == 1

        await chat.text("/family", user=3)
        await chat.tap(chat.button("Ввести код"), user=3)
        await chat.text(code, user=3)
        assert await family.household_info(db, 3) is None
        await chat.tap(chat.button("Да, вступить"), user=3)
        assert await family.active_budget_id(db, 3) == shared
        assert await family.household_info(db, 2) is None
        await close(chat)

    asyncio.run(scenario())


def test_repeated_start_and_restart_preserve_settings_history_and_family(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text("/start", user=2)
        await db.set_opening(2, 2345.67)
        await db.select_month(2, "2025-09")
        await db.set_budget(2, "2025-09", "продукты", 500)
        await chat.text("продукты 125,50; личная запись", user=2)
        await chat.tap(chat.button("Сохранить"), user=2)
        saved = await db.transactions(2, "2025-09")
        summary = await db.summary(2, "2025-09")
        limits = await db.budget_report(2, "2025-09")
        shared = await family.create_household(db, 2)
        await db.set_opening(shared, 789)
        await db.select_month(shared, "2025-08")
        family_context = await family.active_budget_context(db, 2)
        with sqlite3.connect(db.path) as connection:
            registration = connection.execute("SELECT * FROM access_users WHERE user_id=2").fetchone()

        await chat.text("/start", user=2)
        await chat.text("/start 999", user=2)
        assert "Семейный бюджет" in chat.session.calls[-1].text
        await close(chat)
        restarted = Database(db.path)
        await restarted.init()
        monkeypatch.setattr(app, "db", restarted)
        monkeypatch.setattr(app, "rate_limiter", RateLimiter())
        chat = ChatHarness()
        await chat.text("/start", user=2)
        assert await is_registered(restarted, 2)
        assert not await is_registered(restarted, 999)
        assert await restarted.transactions(2, "2025-09") == saved
        assert await restarted.summary(2, "2025-09") == summary
        assert await restarted.budget_report(2, "2025-09") == limits
        assert await restarted.selected_month(2) == "2025-09"
        assert await restarted.selected_month(shared) == "2025-08"
        assert (await restarted.summary(shared, "2025-08"))["balance"] == 789
        assert await family.active_budget_context(restarted, 2) == family_context
        with sqlite3.connect(db.path) as connection:
            assert connection.execute("SELECT * FROM access_users WHERE user_id=2").fetchone() == registration
        await close(chat)

    asyncio.run(scenario())


def test_new_users_and_allowlisted_owner_have_isolated_histories(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        for user in (2, 3):
            await chat.text("/start", user=user)
        categories = {1: "секретвладельца", 2: "секретвторого", 3: "секреттретьего"}
        for user, category in categories.items():
            await db.select_month(user, "2025-09")
            await chat.text(f"{category} {user * 111}", user=user)
            await chat.tap(chat.button("Сохранить"), user=user)
        assert not await is_registered(db, 1)  # Existing allowlist still grants access.
        for user, category in categories.items():
            since = len(chat.session.calls)
            await chat.text("📝 История", user=user)
            await chat.tap(chat.button(category), user=user)
            output = response_text(chat, since)
            assert category in output
            for other in categories.keys() - {user}:
                assert categories[other] not in output
            assert [row["category"] for row in await db.transactions(user, "2025-09")] == [category]
            assert await family.household_info(db, user) is None
        await close(chat)

    asyncio.run(scenario())


def test_forged_and_copied_callbacks_cannot_read_or_change_other_budget(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text("/start", user=2)
        await chat.text("/start", user=3)
        owner_id = await db.add_transaction(1, "expense", "секретвладельца", 321, note="закрытые данные", occurred_on="2025-09-01")
        other_id = await db.add_transaction(2, "expense", "секретвторого", 654, occurred_on="2025-09-01")
        await chat.tap(f"tx:delete:{owner_id}", user=1)
        copied_delete = chat.button("Да, удалить")
        await chat.tap(f"tx:edit:{other_id}", user=2)
        copied_save = chat.button("Сохранить")
        originals = [await db.transaction(1, owner_id), await db.transaction(2, other_id)]
        since = len(chat.session.calls)
        for transaction_id in (owner_id, other_id):
            for action in ("view", "edit", "delete"):
                await chat.tap(f"tx:{action}:{transaction_id}", user=3)
                assert "Операция не найдена" in chat.session.calls[-1].text
        for data in (
            copied_delete, copied_save,
            f"scope:1:0:tx:view:{owner_id}",
            f"scope:2:0:tx:edit:{other_id}",
            "scope:1:0:opening", "scope:2:0:budget:2025-09",
            "scope:1:0:tips:2025-09",
        ):
            await chat.tap(data, user=3)
        output = response_text(chat, since)
        for secret in ("секретвладельца", "закрытые данные", "секретвторого"):
            assert secret not in output
        assert [await db.transaction(1, owner_id), await db.transaction(2, other_id)] == originals
        assert await db.transactions(3, "2025-09") == []
        assert await db.budget_report(3, "2025-09") == []
        await close(chat)

    asyncio.run(scenario())


def test_disabling_signup_restores_allowlist_without_deleting_registrations_or_money(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text("/start", user=2)
        await db.set_opening(2, 500)
        row_id = await db.add_transaction(2, "expense", "продукты", 50, occurred_on="2025-09-01")
        original = await db.transaction(2, row_id)
        monkeypatch.setattr(app, "public_signup", False)
        for text in ("/start", "продукты 20", "📝 История"):
            await chat.text(text, user=2)
            assert "нет доступа" in chat.session.calls[-1].text
        await chat.tap(f"tx:delete:{row_id}", user=2)
        assert "нет доступа" in chat.session.calls[-1].text
        assert await is_registered(db, 2)
        assert await db.transaction(2, row_id) == original
        assert (await db.summary(2, "2025-09"))["balance"] == 450
        await chat.text("/start", user=1)
        assert "Личный бюджет" in chat.session.calls[-1].text
        assert not await is_registered(db, 1)
        await chat.text("/start", user=3)
        assert "нет доступа" in chat.session.calls[-1].text
        assert not await is_registered(db, 3)

        monkeypatch.setattr(app, "public_signup", True)
        await chat.tap(f"tx:view:{row_id}", user=2)
        assert "продукты" in response_text(chat, len(chat.session.calls) - 2)
        await close(chat)

    asyncio.run(scenario())


@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
def test_group_messages_never_register_users_or_expose_budget(tmp_path, monkeypatch, chat_type):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await db.add_transaction(1, "expense", "секретвладельца", 111, occurred_on="2025-09-01")
        for user in (1, 2):
            for text in ("/start", "/start 1", "/privacy", "/whoami", "📝 История"):
                await chat.text(text, user=user, chat_type=chat_type)
                assert "личный чат" in chat.session.calls[-1].text
                assert not await is_registered(db, user)
        assert "секретвладельца" not in response_text(chat)
        assert await db.transactions(2, "2025-09") == []
        await close(chat)

    asyncio.run(scenario())
