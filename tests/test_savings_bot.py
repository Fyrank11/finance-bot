"""Exercise savings through the real dispatcher, scopes and confirmation FSM."""
import asyncio
from datetime import date
from pathlib import Path
import sqlite3

from aiogram.methods import SendPhoto

from app import bot as app, family, savings, savings_ui
from app.db import Database
from app.rate_limit import RateLimiter
from test_bot import ChatHarness


DAY = date(2026, 9, 8)


async def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / "savings-bot.db")
    await db.init()
    monkeypatch.setattr(app, "db", db, raising=False)
    monkeypatch.setattr(app, "allowed_user_ids", frozenset({1, 2, 3}))
    monkeypatch.setattr(app, "public_signup", False)
    monkeypatch.setattr(app, "rate_limiter", RateLimiter(limit=1000, expensive_limit=100))
    monkeypatch.setattr(app, "today", lambda timezone: DAY)
    monkeypatch.setattr(savings_ui, "today", lambda timezone: DAY)
    return db, ChatHarness()


def responses(chat, since=0):
    return "\n".join((getattr(call, "text", "") or getattr(call, "caption", "") or "")
                     for call in chat.session.calls[since:])


def ledger(db):
    with sqlite3.connect(db.path) as conn:
        return conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()


async def close(chat):
    await chat.bot.session.close()
    await chat.dispatcher.storage.close()


async def new_goal(chat, *, user=1, values=("Отпуск", "180000", "0", "18", "6000"), save=True):
    await chat.text("/savings", user=user)
    await chat.tap(chat.button("Мои цели"), user=user)
    await chat.tap(chat.button("Новая цель"), user=user)
    for value in values:
        await chat.text(value, user=user)
    token = chat.button("Сохранить")
    if save:
        await chat.tap(token, user=user)
    return token


async def open_goal(chat, name="Отпуск", *, user=1):
    await chat.text("🎯 Мои цели", user=user)
    await chat.tap(chat.button(name), user=user)


async def plan_wizard(chat, kind, values, *, user=1, save=True):
    await chat.text("/savings", user=user)
    await chat.tap(chat.button("Возможности бюджета" if kind == "budget" else "Резерв"), user=user)
    await chat.tap(chat.button("Задать / изменить план" if kind == "budget" else "Задать / изменить резерв"), user=user)
    for value in values:
        await chat.text(value, user=user)
    token = chat.button("Сохранить")
    if save:
        await chat.tap(token, user=user)
    return token


async def shared_budget(db):
    ident = await family.create_household(db, 1)
    code = await family.create_invite(db, 1)
    assert await family.join_household(db, 2, code) == ident
    return ident


