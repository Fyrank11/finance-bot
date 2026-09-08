"""Cashflow money/date boundaries and real SQLite isolation, without journal writes."""
import asyncio
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from app import recurring
from app.cashflow import (
    MAX_MINOR, add_event, close_event, forecast, get_event, get_profile,
    init_cashflow, ledger_fingerprint, list_events, set_profile,
)
from app.db import Database
from app.savings import create_goal, set_reserve


TODAY = date(2026, 9, 8)


async def database(tmp_path):
    db = Database(tmp_path / "cashflow.db")
    await db.init()
    await init_cashflow(db)
    return db


def profile_values(**overrides):
    return dict(balance_minor=100_000, daily_minor=1000, buffer_minor=5000,
                confirmed_on=TODAY.isoformat(), expected_version=None) | overrides


def event_values(**overrides):
    return dict(kind="income", name="Зарплата", amount_minor=300_000,
                due_on="2026-09-10", create_key="first-income") | overrides


def journal_rows(db):
    with sqlite3.connect(db.path) as conn:
        return conn.execute("SELECT * FROM transactions ORDER BY id").fetchall()


def test_additive_migration_and_no_actual_money_movements(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await db.add_transaction(1, "expense", "Продукты", 12.34, occurred_on="2026-09-01")
        before = journal_rows(db)
        assert await set_profile(db, 1, **profile_values())
        ident = await add_event(db, 1, **event_values())
        await add_event(db, 1, **event_values(kind="saving", create_key="saving"))
        report = await forecast(db, 1, TODAY)
        assert not report["stale"]
        assert await close_event(db, 1, ident, expected_version=1)
        profile = await get_profile(db, 1)
        events = await list_events(db, 1, include_closed=True)
        await asyncio.gather(init_cashflow(db), init_cashflow(db))
        assert await get_profile(db, 1) == profile
        assert await list_events(db, 1, include_closed=True) == events
        assert journal_rows(db) == before
    asyncio.run(scenario())


def test_profile_concurrent_create_edit_and_family_isolation(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        assert await get_profile(db, 1) is None
        assert not await set_profile(db, 1, **profile_values(expected_version=1))
        outcomes = await asyncio.gather(*(set_profile(db, 1, **profile_values()) for _ in range(4)))
        assert sum(outcomes) == 1
        assert (await get_profile(db, 1))["version"] == 1
        outcomes = await asyncio.gather(*(set_profile(db, 1, **profile_values(expected_version=1)) for _ in range(4)))
        assert sum(outcomes) == 1
        assert (await get_profile(db, 1))["version"] == 2
        assert await get_profile(db, -1) is None
        assert await set_profile(db, -1, **profile_values(balance_minor=-100))
        assert (await get_profile(db, -1))["balance_minor"] == -100
        assert (await get_profile(db, 1))["balance_minor"] == 100_000
    asyncio.run(scenario())


@pytest.mark.parametrize("budget_id,overrides", [
    (0, {}), (True, {}), (2**63, {}), (-2**63 - 1, {}),
    (1, {"balance_minor": MAX_MINOR + 1}), (1, {"balance_minor": -MAX_MINOR - 1}),
    (1, {"daily_minor": -1}), (1, {"buffer_minor": 1.1}), (1, {"balance_minor": True}),
    (1, {"confirmed_on": "2026-02-29"}), (1, {"confirmed_on": "20260908"}),
    (1, {"confirmed_on": None}), (1, {"expected_version": True}),
    (1, {"expected_version": 2**63}), (1, {"expected_ledger_fingerprint": "bad"}),
])
def test_profile_validation_rejects_before_mutation(tmp_path, budget_id, overrides):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await set_profile(db, budget_id, **profile_values(**overrides))
        assert await get_profile(db, 1) is None
    asyncio.run(scenario())


def test_expected_journal_snapshot_rejects_changes_during_confirmation(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        captured = await ledger_fingerprint(db, -12)
        await db.add_transaction(-12, "expense", "Продукты", 10, occurred_on="2026-09-01")
        assert not await set_profile(db, -12, **profile_values(expected_ledger_fingerprint=captured))
        assert await get_profile(db, -12) is None
        latest = await ledger_fingerprint(db, -12)
        assert await set_profile(db, -12, **profile_values(expected_ledger_fingerprint=latest))
        await db.add_transaction(-12, "income", "Зарплата", 10, occurred_on="2026-09-01")
        assert not await set_profile(db, -12, **profile_values(expected_version=1, expected_ledger_fingerprint=latest))
        assert (await get_profile(db, -12))["version"] == 1
    asyncio.run(scenario())


def test_event_idempotency_close_scope_and_concurrent_confirmation(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        ids = await asyncio.gather(*(add_event(db, -7, **event_values()) for _ in range(5)))
        assert len(set(ids)) == 1
        ident = ids[0]
        assert len(await list_events(db, -7)) == 1
        assert await get_event(db, 7, ident) is None
        assert not await close_event(db, 7, ident, expected_version=1)
        assert not await close_event(db, -7, ident, expected_version=2)
        outcomes = await asyncio.gather(*(close_event(db, -7, ident, expected_version=1) for _ in range(5)))
        assert sum(outcomes) == 1
        assert await list_events(db, -7) == []
        assert (await get_event(db, -7, ident))["version"] == 2
        assert not await close_event(db, -7, ident, expected_version=2)
        assert await add_event(db, -7, **event_values()) == ident
        assert await list_events(db, -7) == []
        assert await add_event(db, 7, **event_values()) != ident
    asyncio.run(scenario())


def test_open_event_limit_is_atomic_and_closed_events_free_space(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        for index in range(49):
            await add_event(db, 1, **event_values(create_key=str(index)))
        outcomes = await asyncio.gather(*(
            add_event(db, 1, **event_values(create_key=str(index))) for index in range(49, 53)
        ), return_exceptions=True)
        assert sum(isinstance(value, int) for value in outcomes) == 1
        assert all(isinstance(value, (int, ValueError)) for value in outcomes)
        assert len(await list_events(db, 1)) == 50
        ident = (await list_events(db, 1))[0]["id"]
        assert await close_event(db, 1, ident, expected_version=1)
        await add_event(db, 1, **event_values(create_key="another"))
        assert len(await list_events(db, 1)) == 50
        assert len(await list_events(db, 1, include_closed=True)) == 51
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"kind": "investment"}, {"kind": []}, {"name": ""}, {"name": "x\ny"}, {"name": "x" * 81},
    {"amount_minor": 0}, {"amount_minor": -1}, {"amount_minor": True}, {"amount_minor": MAX_MINOR + 1},
    {"due_on": "2027-02-29"}, {"due_on": None}, {"create_key": ""}, {"create_key": "x\ny"},
])
def test_invalid_event_creates_nothing(tmp_path, overrides):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await add_event(db, 1, **event_values(**overrides))
        assert await list_events(db, 1) == []
    asyncio.run(scenario())


@pytest.mark.parametrize("ident,version", [(0, 1), (True, 1), (2**63, 1), (1, None), (1, True), (1, 2**63)])
def test_close_invalid_identifier_or_version(tmp_path, ident, version):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await close_event(db, 1, ident, expected_version=version)
    asyncio.run(scenario())


def test_missing_or_old_profile_suppresses_calculated_amounts(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await add_event(db, 1, **event_values())
        report = await forecast(db, 1, TODAY)
        assert report["stale_reason"] == "missing_profile"
        assert report["next_income_on"] == "2026-09-10"
        assert report["events"]
        for key in ("available_after_buffer_minor", "lowest_minor", "first_shortfall_on", "totals"):
            assert report[key] is None
        assert report["rows"] == []
        await set_profile(db, 1, **profile_values())
        report = await forecast(db, 1, TODAY + timedelta(days=1))
        assert report["stale_reason"] == "date_changed"
        assert report["rows"] == []
        assert report["available_after_buffer_minor"] is None
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["add", "delete", "edit", "same_total_edit"])
def test_any_scoped_journal_change_requires_balance_reconfirmation(tmp_path, change):
    async def scenario():
        db = await database(tmp_path)
        ident = await db.add_transaction(1, "expense", "Продукты", 10, occurred_on="2026-09-01")
        await set_profile(db, 1, **profile_values())
        await db.add_transaction(2, "expense", "Продукты", 10, occurred_on="2026-09-01")
        assert not (await forecast(db, 1, TODAY))["stale"]
        if change == "add":
            await db.add_transaction(1, "income", "Зарплата", 10, occurred_on="2026-09-01")
        elif change == "delete":
            await db.delete_transaction(1, ident)
        else:
            await db.edit_transaction(1, ident, kind="expense", category="Продукты",
                                      amount=10 if change == "same_total_edit" else 20,
                                      occurred_on="2026-09-02")
        stale = await forecast(db, 1, TODAY)
        assert stale["stale_reason"] == "ledger_changed"
        assert stale["rows"] == [] and stale["totals"] is None
        assert await set_profile(db, 1, **profile_values(expected_version=1, balance_minor=90_000))
        fresh = await forecast(db, 1, TODAY)
        assert not fresh["stale"]
        assert fresh["available_after_buffer_minor"] == 85_000
    asyncio.run(scenario())


def test_expected_income_never_hides_a_same_day_cash_gap(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values(balance_minor=1000, buffer_minor=100, daily_minor=200))
        await add_event(db, 1, **event_values(due_on=TODAY.isoformat(), amount_minor=10_000))
        await add_event(db, 1, **event_values(kind="expense", due_on=TODAY.isoformat(), amount_minor=600, create_key="bill"))
        await add_event(db, 1, **event_values(kind="saving", due_on=TODAY.isoformat(), amount_minor=200, create_key="save"))
        report = await forecast(db, 1, TODAY)
        assert report["available_after_buffer_minor"] == 900
        assert report["first_shortfall_on"] == TODAY.isoformat()
        assert report["lowest_minor"] == -100
        assert report["rows"] == [dict(date=TODAY.isoformat(), before_income_minor=-100,
            income_minor=10_000, expense_minor=600, saving_minor=200, daily_minor=200,
            recurring_minor=0, end_minor=9900)]
        assert report["totals"]["outflow_minor"] == 1000
        assert report["totals"]["end_minor"] == 9900
        assert journal_rows(db) == []
    asyncio.run(scenario())


@pytest.mark.parametrize("as_of,end", [
    (date(2028, 2, 28), "2028-03-28"),
    (date(2027, 2, 28), "2027-03-29"),
    (date(2026, 12, 31), "2027-01-29"),
])
def test_default_thirty_days_cross_month_leap_and_year_without_double_buffer(tmp_path, as_of, end):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values(confirmed_on=as_of.isoformat()))
        report = await forecast(db, 1, as_of)
        assert report["horizon_end"] == end
        assert len(report["rows"]) == 30
        assert report["totals"]["end_minor"] == 100_000 - 5000 - 30 * 1000
        assert report["first_shortfall_on"] is None
    asyncio.run(scenario())


def test_nearest_income_bounds_horizon_and_far_income_is_explicitly_excluded(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values())
        far_day = (TODAY + timedelta(days=90)).isoformat()
        far = await add_event(db, 1, **event_values(due_on=far_day))
        report = await forecast(db, 1, TODAY)
        assert len(report["rows"]) == 30
        assert report["next_income_on"] is None
        assert report["outside_horizon_events"][0]["id"] == far
        assert report["totals"]["income_minor"] == 0
        limit_day = (TODAY + timedelta(days=89)).isoformat()
        await add_event(db, 1, **event_values(due_on=limit_day, create_key="limit"))
        report = await forecast(db, 1, TODAY)
        assert len(report["rows"]) == 90
        assert report["horizon_end"] == limit_day
        await add_event(db, 1, **event_values(due_on="2026-09-10", create_key="nearer"))
        report = await forecast(db, 1, TODAY)
        assert len(report["rows"]) == 3
        assert report["next_income_on"] == "2026-09-10"
        assert len(report["outside_horizon_events"]) == 2
    asyncio.run(scenario())


def test_past_income_alone_is_not_assumed_received_or_used_for_forecast(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values(daily_minor=0, buffer_minor=0))
        await add_event(db, 1, **event_values(due_on="2026-09-07"))
        report = await forecast(db, 1, TODAY)
        assert not report["stale"]
        assert len(report["overdue_events"]) == 1
        assert report["next_income_on"] is None
        assert report["totals"]["income_minor"] == 0
        assert report["totals"]["outflow_minor"] == 0
        assert report["totals"]["end_minor"] == 100_000
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["expense", "saving"])
def test_past_outflows_block_until_closed_or_recreated_at_confirmed_date(tmp_path, kind):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values(balance_minor=10_000, daily_minor=0, buffer_minor=0))
        await add_event(db, 1, **event_values(due_on="2026-09-07"))
        ident = await add_event(db, 1, **event_values(kind=kind, amount_minor=15_000,
                                                   due_on="2026-09-07", create_key="outflow"))
        report = await forecast(db, 1, TODAY)
        assert report["stale_reason"] == "overdue_outflows"
        assert report["rows"] == []
        for key in ("available_after_buffer_minor", "lowest_minor", "first_shortfall_on", "totals"):
            assert report[key] is None
        # Reconfirming cash alone does not resolve whether the old expectation
        # is already accounted for or still needs to happen.
        assert await set_profile(db, 1, **profile_values(balance_minor=10_000, daily_minor=0,
                                                        buffer_minor=0, expected_version=1))
        assert (await forecast(db, 1, TODAY))["stale_reason"] == "overdue_outflows"
        assert await close_event(db, 1, ident, expected_version=1)
        resolved = await forecast(db, 1, TODAY)
        assert not resolved["stale"]
        assert resolved["totals"]["end_minor"] == 10_000
        # If the outflow is still expected, recording its new date includes it
        # once and reveals the actual projected gap without posting an expense.
        await add_event(db, 1, **event_values(kind=kind, amount_minor=15_000,
                                            due_on=TODAY.isoformat(), create_key="rescheduled"))
        rescheduled = await forecast(db, 1, TODAY)
        assert not rescheduled["stale"]
        assert rescheduled["first_shortfall_on"] == TODAY.isoformat()
        assert rescheduled["totals"]["end_minor"] == -5000
        assert journal_rows(db) == []
    asyncio.run(scenario())


