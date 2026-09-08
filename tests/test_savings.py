import asyncio
import sqlite3
from datetime import date

import pytest

from app.db import Database
from app.savings import (
    MAX_MINOR, capacity_plan, create_goal, get_budget_plan, get_goal, get_reserve,
    goal_plan, init_savings, list_goals, reserve_plan, set_budget_plan,
    set_reserve, update_goal,
)


async def database(tmp_path):
    db = Database(tmp_path / "savings.db")
    await db.init()
    await init_savings(db)
    return db


def goal_values(**overrides):
    return dict(name="Поездка", target_minor=18000000, saved_minor=0,
                monthly_minor=1000000, due_month="2028-02", create_key="goal-key") | overrides


def budget_values(**overrides):
    values = dict(income_minor=10000000, expenses_minor=6000000, irregular_minor=500000,
                  other_savings_minor=200000, buffer_minor=300000,
                  confirmed_on="2026-09-08", expected_version=None)
    return values | overrides


def reserve_values(**overrides):
    values = dict(essential_minor=5000000, months=3, saved_minor=2000000,
                  monthly_minor=500000, updated_on="2026-09-08", expected_version=None)
    return values | overrides


def test_additive_legacy_migration_and_repeat_initialization(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE goals(id INTEGER PRIMARY KEY, user_id INTEGER, name TEXT, target REAL, saved REAL, due_date TEXT)")
        conn.executemany("INSERT INTO goals VALUES(?,?,?,?,?,?)", [
            (1, 1, "Старая цель", 100.005, 0.005, "2028-02-29"),
            (2, -7, "Без срока", 250, 300, None),
            (3, 1, "Старая дата", 500, 4, "2026-02-30"),
        ])

    async def scenario():
        db = Database(path)
        await init_savings(db)
        before = await list_goals(db, 1)
        assert before[0]["target_minor"] == 10001
        assert before[0]["saved_minor"] == 1
        assert before[0]["due_month"] == "2028-02"
        assert before[0]["due_date"] == "2028-02-29"
        assert before[1]["due_month"] is None
        assert before[1]["due_date"] == "2026-02-30"
        assert (await get_goal(db, -7, 2))["saved_minor"] == 30000
        assert (await get_goal(db, -7, 2))["due_month"] is None
        await asyncio.gather(init_savings(db), init_savings(db))
        assert await list_goals(db, 1) == before
        assert (await get_goal(db, -7, 2))["version"] == 1

    asyncio.run(scenario())
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0] == 3
        assert conn.execute("SELECT target,saved,due_date FROM goals WHERE id=1").fetchone() == (100.005, 0.005, "2028-02-29")


