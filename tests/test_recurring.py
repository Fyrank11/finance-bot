"""Real SQLite tests of money, occurrence dates, retries and payment confirmation."""
import asyncio
import sqlite3
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, User

from app import recurring as rec
from app.db import Database


async def database(tmp_path):
    db = Database(tmp_path / "recurring.db")
    await db.init()
    await rec.init_recurring(db)
    return db


async def mortgage(db, budget_id=1, **overrides):
    fields = {"name": "Ипотека", "category": "ипотека", "amount_minor": 6_100_000,
              "day_of_month": 31, "start_month": "2025-01"}
    fields.update(overrides)
    return await rec.create_schedule(db, budget_id, **fields)


def test_month_end_dates_keep_original_day():
    assert rec.due_date("2025-01", 31) == date(2025, 1, 31)
    assert rec.due_date("2025-02", 31) == date(2025, 2, 28)
    assert rec.due_date("2025-03", 31) == date(2025, 3, 31)
    assert rec.due_date("2024-02", 31) == date(2024, 2, 29)
    assert rec.due_date("2025-04", 30) == date(2025, 4, 30)
    for invalid in (0, 32, -1, 1.5, True):
        with pytest.raises(ValueError):
            rec.due_date("2025-01", invalid)


def test_obligations_include_all_backlog_and_current_month_without_expenses(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        first = await mortgage(db, create_key="one-confirmation")
        assert await mortgage(db, create_key="one-confirmation") == first
        await mortgage(db, 2, amount_minor=999)
        await mortgage(db, start_month="2025-04")
        await rec.init_recurring(db)
        report = await rec.obligations(db, 1, date(2025, 3, 8))
        assert report["total_minor"] == 18_300_000
        assert report["overdue_minor"] == 12_200_000
        assert [item["due_on"] for item in report["items"]] == ["2025-01-31", "2025-02-28", "2025-03-31"]
        assert [item["status"] for item in report["items"]] == ["overdue", "overdue", "upcoming"]
        assert (await rec.obligations(db, 1, date(2025, 3, 31)))["items"][-1]["status"] == "today"
        assert (await db.summary(1, "2025-03"))["expense"] == 0
        assert (await rec.obligations(db, 99, date(2025, 3, 8)))["total_minor"] == 0
    asyncio.run(scenario())


def test_payment_is_idempotent_atomic_today_and_attributed_to_family_actor(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "today", lambda _tz: date(2025, 3, 8))

    async def scenario():
        db = await database(tmp_path)
        schedule_id = await mortgage(db, -100, amount_minor=101)
        results = await asyncio.gather(*[
            rec.mark_paid(db, -100, schedule_id, "2025-02-28", actor_user_id=77, actor_name="Гриша") for _ in range(4)
        ])
        assert sum(result["created"] for result in results) == 1
        assert len({result["transaction_id"] for result in results}) == 1
        assert not (await rec.mark_paid(db, -100, schedule_id, "20250228", actor_user_id=77))["created"]
        transactions = await db.transactions(-100, "2025-03")
        assert len(transactions) == 1
        row = transactions[0]
        assert row["amount_minor"] == 101
        assert row["occurred_on"] == "2025-03-08"
        assert row["category"] == "ипотека / аренда"
        if "actor_user_id" in row:
            assert row["actor_user_id"] == 77
        if "actor_name" in row:
            assert row["actor_name"] == "Гриша"
        assert await rec.linked_payment(db, -100, row["id"])
        assert not await rec.linked_payment(db, 77, row["id"])
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT actor_user_id FROM recurring_payments").fetchone() == (77,)
        report = await rec.obligations(db, -100, date(2025, 3, 8))
        assert report["total_minor"] == 202
        assert [item["due_on"] for item in report["items"]] == ["2025-01-31", "2025-03-31"]
        # Advance payment of this month's obligation is a real expense today.
        await rec.mark_paid(db, -100, schedule_id, "2025-03-31", actor_user_id=78)
        assert all(row["occurred_on"] == "2025-03-08" for row in await db.transactions(-100, "2025-03"))
    asyncio.run(scenario())


def test_unauthorized_or_invalid_occurrence_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "today", lambda _tz: date(2025, 3, 8))

    async def scenario():
        db = await database(tmp_path)
        schedule_id = await mortgage(db)
        for budget_id, due in ((2, "2025-02-28"), (1, "2025-02-27"), (1, "2024-12-31"), (1, "2025-04-30")):
            with pytest.raises(ValueError):
                await rec.mark_paid(db, budget_id, schedule_id, due)
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
            assert conn.execute("SELECT count(*) FROM recurring_payments").fetchone() == (0,)
    asyncio.run(scenario())