def test_invalid_event_date_blocks_numbers_until_the_event_is_reconciled(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values())
        ident = await add_event(db, 1, **event_values(create_key="bad-restore"))
        with sqlite3.connect(db.path) as conn:
            conn.execute("UPDATE cashflow_events SET due_on='bad-date' WHERE id=?", (ident,))
        report = await forecast(db, 1, TODAY)
        assert report["stale_reason"] == "invalid_events"
        assert report["invalid_events"][0]["id"] == ident
        assert report["rows"] == []
        assert report["available_after_buffer_minor"] is None
        assert report["totals"] is None
        assert await close_event(db, 1, ident, expected_version=1)
        assert not (await forecast(db, 1, TODAY))["stale"]
    asyncio.run(scenario())


def test_recurring_arrears_paid_links_disabled_and_horizon_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(recurring, "today", lambda _: TODAY)

    async def scenario():
        db = await database(tmp_path)
        mortgage = await recurring.create_schedule(db, -7, name="Ипотека", category="ипотека",
            amount_minor=101, day_of_month=10, start_month="2026-07", create_key="mortgage")
        disabled = await recurring.create_schedule(db, -7, name="Старая подписка", category="сервисы",
            amount_minor=202, day_of_month=9, start_month="2026-08", create_key="disabled")
        await recurring.create_schedule(db, -7, name="Позже", category="сервисы",
            amount_minor=999, day_of_month=30, start_month="2026-09", create_key="later")
        await recurring.create_schedule(db, 7, name="Чужой платеж", category="сервисы",
            amount_minor=9999, day_of_month=9, start_month="2026-09", create_key="foreign")
        await recurring.mark_paid(db, -7, mortgage, "2026-08-10", actor_user_id=7)
        await recurring.disable_schedule(db, -7, disabled)
        await set_profile(db, -7, **profile_values(daily_minor=0, buffer_minor=0))
        await add_event(db, -7, **event_values(due_on="2026-09-12"))
        before = journal_rows(db)
        report = await forecast(db, -7, TODAY)
        assert [(item["name"], item["due_on"]) for item in report["recurring_items"]] == [
            ("Ипотека", "2026-07-10"), ("Старая подписка", "2026-08-09"), ("Ипотека", "2026-09-10")]
        assert report["rows"][0]["recurring_minor"] == 303
        assert report["rows"][2]["recurring_minor"] == 101
        assert [item["status"] for item in report["recurring_items"]] == ["overdue", "overdue", "upcoming"]
        assert report["totals"]["expense_minor"] == 404
        assert report["totals"]["recurring_minor"] == 404
        assert journal_rows(db) == before
    asyncio.run(scenario())


