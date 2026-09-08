"""A dated cash plan, separate from the journal and from savings balances.

The caller authorizes the personal or family budget before calling this module.
Expected events are never posted as actual transactions. A confirmed cash balance
is usable only on the same day and while the journal remains unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import TYPE_CHECKING

import aiosqlite

from .savings import MAX_MINOR

if TYPE_CHECKING:
    from .db import Database


MAX_OPEN_EVENTS = 50
MAX_HORIZON_DAYS = 90
DEFAULT_HORIZON_DAYS = 30
MAX_SQLITE_INTEGER = 2**63 - 1
EVENT_KINDS = {"income", "expense", "saving"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS cashflow_profiles (
    budget_id INTEGER PRIMARY KEY,
    balance_minor INTEGER NOT NULL,
    daily_minor INTEGER NOT NULL CHECK(daily_minor >= 0),
    buffer_minor INTEGER NOT NULL CHECK(buffer_minor >= 0),
    confirmed_on TEXT NOT NULL,
    ledger_fingerprint TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS cashflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('income','expense','saving')),
    name TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    due_on TEXT NOT NULL,
    is_closed INTEGER NOT NULL DEFAULT 0 CHECK(is_closed IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1,
    create_key TEXT NOT NULL,
    UNIQUE(budget_id,create_key)
);
CREATE INDEX IF NOT EXISTS idx_cashflow_events_budget
ON cashflow_events(budget_id,is_closed,due_on,id);
"""


def _budget_id(value: int) -> int:
    if type(value) is not int or value == 0 or not -2**63 <= value <= MAX_SQLITE_INTEGER:
        raise ValueError("Некорректный бюджет")
    return value


def _identifier(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_SQLITE_INTEGER:
        raise ValueError("Некорректный номер события")
    return value


def _version(value: int | None) -> int | None:
    if value is not None and (type(value) is not int or not 1 <= value < MAX_SQLITE_INTEGER):
        raise ValueError("Некорректная версия записи")
    return value


def _minor(value: int, *, positive: bool = False, signed: bool = False) -> int:
    minimum = -MAX_MINOR if signed else 1 if positive else 0
    if type(value) is not int or not minimum <= value <= MAX_MINOR:
        raise ValueError("Сумма должна быть целым числом копеек в допустимых пределах")
    return value


def _iso_date(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Дата должна быть в формате ГГГГ-ММ-ДД")
    parsed = date.fromisoformat(value)
    if parsed.year < 2000:
        raise ValueError("Дата должна быть не раньше 2000 года")
    return parsed.isoformat()


def _name(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("Название — одна строка, до 80 символов")
    value = " ".join(value.split())
    if not value or len(value) > 80:
        raise ValueError("Название — от 1 до 80 символов")
    return value


async def init_cashflow(db: Database) -> None:
    """Additive, repeatable migration; no existing money records are changed."""
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def _ledger_fingerprint(conn: aiosqlite.Connection, budget_id: int) -> str:
    """Hash every row identity and revision, not a sum that can hide edits."""
    digest = hashlib.sha256()
    async with conn.execute(
        "SELECT id,version,amount_minor,occurred_on,kind FROM transactions WHERE user_id=? ORDER BY id",
        (budget_id,),
    ) as cursor:
        async for row in cursor:
            digest.update(json.dumps(list(row), ensure_ascii=True, separators=(",", ":")).encode())
            digest.update(b"\n")
    return digest.hexdigest()


async def ledger_fingerprint(db: Database, budget_id: int) -> str:
    """Capture the journal when cash is entered, before the final confirmation."""
    _budget_id(budget_id)
    async with aiosqlite.connect(db.path) as conn:
        return await _ledger_fingerprint(conn, budget_id)


async def get_profile(db: Database, budget_id: int) -> dict | None:
    _budget_id(budget_id)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT * FROM cashflow_profiles WHERE budget_id=?", (budget_id,),
        )).fetchone()
        return dict(row) if row else None


async def set_profile(db: Database, budget_id: int, *, balance_minor: int,
                      daily_minor: int, buffer_minor: int, confirmed_on: str,
                      expected_version: int | None,
                      expected_ledger_fingerprint: str | None = None) -> bool:
    """Confirm available cash, excluding money already separated for goals.

    A negative balance is allowed to represent a cash deficit. The buffer and
    expected daily spending are nonnegative. The snapshot and versioned write
    share a transaction so a simultaneous journal write cannot be missed.
    """
    _budget_id(budget_id)
    _version(expected_version)
    if (expected_ledger_fingerprint is not None and
            (not isinstance(expected_ledger_fingerprint, str) or
             not re.fullmatch(r"[0-9a-f]{64}", expected_ledger_fingerprint))):
        raise ValueError("Некорректная отметка состояния журнала")
    values = (_minor(balance_minor, signed=True), _minor(daily_minor),
              _minor(buffer_minor), _iso_date(confirmed_on))
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            fingerprint = await _ledger_fingerprint(conn, budget_id)
            if expected_ledger_fingerprint is not None and fingerprint != expected_ledger_fingerprint:
                await conn.commit()
                return False
            if expected_version is None:
                cursor = await conn.execute(
                    "INSERT INTO cashflow_profiles(budget_id,balance_minor,daily_minor,buffer_minor,confirmed_on,ledger_fingerprint) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(budget_id) DO NOTHING",
                    (budget_id, *values, fingerprint),
                )
            else:
                cursor = await conn.execute(
                    "UPDATE cashflow_profiles SET balance_minor=?,daily_minor=?,buffer_minor=?,confirmed_on=?,"
                    "ledger_fingerprint=?,version=version+1 WHERE budget_id=? AND version=?",
                    (*values, fingerprint, budget_id, expected_version),
                )
            await conn.commit()
            return bool(cursor.rowcount)
        except BaseException:
            await conn.rollback()
            raise


async def list_events(db: Database, budget_id: int, *, include_closed: bool = False) -> list[dict]:
    _budget_id(budget_id)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute(
            "SELECT * FROM cashflow_events WHERE budget_id=?" +
            ("" if include_closed else " AND is_closed=0") + " ORDER BY due_on,id", (budget_id,),
        )).fetchall()
        return [dict(row) for row in rows]