def test_failed_marker_insert_rolls_back_expense(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "today", lambda _tz: date(2025, 3, 8))

    async def scenario():
        db = await database(tmp_path)
        schedule_id = await mortgage(db)
        with sqlite3.connect(db.path) as conn:
            conn.execute("CREATE TRIGGER reject_payment BEFORE INSERT ON recurring_payments BEGIN SELECT RAISE(ABORT, 'simulated failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match="simulated failure"):
            await rec.mark_paid(db, 1, schedule_id, "2025-02-28")
        assert not await db.transactions(1, "2025-03")
        with sqlite3.connect(db.path) as conn:
            assert conn.execute("SELECT count(*) FROM recurring_payments").fetchone() == (0,)
    asyncio.run(scenario())


def test_disable_preserves_due_and_paid_history_and_blocks_future(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "today", lambda _tz: date(2025, 3, 8))

    async def scenario():
        db = await database(tmp_path)
        schedule_id = await mortgage(db)
        await rec.mark_paid(db, 1, schedule_id, "2025-01-31")
        assert not await rec.disable_schedule(db, 2, schedule_id)
        assert await rec.disable_schedule(db, 1, schedule_id)
        assert not await rec.disable_schedule(db, 1, schedule_id)
        report = await rec.obligations(db, 1, date(2025, 7, 20))
        assert [item["due_on"] for item in report["items"]] == ["2025-02-28"]
        with pytest.raises(ValueError):
            await rec.mark_paid(db, 1, schedule_id, "2025-03-31")
        await rec.mark_paid(db, 1, schedule_id, "2025-02-28")
        assert (await rec.obligations(db, 1, date(2025, 7, 20)))["total_minor"] == 0
        assert len(await db.transactions(1, "2025-03")) == 2
    asyncio.run(scenario())


def test_deleted_expense_reopens_obligation_and_can_be_paid_again(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "today", lambda _tz: date(2025, 3, 8))

    async def scenario():
        db = await database(tmp_path)
        schedule_id = await mortgage(db, start_month="2025-03")
        paid = await rec.mark_paid(db, 1, schedule_id, "2025-03-31")
        assert (await rec.obligations(db, 1, date(2025, 3, 8)))["total_minor"] == 0
        await db.delete_transaction(1, paid["transaction_id"])
        assert not await rec.linked_payment(db, 1, paid["transaction_id"])
        assert (await rec.obligations(db, 1, date(2025, 3, 8)))["total_minor"] == 6_100_000
        replacement = await rec.mark_paid(db, 1, schedule_id, "2025-03-31")
        assert replacement["created"]
        assert len(await db.transactions(1, "2025-03")) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("override", [
    {"amount_minor": 0}, {"amount_minor": -100}, {"amount_minor": 1.1}, {"amount_minor": 100_000_000_000},
    {"name": "\nИпотека"}, {"name": " "}, {"name": "x" * 81}, {"day_of_month": 32}, {"start_month": "2025-13"},
])
def test_invalid_schedule_does_not_get_saved(tmp_path, override):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await mortgage(db, **override)
        assert not await rec.list_schedules(db, 1)
    asyncio.run(scenario())


def test_buttons_require_confirmation_and_do_not_take_over_other_fsm(tmp_path, monkeypatch):
    monkeypatch.setattr(rec, "today", lambda _tz: date(2025, 3, 8))

    async def scenario():
        db = await database(tmp_path)
        schedule_id = await mortgage(db, start_month="2025-03")
        storage = MemoryStorage()
        state = FSMContext(storage=storage, key=StorageKey(bot_id=100, chat_id=1, user_id=1))
        message = Message(message_id=1, date=datetime.now(timezone.utc), chat=Chat(id=1, type="private"), text="500")

        def callback(data):
            return CallbackQuery(id="test", from_user=User(id=1, is_bot=False, first_name="Test"),
                                 chat_instance="test", message=message, data=data)

        with patch.object(Message, "answer", new=AsyncMock()) as answers, patch.object(CallbackQuery, "answer", new=AsyncMock()):
            await state.set_state("Form:amount")
            assert not await rec.handle_message(message, state, db, 1)
            assert await rec.handle_callback(callback("rec:add:1:0"), state, db, 1)
            assert await state.get_state() == "Form:amount"
            await state.clear()
            assert await rec.handle_callback(callback(f"rec:pay:{schedule_id}:2025-03-31"), state, db, 1)
            assert not await db.transactions(1, "2025-03")
            data = await state.get_data()
            confirm = f"rec:confirm:{data['rec_draft']['token']}"
            # Switching budget cannot confirm the old budget's payment.
            await rec.handle_callback(callback(confirm), state, db, 2)
            assert not await db.transactions(1, "2025-03")
            await rec.handle_callback(callback(confirm), state, db, 1)
            await rec.handle_callback(callback(confirm), state, db, 1)
            assert len(await db.transactions(1, "2025-03")) == 1
            await rec.handle_callback(callback(f"rec:disable:{schedule_id}"), state, db, 1)
            assert (await rec.list_schedules(db, 1))[0]["disabled_on"] is None
            data = await state.get_data()
            await rec.handle_callback(callback(f"rec:confirm:{data['rec_draft']['token']}"), state, db, 1)
            assert (await rec.list_schedules(db, 1))[0]["disabled_on"] == "2025-03-08"
            assert answers.await_count > 0
        await storage.close()
    asyncio.run(scenario())