def test_linked_bill_payment_during_forecast_suppresses_stale_amounts(tmp_path, monkeypatch):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values())

        async def changed_obligations(db, budget_id, as_of):
            await db.add_transaction(budget_id, "expense", "Продукты", 10, occurred_on="2026-09-01")
            return {"items": []}

        monkeypatch.setattr(recurring, "obligations", changed_obligations)
        report = await forecast(db, 1, TODAY)
        assert report["stale_reason"] == "ledger_changed"
        assert report["rows"] == []
        assert report["available_after_buffer_minor"] is None
        assert report["totals"] is None
    asyncio.run(scenario())


def test_monthly_savings_allocations_are_not_automatically_subtracted(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await set_profile(db, 1, **profile_values(daily_minor=0, buffer_minor=0))
        await create_goal(db, 1, name="Цель", target_minor=100_000, saved_minor=0,
                          monthly_minor=10_000, due_month="2027-01", create_key="goal")
        await set_reserve(db, 1, essential_minor=10_000, months=3, saved_minor=0,
                          monthly_minor=5000, updated_on=TODAY.isoformat(), expected_version=None)
        report = await forecast(db, 1, TODAY)
        assert report["totals"]["saving_minor"] == 0
        assert report["totals"]["end_minor"] == 100_000
        await add_event(db, 1, **event_values(kind="saving", amount_minor=15_000, create_key="transfer"))
        report = await forecast(db, 1, TODAY)
        assert report["totals"]["saving_minor"] == 15_000
        assert report["totals"]["end_minor"] == 85_000
    asyncio.run(scenario())


@pytest.mark.parametrize("as_of", [None, "2026-09-08", datetime(2026, 9, 8), date(1999, 12, 31), date(9998, 12, 30), date.max])
def test_invalid_forecast_date_fails_cleanly(tmp_path, as_of):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await forecast(db, 1, as_of)
    asyncio.run(scenario())