def test_goal_mobile_flow_calculation_and_replayed_confirmation(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await db.add_transaction(1, "expense", "продукты", 12.34, occurred_on="2025-09-01")
        before = ledger(db)
        token = await new_goal(chat, save=False)
        assert await savings.list_goals(db, 1) == []
        output = responses(chat)
        assert "10 000 ₽" in output
        assert "18 взносов" in output
        assert "30 ежемесячных взносов" in output
        await chat.tap(token)
        await chat.tap(token)
        goals = await savings.list_goals(db, 1)
        assert len(goals) == 1
        assert {key: goals[0][key] for key in ("name", "target_minor", "saved_minor", "monthly_minor", "due_month")} == {
            "name": "Отпуск", "target_minor": 18000000, "saved_minor": 0,
            "monthly_minor": 600000, "due_month": "2028-02",
        }
        assert goals[0]["version"] == 1
        assert "уже закрыто" in responses(chat)
        assert ledger(db) == before
        await close(chat)
    asyncio.run(scenario())


def test_budget_five_fields_and_reserve_four_fields_balance_without_ledger_changes(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await new_goal(chat, values=("Отпуск", "180000", "0", "18", "10000"))
        await chat.text("/savings")
        await chat.tap(chat.button("Возможности бюджета"))
        await chat.tap(chat.button("Задать / изменить план"))
        for value in ("100000", "70000", "10000", "5000"):
            await chat.text(value)
            assert await savings.get_budget_plan(db, 1) is None
        await chat.text("2000")
        assert await savings.get_budget_plan(db, 1) is None
        await chat.tap(chat.button("Сохранить"))
        budget = await savings.get_budget_plan(db, 1)
        assert budget["confirmed_on"] == "2026-09-08"
        assert [budget[key] for key in ("income_minor", "expenses_minor", "irregular_minor", "other_savings_minor", "buffer_minor")] == [10000000, 7000000, 1000000, 500000, 200000]
        await chat.text("/savings")
        await chat.tap(chat.button("Резерв"))
        await chat.tap(chat.button("Задать / изменить резерв"))
        await chat.text("50000,01")
        await chat.text("0")  # Invalid period does not advance to saved amount.
        assert "от 1 до 36" in responses(chat)
        for value in ("3", "0", "5000"):
            await chat.text(value)
            assert await savings.get_reserve(db, 1) is None
        await chat.tap(chat.button("Сохранить"))
        reserve = await savings.get_reserve(db, 1)
        assert [reserve[key] for key in ("essential_minor", "months", "saved_minor", "monthly_minor")] == [5000001, 3, 0, 500000]
        assert "150 000,03 ₽" in responses(chat)
        await chat.text("/savings")
        assert "−2 000 ₽" in responses(chat)
        assert "План не сходится" in responses(chat)
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_goal_saved_balance_edit_and_archive_are_confirmed_replacements(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await new_goal(chat)
        goal = (await savings.list_goals(db, 1))[0]
        await chat.tap(chat.button("Обновить накопленное"))
        await chat.text("12500,25")
        assert (await savings.get_goal(db, 1, goal["id"]))["saved_minor"] == 0
        await chat.tap(chat.button("Сохранить"))
        await chat.tap(chat.button("Обновить накопленное"))
        await chat.text("15000")
        await chat.tap(chat.button("Сохранить"))
        assert (await savings.get_goal(db, 1, goal["id"]))["saved_minor"] == 1500000
        await chat.tap(chat.button("Изменить план"))
        for value in ("Новый отпуск", "200000", "15000", "12.2027", "11000"):
            await chat.text(value)
        await chat.tap(chat.button("Сохранить"))
        changed = await savings.get_goal(db, 1, goal["id"])
        assert (changed["name"], changed["target_minor"], changed["saved_minor"], changed["monthly_minor"], changed["due_month"]) == (
            "Новый отпуск", 20000000, 1500000, 1100000, "2027-12")
        assert changed["version"] == 4
        await chat.tap(chat.button("В архив"))
        assert len(await savings.list_goals(db, 1)) == 1
        token = chat.button("Сохранить")
        await chat.tap(token)
        await chat.tap(token)
        assert await savings.list_goals(db, 1) == []
        assert (await savings.get_goal(db, 1, goal["id"]))["version"] == 5
        assert (await db.summary(1))["goals"] == []
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_invalid_inputs_and_cancel_do_not_save_partial_or_old_drafts(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text("🎯 Цель")
        await chat.tap(chat.button("Новая цель"))
        await chat.text("x" * 121)
        assert "120 символов" in responses(chat)
        await chat.text("Учеба")
        for invalid in ("-1", "0", "abc", "1000000000"):
            await chat.text(invalid)
            assert await savings.list_goals(db, 1) == []
        await chat.text("180000")
        await chat.text("0")
        for invalid in ("0", "601", "08.2026", "13.2027"):
            await chat.text(invalid)
        await chat.text("18")
        await chat.text("0")
        token = chat.button("Сохранить")
        await chat.text("/cancel")
        await chat.tap(token)
        assert await savings.list_goals(db, 1) == []
        token = await new_goal(chat, save=False)
        await chat.text("/savings")
        await chat.tap(token)
        assert await savings.list_goals(db, 1) == []
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_legacy_buttons_open_new_section_and_legacy_goal_has_no_invented_deadline(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await db.add_goal(1, "Старая цель", 1234.56, 34.56)
        since = len(chat.session.calls)
        await chat.text("🧭 Распределение")
        assert "Накопления и цели" in responses(chat, since)
        await chat.text("🎯 Цель")
        await chat.tap(chat.button("Старая цель"))
        output = responses(chat, since)
        assert "Месяц цели: не задан" in output
        assert "1 200 ₽" in output
        assert "Задайте срок" in output
        assert "Ваш взнос: 0 ₽" in output
        assert "50/30/20" not in output
        await chat.text("/savings@my_finance_advisor_bot")
        assert "Накопления и цели" in responses(chat, len(chat.session.calls) - 1)
        await close(chat)
    asyncio.run(scenario())


def test_other_user_copied_and_forged_callbacks_never_expose_or_change_goal(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await new_goal(chat, values=("Секрет владельца", "180000", "0", "18", "6000"))
        ident = (await savings.list_goals(db, 1))[0]["id"]
        await chat.tap(chat.button("Обновить накопленное"))
        await chat.text("9999")
        copied_confirm = chat.button("Сохранить")
        await chat.text("/savings", user=3)
        since = len(chat.session.calls)
        for action in ("goal", "saved", "edit", "archive"):
            await chat.tap(f"scope:1:0:sav:{action}:{ident}", user=3)
            await chat.tap(f"scope:3:0:sav:{action}:{ident}", user=3)
        await chat.tap(copied_confirm, user=3)
        await chat.tap(copied_confirm.replace("scope:1:0:", "scope:3:0:"), user=3)
        assert "Секрет владельца" not in responses(chat, since)
        assert (await savings.get_goal(db, 1, ident))["saved_minor"] == 0
        assert await savings.list_goals(db, 3) == []
        # Copying a confirmation cannot consume its owner's separate FSM.
        await chat.tap(copied_confirm, user=1)
        assert (await savings.get_goal(db, 1, ident))["saved_minor"] == 999900
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_scope_switch_invalidates_personal_draft_and_old_family_buttons(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        personal_confirm = await new_goal(chat, save=False)
        shared = await family.create_household(db, 1)
        await chat.tap(personal_confirm)
        assert await savings.list_goals(db, 1) == await savings.list_goals(db, shared) == []
        await new_goal(chat, values=("Семейный отпуск", "500000", "0", "24", "20000"))
        family_saved_button = chat.button("Обновить накопленное")
        family_goal = (await savings.list_goals(db, shared))[0]
        await family.switch_budget(db, 1, shared=False)
        await chat.tap(family_saved_button)
        await chat.text("/savings")
        assert "личный бюджет" in responses(chat)
        assert await savings.list_goals(db, 1) == []
        await family.switch_budget(db, 1, shared=True)
        await chat.text("/savings")
        since = len(chat.session.calls)
        await chat.tap(family_saved_button)
        assert "другого бюджета" in responses(chat, since)
        assert await savings.get_goal(db, shared, family_goal["id"]) == family_goal
        await close(chat)
    asyncio.run(scenario())


def test_family_member_stale_edit_cannot_overwrite_newer_saved_amount(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        shared = await shared_budget(db)
        await new_goal(chat)
        ident = (await savings.list_goals(db, shared))[0]["id"]
        await chat.tap(chat.button("Обновить накопленное"), user=1)
        await chat.text("1000", user=1)
        first_confirm = chat.button("Сохранить")
        await open_goal(chat, user=2)
        await chat.tap(chat.button("Обновить накопленное"), user=2)
        await chat.text("2000", user=2)
        second_confirm = chat.button("Сохранить")
        await chat.tap(second_confirm, user=2)
        since = len(chat.session.calls)
        await chat.tap(first_confirm, user=1)
        assert "План уже изменен" in responses(chat, since)
        row = await savings.get_goal(db, shared, ident)
        assert row["saved_minor"] == 200000 and row["version"] == 2
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_removed_family_member_cannot_submit_pending_goal_confirmation(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        shared = await shared_budget(db)
        token = await new_goal(chat, user=2, save=False)
        await family.remove_member(db, 1, 2)
        await chat.tap(token, user=2)
        assert await savings.list_goals(db, shared) == []
        assert await savings.list_goals(db, 2) == []
        assert "доступ изменился" in responses(chat)
        await close(chat)
    asyncio.run(scenario())


def test_family_budget_and_reserve_confirmation_detect_parallel_creation(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        shared = await shared_budget(db)
        for kind, first, second, getter, value_field, expected in (
            ("budget", ("100000", "70000", "0", "0", "0"), ("200000", "70000", "0", "0", "0"), savings.get_budget_plan, "income_minor", 20000000),
            ("reserve", ("10000", "3", "0", "1000"), ("20000", "6", "0", "2000"), savings.get_reserve, "essential_minor", 2000000),
        ):
            owner_token = await plan_wizard(chat, kind, first, user=1, save=False)
            await plan_wizard(chat, kind, second, user=2)
            since = len(chat.session.calls)
            await chat.tap(owner_token, user=1)
            assert "План уже изменен" in responses(chat, since)
            row = await getter(db, shared)
            assert row[value_field] == expected and row["version"] == 1
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_malformed_callbacks_are_safe_and_habits_offer_no_financial_products(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text("/savings")
        for data in (
            "sav:new", "sav:confirm:fake", "scope:", "scope:1:nan:sav:new",
            "scope:1:0:sav:", "scope:1:0:sav:goals", "scope:1:0:sav:goal:abc",
            "scope:1:0:sav:saved:-1", "scope:1:0:sav:archive:0", "scope:1:0:sav:edit",
            "scope:1:0:sav:goal:999999999999999999999999999999",
            "scope:1:0:sav:confirm", "scope:1:0:sav:confirm:fake", "scope:1:0:sav:unknown",
        ):
            await chat.tap(data)
        assert await savings.list_goals(db, 1) == []
        await chat.text("/savings")
        await chat.tap(chat.button("Привычки накоплений"))
        text = responses(chat).lower()
        assert "размер зависит" in text and "неполученную премию" in text
        for forbidden in ("офз", "депозит", "ставка банка", "купите", "доходность 10%", "50/30/20"):
            assert forbidden not in text
        await chat.tap(chat.button("Постер"))
        photos = [call for call in chat.session.calls if isinstance(call, SendPhoto)]
        assert len(photos) == 1
        assert Path(photos[0].photo.path).is_file()
        assert "выбираете вы" in photos[0].caption
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())