def test_legacy_insert_after_init_remains_readable_and_editable(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await db.add_goal(5, "Старый интерфейс", 10.005, 2.005, "2027-12-07")
        row = (await list_goals(db, 5))[0]
        assert (row["target_minor"], row["saved_minor"], row["due_month"]) == (1001, 201, "2027-12")
        assert await get_goal(db, 5, row["id"]) == row
        assert await update_goal(db, 5, row["id"], expected_version=1, saved_minor=701, due_month=None)
        edited = await get_goal(db, 5, row["id"])
        assert (edited["saved"], edited["saved_minor"], edited["due_month"], edited["due_date"]) == (7.01, 701, None, None)
        await init_savings(db)
        assert await get_goal(db, 5, row["id"]) == edited
    asyncio.run(scenario())


def test_create_idempotency_concurrency_and_budget_isolation(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        ids = await asyncio.gather(*(create_goal(db, 8, **goal_values()) for _ in range(8)))
        assert len(set(ids)) == 1
        family_id = await create_goal(db, -8, **goal_values())
        assert family_id != ids[0]
        assert len(await list_goals(db, 8)) == len(await list_goals(db, -8)) == 1
        assert await get_goal(db, -8, ids[0]) is None
        assert not await update_goal(db, -8, ids[0], expected_version=1, saved_minor=1)
        assert (await get_goal(db, 8, ids[0]))["saved_minor"] == 0
    asyncio.run(scenario())


def test_optimistic_goal_edit_and_legacy_fields(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        ident = await create_goal(db, 1, **goal_values())
        results = await asyncio.gather(*(
            update_goal(db, 1, ident, expected_version=1, saved_minor=value)
            for value in [101, 202, 303]
        ))
        assert sum(results) == 1
        row = await get_goal(db, 1, ident)
        assert row["version"] == 2 and row["saved_minor"] in [101, 202, 303]
        assert row["saved"] == row["saved_minor"] / 100
        assert await update_goal(db, 1, ident, expected_version=2, target_minor=MAX_MINOR, due_month="2028-02")
        row = await get_goal(db, 1, ident)
        assert row["target_minor"] == MAX_MINOR
        assert row["target"] == MAX_MINOR / 100
        assert row["due_date"] == "2028-02-29"
        assert not await update_goal(db, 1, ident, expected_version=2, target_minor=1)
    asyncio.run(scenario())


def test_cap_is_atomic_and_archiving_frees_a_slot(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        identifiers = []
        for number in range(49):
            identifiers.append(await create_goal(db, 1, **(goal_values() | {"create_key": f"key-{number}"})))
        results = await asyncio.gather(*(
            create_goal(db, 1, **(goal_values() | {"create_key": f"race-{number}"}))
            for number in range(3)
        ), return_exceptions=True)
        assert sum(isinstance(result, int) for result in results) == 1
        assert sum(isinstance(result, ValueError) for result in results) == 2
        # A replay succeeds even when all slots are occupied.
        assert await create_goal(db, 1, **(goal_values() | {"create_key": "key-0"})) == identifiers[0]
        assert await update_goal(db, 1, identifiers[0], expected_version=1, is_archived=True)
        assert len(await list_goals(db, 1)) == 49
        assert len(await list_goals(db, 1, include_archived=True)) == 50
        await create_goal(db, 1, **(goal_values() | {"create_key": "after-archive"}))
        with pytest.raises(ValueError, match="50"):
            await update_goal(db, 1, identifiers[0], expected_version=2, is_archived=False)
        assert (await get_goal(db, 1, identifiers[0]))["version"] == 2
        # Another budget has its own limit.
        await create_goal(db, 2, **goal_values())
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"name": ""}, {"name": "x" * 121}, {"name": "a\nb"},
    {"target_minor": 0}, {"target_minor": 1.5}, {"saved_minor": -1},
    {"monthly_minor": True}, {"monthly_minor": MAX_MINOR + 1},
    {"due_month": "2026-2"}, {"due_month": "2026-13"}, {"due_month": "0000-01"},
    {"due_month": "2026-09-30"}, {"due_month": 202609}, {"create_key": ""},
])
def test_goal_validation_rejects_invalid_inputs_before_writing(tmp_path, changes):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await create_goal(db, 1, **(goal_values() | changes))
        assert await list_goals(db, 1) == []
    asyncio.run(scenario())


def test_update_rejects_unknown_fields_and_status(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        ident = await create_goal(db, 1, **goal_values())
        for changes in ({"user_id": 2}, {"create_key": "replace"}, {"is_archived": 2}, {}):
            with pytest.raises(ValueError):
                await update_goal(db, 1, ident, expected_version=1, **changes)
        assert (await get_goal(db, 1, ident))["version"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("deadline,as_of,remaining,monthly,expected", [
    ("2028-02", date(2026, 9, 8), 18000000, 1000000, (18, 1000000, 18)),
    ("2026-09", date(2026, 9, 30), 1001, 500, (1, 1001, 3)),
    ("2026-11", date(2026, 9, 8), 1001, 500, (3, 334, 3)),
    ("2026-08", date(2026, 9, 1), 1001, 0, (0, None, None)),
    (None, date(2026, 9, 8), 1001, 0, (None, None, None)),
    (None, date(2026, 9, 8), 0, 0, (None, 0, 0)),
    ("2020-01", date(2026, 9, 8), 0, 0, (0, 0, 0)),
    ("2027-01", date(2026, 12, 31), MAX_MINOR, 1, (2, 50000000000, MAX_MINOR)),
])
def test_goal_planning_integer_ceiling_and_month_boundaries(deadline, as_of, remaining, monthly, expected):
    goal = {"target_minor": max(remaining, 1), "saved_minor": int(remaining == 0),
            "due_month": deadline, "monthly_minor": monthly}
    plan = goal_plan(goal, as_of)
    assert (plan["months_left"], plan["required_minor"], plan["chosen_months"]) == expected
    assert plan["remaining_minor"] == remaining


def test_oversaved_goal_is_complete_without_negative_contribution():
    plan = goal_plan(dict(target_minor=100, saved_minor=150, monthly_minor=100, due_month="2026-10"), date(2026, 9, 8))
    assert plan == dict(remaining_minor=0, months_left=2, required_minor=0, chosen_months=0, progress_percent=100.0)


def test_subkopeck_legacy_goal_remains_safe_to_display(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await db.add_goal(1, "Дробная старая цель", 0.004)
        row = (await list_goals(db, 1))[0]
        assert row["target_minor"] == 0
        assert goal_plan(row, date(2026, 9, 8))["progress_percent"] == 100.0
        await init_savings(db)
        assert (await list_goals(db, 1))[0] == row
    asyncio.run(scenario())


@pytest.mark.parametrize("getter,setter,values", [
    (get_budget_plan, set_budget_plan, budget_values),
    (get_reserve, set_reserve, reserve_values),
])
def test_plan_create_edit_concurrency_and_isolation(tmp_path, getter, setter, values):
    async def scenario():
        db = await database(tmp_path)
        assert await getter(db, 1) is None
        assert not await setter(db, 1, **values(expected_version=1))
        created = await asyncio.gather(*(setter(db, 1, **values()) for _ in range(4)))
        assert sum(created) == 1
        assert (await getter(db, 1))["version"] == 1
        assert not await setter(db, 1, **values())
        edited = await asyncio.gather(*(setter(db, 1, **values(expected_version=1)) for _ in range(4)))
        assert sum(edited) == 1
        assert (await getter(db, 1))["version"] == 2
        assert await getter(db, -1) is None
        assert await setter(db, -1, **values())
        assert (await getter(db, -1))["version"] == 1
        await init_savings(db)
        assert (await getter(db, 1))["version"] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("setter,values", [
    (set_budget_plan, budget_values(income_minor=-1)),
    (set_budget_plan, budget_values(expenses_minor=1.1)),
    (set_budget_plan, budget_values(other_savings_minor=MAX_MINOR + 1)),
    (set_budget_plan, budget_values(confirmed_on="2026-02-29")),
    (set_budget_plan, budget_values(expected_version=True)),
    (set_reserve, reserve_values(months=0)),
    (set_reserve, reserve_values(months=37)),
    (set_reserve, reserve_values(months=True)),
    (set_reserve, reserve_values(saved_minor=-1)),
    (set_reserve, reserve_values(updated_on="20260908")),
])
def test_budget_and_reserve_validation(tmp_path, setter, values):
    async def scenario():
        db = await database(tmp_path)
        with pytest.raises(ValueError):
            await setter(db, 1, **values)
        assert await get_budget_plan(db, 1) is None
        assert await get_reserve(db, 1) is None
    asyncio.run(scenario())


def test_reserve_plans_zero_expenses_and_exact_kopecks():
    row = dict(essential_minor=1001, months=3, saved_minor=2000, monthly_minor=500)
    plan = reserve_plan(row)
    assert plan["target_minor"] == 3003
    assert plan["remaining_minor"] == 1003
    assert plan["chosen_months"] == 3
    assert plan["covered_months"] == pytest.approx(2000 / 1001)
    assert reserve_plan(row | {"monthly_minor": 0})["chosen_months"] is None
    assert reserve_plan(row | {"saved_minor": 9999})["remaining_minor"] == 0
    assert reserve_plan(row | {"essential_minor": 0}) == dict(target_minor=0, remaining_minor=0, chosen_months=0, covered_months=None)


def test_capacity_counts_each_active_incomplete_allocation_once_and_preserves_deficit():
    today = date(2026, 9, 8)
    budget = budget_values()
    active = dict(target_minor=9000000, saved_minor=0, monthly_minor=1000000, due_month="2026-01", is_archived=0)
    goals = [active, active | {"is_archived": 1}, active | {"saved_minor": 9000000}, active | {"monthly_minor": 0}]
    reserve = reserve_values()
    assert capacity_plan(None, goals, reserve, today) is None
    assert capacity_plan(budget, goals, reserve, today) == dict(capacity_minor=3000000, committed_minor=1500000, unassigned_minor=1500000)
    assert capacity_plan(budget | {"income_minor": 0}, goals, reserve, today) == dict(capacity_minor=-7000000, committed_minor=1500000, unassigned_minor=-8500000)
    assert capacity_plan(budget, goals, reserve | {"saved_minor": 15000000}, today)["committed_minor"] == 1000000


def test_savings_changes_never_create_or_change_transactions(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await db.add_transaction(1, "expense", "Продукты", 123.45)
        with sqlite3.connect(db.path) as conn:
            before = conn.execute("SELECT * FROM transactions").fetchall()
        ident = await create_goal(db, 1, **goal_values())
        await set_budget_plan(db, 1, **budget_values())
        await set_reserve(db, 1, **reserve_values())
        await update_goal(db, 1, ident, expected_version=1, saved_minor=9000000)
        await update_goal(db, 1, ident, expected_version=2, is_archived=True)
        capacity_plan(await get_budget_plan(db, 1), await list_goals(db, 1), await get_reserve(db, 1), date(2026, 9, 8))
        with sqlite3.connect(db.path) as conn:
            after = conn.execute("SELECT * FROM transactions").fetchall()
        assert after == before
    asyncio.run(scenario())
