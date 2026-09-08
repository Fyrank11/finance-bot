"""Savings plans: integer kopecks, explicit assumptions, no money movements.

The caller must authorize ``budget_id`` (personal or family) before using this
module. Every stored object remains scoped to that budget. Planning a transfer
or editing a saved balance never creates a transaction.
"""
from __future__ import annotations

import calendar
import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from .db import Database


MAX_MINOR = 99_999_999_999
MAX_ACTIVE_GOALS = 50
GOAL_CHANGES = {"name", "target_minor", "saved_minor", "monthly_minor", "due_month", "is_archived"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS savings_budget_plan (
    budget_id INTEGER PRIMARY KEY,
    income_minor INTEGER NOT NULL CHECK(income_minor >= 0),
    expenses_minor INTEGER NOT NULL CHECK(expenses_minor >= 0),
    irregular_minor INTEGER NOT NULL CHECK(irregular_minor >= 0),
    other_savings_minor INTEGER NOT NULL CHECK(other_savings_minor >= 0),
    buffer_minor INTEGER NOT NULL CHECK(buffer_minor >= 0),
    confirmed_on TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS savings_reserve (
    budget_id INTEGER PRIMARY KEY,
    essential_minor INTEGER NOT NULL CHECK(essential_minor >= 0),
    months INTEGER NOT NULL CHECK(months BETWEEN 1 AND 36),
    saved_minor INTEGER NOT NULL CHECK(saved_minor >= 0),
    monthly_minor INTEGER NOT NULL CHECK(monthly_minor >= 0),
    updated_on TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
"""


def _budget_id(value: int) -> int:
    if type(value) is not int or value == 0:
        raise ValueError("Некорректный бюджет")
    return value


def _minor(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or not (1 if positive else 0) <= value <= MAX_MINOR:
        raise ValueError("Сумма должна быть задана целым числом копеек в допустимых пределах")
    return value


def _name(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("Название — одна строка, от 1 до 120 символов")
    value = " ".join(value.split())
    if not value or len(value) > 120:
        raise ValueError("Название — от 1 до 120 символов")
    return value


def _month(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("Срок должен быть месяцем в формате ГГГГ-ММ")
    year, month = map(int, value.split("-"))
    date(year, month, 1)
    return value


def _iso_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД")
    return date.fromisoformat(value).isoformat()


def _version(value: int | None, *, allow_none: bool = False) -> int | None:
    if value is None and allow_none:
        return None
    if type(value) is not int or value < 1:
        raise ValueError("Некорректная версия записи")
    return value


def _due_date(month: str | None) -> str | None:
    if month is None:
        return None
    year, number = map(int, month.split("-"))
    return date(year, number, calendar.monthrange(year, number)[1]).isoformat()


def _legacy_minor(value: float) -> int:
    # Only the legacy boundary uses decimal conversion. New values are integers.
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _legacy_month(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _iso_date(value)[:7]
    except (ValueError, TypeError):
        return None


def _goal(row: aiosqlite.Row | dict) -> dict:
    result = dict(row)
    for column, legacy in (("target_minor", "target"), ("saved_minor", "saved")):
        if result[column] is None:
            result[column] = _legacy_minor(result[legacy])
    if result["due_month"] is None:
        result["due_month"] = _legacy_month(result["due_date"])
    return result


async def init_savings(db: Database) -> None:
    """Additive, repeatable migration, including data written by the old app."""
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        await conn.executescript(SCHEMA)
        await conn.execute("BEGIN IMMEDIATE")
        try:
            columns = {row[1] for row in await (await conn.execute("PRAGMA table_info(goals)")).fetchall()}
            for name, definition in {
                "target_minor": "INTEGER", "saved_minor": "INTEGER",
                "monthly_minor": "INTEGER NOT NULL DEFAULT 0", "due_month": "TEXT",
                "version": "INTEGER NOT NULL DEFAULT 1", "is_archived": "INTEGER NOT NULL DEFAULT 0",
                "create_key": "TEXT",
            }.items():
                if name not in columns:
                    await conn.execute(f"ALTER TABLE goals ADD COLUMN {name} {definition}")
            rows = await (await conn.execute(
                "SELECT id,target,saved,target_minor,saved_minor,due_date,due_month FROM goals "
                "WHERE target_minor IS NULL OR saved_minor IS NULL OR (due_month IS NULL AND due_date IS NOT NULL)"
            )).fetchall()
            for row in rows:
                await conn.execute(
                    "UPDATE goals SET target_minor=?,saved_minor=?,due_month=? WHERE id=?",
                    (row[3] if row[3] is not None else _legacy_minor(row[1]),
                     row[4] if row[4] is not None else _legacy_minor(row[2]),
                     row[6] if row[6] is not None else _legacy_month(row[5]), row[0]),
                )
            await conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_savings_create_key "
                "ON goals(user_id,create_key) WHERE create_key IS NOT NULL"
            )
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_savings_goals_budget ON goals(user_id,is_archived)")
            await conn.commit()
        except BaseException:
            await conn.rollback()
            raise


async def list_goals(db: Database, budget_id: int, include_archived: bool = False) -> list[dict]:
    _budget_id(budget_id)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        where = "user_id=?" + ("" if include_archived else " AND is_archived=0")
        rows = await (await conn.execute(f"SELECT * FROM goals WHERE {where} ORDER BY id", (budget_id,))).fetchall()
        return [_goal(row) for row in rows]


async def get_goal(db: Database, budget_id: int, goal_id: int) -> dict | None:
    _budget_id(budget_id)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute("SELECT * FROM goals WHERE user_id=? AND id=?", (budget_id, goal_id))).fetchone()
        return _goal(row) if row else None


async def create_goal(db: Database, budget_id: int, *, name: str, target_minor: int,
                      saved_minor: int, monthly_minor: int, due_month: str | None,
                      create_key: str) -> int:
    _budget_id(budget_id)
    name, due_month = _name(name), _month(due_month)
    _minor(target_minor, positive=True)
    _minor(saved_minor)
    _minor(monthly_minor)
    if not isinstance(create_key, str) or not create_key.strip() or len(create_key) > 128:
        raise ValueError("Некорректный ключ создания цели")
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            existing = await (await conn.execute(
                "SELECT id FROM goals WHERE user_id=? AND create_key=?", (budget_id, create_key)
            )).fetchone()
            if existing:
                await conn.commit()
                return existing[0]
            count = await (await conn.execute("SELECT COUNT(*) FROM goals WHERE user_id=? AND is_archived=0", (budget_id,))).fetchone()
            if count[0] >= MAX_ACTIVE_GOALS:
                raise ValueError("Можно вести до 50 активных целей. Сначала отправьте одну из целей в архив")
            cursor = await conn.execute(
                "INSERT INTO goals(user_id,name,target,saved,due_date,target_minor,saved_minor,monthly_minor,due_month,create_key) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (budget_id, name, target_minor / 100, saved_minor / 100, _due_date(due_month),
                 target_minor, saved_minor, monthly_minor, due_month, create_key),
            )
            await conn.commit()
            return cursor.lastrowid
        except BaseException:
            await conn.rollback()
            raise


async def update_goal(db: Database, budget_id: int, goal_id: int, *, expected_version: int,
                      **changes) -> bool:
    _budget_id(budget_id)
    _version(expected_version)
    if not changes or not changes.keys() <= GOAL_CHANGES:
        raise ValueError("Не указаны допустимые изменения цели")
    values = dict(changes)
    if "name" in values:
        values["name"] = _name(values["name"])
    for field in ("target_minor", "saved_minor", "monthly_minor"):
        if field in values:
            _minor(values[field], positive=field == "target_minor")
    if "due_month" in values:
        values["due_month"] = _month(values["due_month"])
        values["due_date"] = _due_date(values["due_month"])
    if "is_archived" in values:
        if type(values["is_archived"]) not in (int, bool) or values["is_archived"] not in (0, 1):
            raise ValueError("Некорректный статус цели")
        values["is_archived"] = int(values["is_archived"])
    for field, legacy in (("target_minor", "target"), ("saved_minor", "saved")):
        if field in values:
            values[legacy] = values[field] / 100
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            row = await (await conn.execute(
                "SELECT is_archived FROM goals WHERE user_id=? AND id=? AND version=?",
                (budget_id, goal_id, expected_version),
            )).fetchone()
            if row is None:
                await conn.commit()
                return False
            if row[0] and values.get("is_archived") == 0:
                count = await (await conn.execute("SELECT COUNT(*) FROM goals WHERE user_id=? AND is_archived=0", (budget_id,))).fetchone()
                if count[0] >= MAX_ACTIVE_GOALS:
                    raise ValueError("Можно вести до 50 активных целей")
            cursor = await conn.execute(
                "UPDATE goals SET " + ",".join(f"{key}=?" for key in values) + ",version=version+1 "
                "WHERE user_id=? AND id=? AND version=?",
                (*values.values(), budget_id, goal_id, expected_version),
            )
            await conn.commit()
            return bool(cursor.rowcount)
        except BaseException:
            await conn.rollback()
            raise


def _ceil_div(amount: int, divisor: int) -> int:
    return (amount + divisor - 1) // divisor


def goal_plan(goal: dict, as_of: date) -> dict:
    """Contributions start this month; the deadline is its month's last day."""
    remaining = max(0, goal["target_minor"] - goal["saved_minor"])
    deadline = goal.get("due_month")
    months_left = None
    if deadline:
        year, month = map(int, _month(deadline).split("-"))
        months_left = max(0, (year - as_of.year) * 12 + month - as_of.month + 1)
    required = (0 if remaining == 0 else
                _ceil_div(remaining, months_left) if months_left else None)
    monthly = goal["monthly_minor"]
    chosen_months = 0 if remaining == 0 else _ceil_div(remaining, monthly) if monthly else None
    # A pre-migration REAL goal below half a kopeck can legitimately round to
    # zero. Preserve that legacy row and display it as complete.
    progress = (100.0 if goal["target_minor"] == 0 else
                min(100.0, max(0.0, round(goal["saved_minor"] * 100 / goal["target_minor"], 2))))
    return {"remaining_minor": remaining, "months_left": months_left, "required_minor": required,
            "chosen_months": chosen_months, "progress_percent": progress}


async def _get_plan(db: Database, table: str, budget_id: int) -> dict | None:
    _budget_id(budget_id)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(f"SELECT * FROM {table} WHERE budget_id=?", (budget_id,))).fetchone()
        return dict(row) if row else None


async def _set_plan(db: Database, table: str, budget_id: int, values: dict,
                    expected_version: int | None) -> bool:
    _budget_id(budget_id)
    _version(expected_version, allow_none=True)
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        if expected_version is None:
            cursor = await conn.execute(
                f"INSERT INTO {table}(budget_id,{','.join(values)}) VALUES({','.join('?' for _ in range(len(values) + 1))}) "
                "ON CONFLICT(budget_id) DO NOTHING", (budget_id, *values.values()),
            )
        else:
            cursor = await conn.execute(
                f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in values) + ",version=version+1 "
                "WHERE budget_id=? AND version=?", (*values.values(), budget_id, expected_version),
            )
        await conn.commit()
        return bool(cursor.rowcount)


async def get_budget_plan(db: Database, budget_id: int) -> dict | None:
    return await _get_plan(db, "savings_budget_plan", budget_id)


async def set_budget_plan(db: Database, budget_id: int, *, income_minor: int,
                          expenses_minor: int, irregular_minor: int, other_savings_minor: int,
                          buffer_minor: int, confirmed_on: str, expected_version: int | None) -> bool:
    values = {"income_minor": _minor(income_minor), "expenses_minor": _minor(expenses_minor),
              "irregular_minor": _minor(irregular_minor), "other_savings_minor": _minor(other_savings_minor),
              "buffer_minor": _minor(buffer_minor), "confirmed_on": _iso_date(confirmed_on)}
    return await _set_plan(db, "savings_budget_plan", budget_id, values, expected_version)


async def get_reserve(db: Database, budget_id: int) -> dict | None:
    return await _get_plan(db, "savings_reserve", budget_id)


async def set_reserve(db: Database, budget_id: int, *, essential_minor: int, months: int,
                      saved_minor: int, monthly_minor: int, updated_on: str,
                      expected_version: int | None) -> bool:
    if type(months) is not int or not 1 <= months <= 36:
        raise ValueError("Размер резерва — от 1 до 36 месяцев расходов")
    values = {"essential_minor": _minor(essential_minor), "months": months,
              "saved_minor": _minor(saved_minor), "monthly_minor": _minor(monthly_minor),
              "updated_on": _iso_date(updated_on)}
    return await _set_plan(db, "savings_reserve", budget_id, values, expected_version)


def reserve_plan(row: dict) -> dict:
    target = row["essential_minor"] * row["months"]
    remaining = max(0, target - row["saved_minor"])
    monthly = row["monthly_minor"]
    chosen_months = 0 if remaining == 0 else _ceil_div(remaining, monthly) if monthly else None
    covered = row["saved_minor"] / row["essential_minor"] if row["essential_minor"] else None
    return {"target_minor": target, "remaining_minor": remaining,
            "chosen_months": chosen_months, "covered_months": covered}


def capacity_plan(budget: dict | None, goals: list[dict], reserve: dict | None, as_of: date) -> dict | None:
    """A signed planning gap, not a spending limit or an account balance.

    Expenses already include debt payments; irregular and other savings are
    separate user-confirmed allocations. Ledger expenses are not subtracted
    again. Overdue incomplete goals retain their chosen monthly contribution.
    """
    if budget is None:
        return None
    capacity = budget["income_minor"] - sum(budget[key] for key in
        ("expenses_minor", "irregular_minor", "other_savings_minor", "buffer_minor"))
    committed = sum(goal["monthly_minor"] for goal in goals
                    if not goal.get("is_archived") and goal_plan(goal, as_of)["remaining_minor"] > 0)
    if reserve is not None and reserve_plan(reserve)["remaining_minor"] > 0:
        committed += reserve["monthly_minor"]
    return {"capacity_minor": capacity, "committed_minor": committed, "unassigned_minor": capacity - committed}