async def get_event(db: Database, budget_id: int, event_id: int) -> dict | None:
    _budget_id(budget_id)
    _identifier(event_id)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute(
            "SELECT * FROM cashflow_events WHERE budget_id=? AND id=?", (budget_id, event_id),
        )).fetchone()
        return dict(row) if row else None


async def add_event(db: Database, budget_id: int, *, kind: str, name: str,
                    amount_minor: int, due_on: str, create_key: str) -> int:
    _budget_id(budget_id)
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        raise ValueError("Выберите доход, расход или перевод в накопления")
    values = (kind, _name(name), _minor(amount_minor, positive=True), _iso_date(due_on))
    if (not isinstance(create_key, str) or not create_key.strip() or len(create_key) > 128
            or any(ord(char) < 32 or ord(char) == 127 for char in create_key)):
        raise ValueError("Некорректный ключ создания события")
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        try:
            existing = await (await conn.execute(
                "SELECT id FROM cashflow_events WHERE budget_id=? AND create_key=?", (budget_id, create_key),
            )).fetchone()
            if existing:
                await conn.commit()
                return existing[0]
            count = await (await conn.execute(
                "SELECT COUNT(*) FROM cashflow_events WHERE budget_id=? AND is_closed=0", (budget_id,),
            )).fetchone()
            if count[0] >= MAX_OPEN_EVENTS:
                raise ValueError("Можно вести до 50 ожидаемых событий. Сначала закройте одно из них")
            cursor = await conn.execute(
                "INSERT INTO cashflow_events(budget_id,kind,name,amount_minor,due_on,create_key) VALUES(?,?,?,?,?,?)",
                (budget_id, *values, create_key),
            )
            await conn.commit()
            return cursor.lastrowid
        except BaseException:
            await conn.rollback()
            raise


async def close_event(db: Database, budget_id: int, event_id: int, *, expected_version: int) -> bool:
    """Remove an expectation; actual income or spending must be recorded separately."""
    _budget_id(budget_id)
    _identifier(event_id)
    _version(expected_version)
    if expected_version is None:
        raise ValueError("Некорректная версия записи")
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        cursor = await conn.execute(
            "UPDATE cashflow_events SET is_closed=1,version=version+1 "
            "WHERE budget_id=? AND id=? AND version=? AND is_closed=0",
            (budget_id, event_id, expected_version),
        )
        await conn.commit()
        return bool(cursor.rowcount)


async def forecast(db: Database, budget_id: int, as_of: date) -> dict:
    """Project up to 90 inclusive days; each day's outflows precede its income.

    The default is 30 days. Past one-time expectations are never assumed to have
    happened. An unresolved past expense/saving or an invalid event date blocks
    numerical results until reconciliation: its amount may or may not already
    be reflected in the confirmed cash balance. Existing unpaid recurring arrears
    are included today, while paid occurrences and occurrences after a schedule
    was disabled are excluded by
    ``recurring.obligations``. Its end-of-month output is filtered to our horizon.
    """
    _budget_id(budget_id)
    # Recurring month helpers support years through 9998. Keep the complete
    # horizon inside their range as well as inside Python's date range.
    if (type(as_of) is not date or as_of.year < 2000 or
            as_of > date(9998, 12, 31) - timedelta(days=MAX_HORIZON_DAYS - 1)):
        raise ValueError("Некорректная дата прогноза")
    current = as_of.isoformat()
    last_allowed = (as_of + timedelta(days=MAX_HORIZON_DAYS - 1)).isoformat()
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN")
        row = await (await conn.execute(
            "SELECT * FROM cashflow_profiles WHERE budget_id=?", (budget_id,),
        )).fetchone()
        profile = dict(row) if row else None
        fingerprint = await _ledger_fingerprint(conn, budget_id)
        rows = await (await conn.execute(
            "SELECT * FROM cashflow_events WHERE budget_id=? AND is_closed=0 ORDER BY due_on,id", (budget_id,),
        )).fetchall()
        events = [dict(item) for item in rows]

    # Stored values can come from old versions or a manually restored database.
    # Unusable dates are surfaced separately instead of silently changing a plan.
    valid_events, invalid_events = [], []
    for event in events:
        try:
            _iso_date(event.get("due_on"))
        except (ValueError, TypeError):
            invalid_events.append(event)
        else:
            valid_events.append(event)
    income_dates = [event["due_on"] for event in valid_events
                    if event["kind"] == "income" and current <= event["due_on"] <= last_allowed]
    next_income = min(income_dates, default=None)
    horizon = date.fromisoformat(next_income) if next_income else as_of + timedelta(days=DEFAULT_HORIZON_DAYS - 1)
    horizon_text = horizon.isoformat()
    overdue = [event for event in valid_events if event["due_on"] < current]
    outside = [event for event in valid_events if event["due_on"] > horizon_text]
    stale_reason = ("missing_profile" if profile is None else
                    "date_changed" if profile["confirmed_on"] != current else
                    "ledger_changed" if profile["ledger_fingerprint"] != fingerprint else
                    "overdue_outflows" if any(event["kind"] in ("expense", "saving") for event in overdue) else
                    "invalid_events" if invalid_events else None)
    result = {"profile": profile, "stale": stale_reason is not None, "stale_reason": stale_reason,
              "events": events, "overdue_events": overdue, "outside_horizon_events": outside,
              "invalid_events": invalid_events, "next_income_on": next_income,
              "horizon_end": horizon_text, "rows": [], "recurring_items": [],
              "available_after_buffer_minor": None, "first_shortfall_on": None,
              "lowest_minor": None, "totals": None}
    if stale_reason:
        return result

    from .recurring import obligations
    bills = (await obligations(db, budget_id, horizon))["items"]
    bills = [dict(item, status="overdue" if item["due_on"] < current else
                  "today" if item["due_on"] == current else "upcoming")
             for item in bills if item["due_on"] <= horizon_text]
    # A linked bill may have been paid while obligations were read. Do not show
    # a number computed from an earlier balance after a concurrent journal edit.
    async with aiosqlite.connect(db.path) as conn:
        if await _ledger_fingerprint(conn, budget_id) != fingerprint:
            result.update(stale=True, stale_reason="ledger_changed")
            return result

    dated = {}
    for event in valid_events:
        if current <= event["due_on"] <= horizon_text:
            amounts = dated.setdefault(event["due_on"], {"income": 0, "expense": 0, "saving": 0, "recurring": 0})
            amounts[event["kind"]] += event["amount_minor"]
    for item in bills:
        amounts = dated.setdefault(max(current, item["due_on"]), {"income": 0, "expense": 0, "saving": 0, "recurring": 0})
        amounts["recurring"] += item["amount_minor"]

    balance = profile["balance_minor"] - profile["buffer_minor"]
    result.update(available_after_buffer_minor=balance, lowest_minor=balance,
                  recurring_items=bills)
    totals = {key: 0 for key in ("income_minor", "expense_minor", "saving_minor", "daily_minor", "recurring_minor")}
    day = as_of
    while day <= horizon:
        day_text = day.isoformat()
        amounts = dated.get(day_text, {"income": 0, "expense": 0, "saving": 0, "recurring": 0})
        expense = amounts["expense"] + amounts["recurring"]
        before_income = balance - profile["daily_minor"] - expense - amounts["saving"]
        balance = before_income + amounts["income"]
        result["lowest_minor"] = min(result["lowest_minor"], before_income)
        if before_income < 0 and result["first_shortfall_on"] is None:
            result["first_shortfall_on"] = day_text
        row = {"date": day_text, "before_income_minor": before_income,
               "income_minor": amounts["income"], "expense_minor": expense,
               "saving_minor": amounts["saving"], "daily_minor": profile["daily_minor"],
               "recurring_minor": amounts["recurring"], "end_minor": balance}
        result["rows"].append(row)
        for key in totals:
            totals[key] += row[key]
        day += timedelta(days=1)
    totals["outflow_minor"] = totals["expense_minor"] + totals["saving_minor"] + totals["daily_minor"]
    totals["end_minor"] = balance
    result["totals"] = totals
    return result
