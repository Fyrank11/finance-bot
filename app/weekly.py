"""Opt-in weekly reports, pinned to a confirmed personal or family budget.

Scheduling uses the recipient's IANA timezone. On a DST gap, a requested time
moves to the first valid minute within three hours; an entirely skipped day is
skipped. On a fold, only the first occurrence is used. Enabling or editing a
schedule always starts strictly in the future. After downtime, only the latest
scheduled occurrence can be sent, and only within 24 hours of its due time.

Delivery is an at-most-once automatic *attempt*: claiming and advancing the
schedule commits before Telegram is called. A crash, timeout or ambiguous
Telegram response is never retried automatically; the following week continues.
Exactly-once delivery cannot be guaranteed across SQLite and Telegram. No
financial report text, token or exception message is stored in delivery logs.

The worker and user handlers must share ``access_lock``. It covers authorization,
claiming, building and sending each report, so a queued report cannot bypass a
completed opt-out or household removal. Stop the worker before closing Bot's
session; an in-flight send has a ten-second timeout.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiosqlite
from aiogram.exceptions import TelegramForbiddenError

from .coaching import _money
from .recurring import obligations

LOGGER = logging.getLogger(__name__)
UTC = utc_timezone.utc
SEND_TIMEOUT = 10
POLL_SECONDS = 30
SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_subscriptions (
    user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
    budget_id INTEGER NOT NULL CHECK(budget_id != 0),
    weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
    hour INTEGER NOT NULL CHECK(hour BETWEEN 0 AND 23),
    minute INTEGER NOT NULL CHECK(minute BETWEEN 0 AND 59),
    timezone TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0,1)),
    version INTEGER NOT NULL DEFAULT 1,
    next_due_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_weekly_due ON weekly_subscriptions(enabled,next_due_at);
CREATE TABLE IF NOT EXISTS weekly_deliveries (
    user_id INTEGER NOT NULL,
    scheduled_at TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('claimed','sent','failed','blocked')),
    PRIMARY KEY(user_id,scheduled_at)
);
"""


def _id(value: int, *, positive: bool = False) -> int:
    if type(value) is not int or not -(2**63 - 1) <= value <= 2**63 - 1 or value == 0 or (positive and value < 0):
        raise ValueError("Некорректный идентификатор пользователя или бюджета")
    return value


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Время должно содержать часовой пояс")
    return value.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds")


def _schedule(weekday: int, hour: int, minute: int, timezone: str) -> ZoneInfo:
    if any(type(value) is not int or not 0 <= value <= maximum
           for value, maximum in ((weekday, 6), (hour, 23), (minute, 59))):
        raise ValueError("Проверьте день недели и время обзора")
    if not isinstance(timezone, str) or len(timezone) > 100:
        raise ValueError("Некорректный часовой пояс")
    try:
        return ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Неизвестный часовой пояс IANA") from exc


def _occurrence(day: date, hour: int, minute: int, zone: ZoneInfo) -> datetime | None:
    local = datetime.combine(day, time(hour, minute))
    for offset in range(181):
        candidate = local + timedelta(minutes=offset)
        if candidate.date() != day:
            return None
        aware = candidate.replace(tzinfo=zone, fold=0)
        absolute = aware.astimezone(UTC)
        if absolute.astimezone(zone).replace(tzinfo=None) == candidate:
            return absolute
    return None


def next_due(now: datetime, *, weekday: int, hour: int, minute: int, timezone: str) -> datetime:
    """The first selected civil-time occurrence strictly after ``now`` in UTC."""
    now = _utc(now)
    zone = _schedule(weekday, hour, minute, timezone)
    current = now.astimezone(zone).date()
    for offset in range(15):
        day = current + timedelta(days=offset)
        if day.weekday() == weekday:
            candidate = _occurrence(day, hour, minute, zone)
            if candidate is not None and candidate > now:
                return candidate
    raise ValueError("Не удалось определить дату следующего обзора")


def _latest_due(now: datetime, row: dict) -> datetime | None:
    zone = _schedule(row["weekday"], row["hour"], row["minute"], row["timezone"])
    current = now.astimezone(zone).date()
    for offset in range(15):
        day = current - timedelta(days=offset)
        if day.weekday() == row["weekday"]:
            candidate = _occurrence(day, row["hour"], row["minute"], zone)
            if candidate is not None and candidate <= now:
                return candidate
    return None


async def init_weekly(db) -> None:
    async with aiosqlite.connect(db.path) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def get_subscription(db, user_id: int) -> dict | None:
    _id(user_id, positive=True)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        row = await (await conn.execute("SELECT * FROM weekly_subscriptions WHERE user_id=?", (user_id,))).fetchone()
        return dict(row) if row else None


async def _budget_access(conn, user_id: int, budget_id: int) -> bool:
    if budget_id > 0:
        return budget_id == user_id
    return bool(await (await conn.execute(
        "SELECT 1 FROM family_members m JOIN family_households h ON h.id=m.household_id "
        "WHERE m.user_id=? AND m.household_id=?", (user_id, -budget_id),
    )).fetchone())


async def set_subscription(db, user_id: int, *, budget_id: int, weekday: int, hour: int,
                           minute: int, timezone: str, enabled: bool,
                           expected_version: int | None, now: datetime) -> bool:
    _id(user_id, positive=True)
    _id(budget_id)
    _schedule(weekday, hour, minute, timezone)
    if type(enabled) is not bool:
        raise ValueError("Укажите, включены ли обзоры")
    if expected_version is not None and (type(expected_version) is not int or not 1 <= expected_version < 2**63):
        raise ValueError("Некорректная версия настроек")
    now = _utc(now)
    due = _stamp(next_due(now, weekday=weekday, hour=hour, minute=minute, timezone=timezone)) if enabled else None
    async with aiosqlite.connect(db.path, timeout=10) as conn:
        await conn.execute("BEGIN IMMEDIATE")
        if budget_id > 0 and budget_id != user_id or enabled and not await _budget_access(conn, user_id, budget_id):
            raise ValueError("Нет доступа к выбранному бюджету")
        if expected_version is None:
            cursor = await conn.execute(
                "INSERT INTO weekly_subscriptions(user_id,budget_id,weekday,hour,minute,timezone,enabled,next_due_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(user_id) DO NOTHING",
                (user_id, budget_id, weekday, hour, minute, timezone, int(enabled), due, _stamp(now)),
            )
        else:
            cursor = await conn.execute(
                "UPDATE weekly_subscriptions SET budget_id=?,weekday=?,hour=?,minute=?,timezone=?,enabled=?,"
                "next_due_at=?,updated_at=?,version=version+1 WHERE user_id=? AND version=?",
                (budget_id, weekday, hour, minute, timezone, int(enabled), due, _stamp(now), user_id, expected_version),
            )
        await conn.commit()
        return bool(cursor.rowcount)


def _label(value: str, maximum: int = 70) -> str:
    return " ".join(str(value).split())[:maximum]


async def build_digest(db, budget_id: int, as_of: date) -> str:
    """Plain-text report for seven completed calendar days; does not write money."""
    _id(budget_id)
    if type(as_of) is not date:
        raise ValueError("Укажите дату обзора")
    start, end = as_of - timedelta(days=7), as_of - timedelta(days=1)
    previous = start - timedelta(days=7)
    async with aiosqlite.connect(db.path) as conn:
        rows = await (await conn.execute(
            "SELECT CASE WHEN occurred_on>=? THEN 1 ELSE 0 END period,kind,COUNT(*),SUM(amount_minor) "
            "FROM transactions WHERE user_id=? AND occurred_on>=? AND occurred_on<? GROUP BY period,kind",
            (start.isoformat(), budget_id, previous.isoformat(), as_of.isoformat()),
        )).fetchall()
        categories = await (await conn.execute(
            "SELECT category,SUM(amount_minor) total FROM transactions WHERE user_id=? AND kind='expense' "
            "AND occurred_on>=? AND occurred_on<? GROUP BY category ORDER BY total DESC,category LIMIT 3",
            (budget_id, start.isoformat(), as_of.isoformat()),
        )).fetchall()
        excess = await (await conn.execute(
            "SELECT b.category,b.limit_minor,COALESCE(SUM(t.amount_minor),0) spent FROM budgets b "
            "LEFT JOIN transactions t ON t.user_id=b.user_id AND t.category=b.category AND t.kind='expense' "
            "AND t.occurred_on>=? AND t.occurred_on<? WHERE b.user_id=? AND b.month=? "
            "GROUP BY b.category,b.limit_minor HAVING spent>b.limit_minor ORDER BY spent-b.limit_minor DESC LIMIT 1",
            (as_of.strftime("%Y-%m-01"), as_of.isoformat(), budget_id, as_of.strftime("%Y-%m")),
        )).fetchone()
    amounts = {(row[0], row[1]): row[3] for row in rows}
    counts = {period: sum(row[2] for row in rows if row[0] == period) for period in (0, 1)}
    income, expense = amounts.get((1, "income"), 0), amounts.get((1, "expense"), 0)
    lines = [f"📬 Недельный обзор · {'семейный' if budget_id < 0 else 'личный'} бюджет",
             f"{start:%d.%m.%Y}–{end:%d.%m.%Y} · завершённые 7 дней",
             f"Даты учёта: {db.timezone}",
             f"\nВнесено операций: {counts[1]}", f"Доходы: {_money(income)}", f"Расходы: {_money(expense)}",
             f"Разница доходов и расходов: {_money(income - expense)} (это не остаток денег)."]
    if counts[0] and counts[1]:
        delta = expense - amounts.get((0, "expense"), 0)
        direction = "больше" if delta > 0 else "меньше" if delta < 0 else "столько же"
        detail = f" на {_money(abs(delta))}" if delta else ""
        lines.append(f"\nРасходов в записях {direction}{detail}, чем за {previous:%d.%m}–{start - timedelta(days=1):%d.%m}.")
    else:
        lines.append("\nДля сравнения недель недостаточно записей в одном из периодов; это не означает экономию.")
    if categories:
        lines.append("\nБольше всего расходов:")
        lines.extend(f"• {_label(category)}: {_money(total)}" for category, total in categories)
    if excess:
        lines.append(f"\nЛимит месяца «{_label(excess[0])}»: {_money(excess[2])} из {_money(excess[1])} по записям до {end:%d.%m}.")
    upcoming_end = as_of + timedelta(days=6)
    report = await obligations(db, budget_id, upcoming_end)
    overdue = [item for item in report["items"] if item["due_on"] < as_of.isoformat()]
    upcoming = [item for item in report["items"] if as_of.isoformat() <= item["due_on"] <= upcoming_end.isoformat()]
    lines.append(f"\n🗓 Платежи {as_of:%d.%m}–{upcoming_end:%d.%m}:")
    if upcoming:
        lines.append(f"Не отмечено оплаченными: {len(upcoming)}, всего {_money(sum(i['amount_minor'] for i in upcoming))}.")
        lines.extend(f"• {date.fromisoformat(i['due_on']):%d.%m} · {_label(i['name'])}: {_money(i['amount_minor'])}"
                     for i in upcoming[:3])
        if len(upcoming) > 3:
            lines.append("Остальные платежи — /payments.")
    else:
        lines.append("Неоплаченных платежей в расписании на эти даты нет.")
    if overdue:
        lines.append(f"Отдельно: срок прошёл у {len(overdue)} платежей на {_money(sum(i['amount_minor'] for i in overdue))}.")
    if upcoming or overdue:
        lines.append("Если уже оплатили, отметьте платёж в боте.")
    lines.append("\nОбзор основан только на внесённых данных. Сверьте записи: отсутствие операции не подтверждает отсутствие траты. Ожидаемые доходы не считаются полученными.")
    lines.append("Время, выбранный бюджет и отключение обзоров — /weekly.")
    return "\n".join(lines)


async def _disable(conn, user_id: int) -> None:
    await conn.execute("UPDATE weekly_subscriptions SET enabled=0,next_due_at=NULL,version=version+1 WHERE user_id=? AND enabled=1", (user_id,))


async def _claim(db, candidate: dict, now: datetime, allowed_user_ids: frozenset,
                 public_signup: bool) -> tuple[dict, datetime] | None:
    """Caller holds access_lock; transaction rechecks current permission and row."""
    async with aiosqlite.connect(db.path, timeout=10) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("BEGIN IMMEDIATE")
        row = await (await conn.execute("SELECT * FROM weekly_subscriptions WHERE user_id=?", (candidate["user_id"],))).fetchone()
        if not row or not row["enabled"] or row["version"] != candidate["version"] or not row["next_due_at"]:
            return None
        row = dict(row)
        stored_due = _utc(datetime.fromisoformat(row["next_due_at"]))
        if stored_due > now:
            return None
        authorized = row["user_id"] in allowed_user_ids
        if not authorized and public_signup:
            authorized = bool(await (await conn.execute("SELECT 1 FROM access_users WHERE user_id=?", (row["user_id"],))).fetchone())
        if not authorized or not await _budget_access(conn, row["user_id"], row["budget_id"]):
            await _disable(conn, row["user_id"])
            await conn.commit()
            return None
        latest = _latest_due(now, row)
        following = next_due(now, weekday=row["weekday"], hour=row["hour"], minute=row["minute"], timezone=row["timezone"])
        await conn.execute("UPDATE weekly_subscriptions SET next_due_at=? WHERE user_id=?", (_stamp(following), row["user_id"]))
        if latest is None or latest < stored_due or now - latest > timedelta(hours=24):
            await conn.commit()
            return None
        cursor = await conn.execute(
            "INSERT INTO weekly_deliveries(user_id,scheduled_at,claimed_at,status) VALUES(?,?,?,'claimed') "
            "ON CONFLICT(user_id,scheduled_at) DO NOTHING", (row["user_id"], _stamp(latest), _stamp(now)),
        )
        await conn.commit()
        return (row, latest) if cursor.rowcount else None


async def _finish(db, user_id: int, scheduled: datetime, now: datetime, status: str) -> None:
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute("UPDATE weekly_deliveries SET status=?,finished_at=? WHERE user_id=? AND scheduled_at=?",
                           (status, _stamp(now), user_id, _stamp(scheduled)))
        if status == "blocked":
            await _disable(conn, user_id)
        await conn.commit()


async def dispatch_due(bot, db, *, access_lock: asyncio.Lock, allowed_user_ids: frozenset,
                       public_signup: bool, now: datetime, stop_event: asyncio.Event | None = None) -> int:
    """Attempt each currently due subscription once; return confirmed sent count."""
    now = _utc(now)
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        candidates = await (await conn.execute(
            "SELECT * FROM weekly_subscriptions WHERE enabled=1 AND next_due_at<=? ORDER BY next_due_at,user_id",
            (_stamp(now),),
        )).fetchall()
    sent = 0
    for candidate in candidates:
        if stop_event is not None and stop_event.is_set():
            break
        try:
            async with access_lock:
                if stop_event is not None and stop_event.is_set():
                    break
                claimed = await _claim(db, dict(candidate), now, allowed_user_ids, public_signup)
                if claimed is None:
                    continue
                row, scheduled = claimed
                try:
                    # Delivery timezone only selects the send time. Transaction dates
                    # belong to the database's bookkeeping timezone, even if the
                    # recipient is already on the following calendar day elsewhere.
                    # Late delivery keeps the original scheduled reporting period.
                    text = await build_digest(db, row["budget_id"], scheduled.astimezone(ZoneInfo(db.timezone)).date())
                    await asyncio.wait_for(bot.send_message(chat_id=row["user_id"], text=text, parse_mode=None), timeout=SEND_TIMEOUT)
                    status = "sent"
                    sent += 1
                except TelegramForbiddenError:
                    status = "blocked"
                except Exception:
                    # Includes RetryAfter: no immediate or ambiguous retry, even after restart.
                    status = "failed"
                    LOGGER.warning("Weekly report attempt failed; next scheduled week remains enabled")
                await _finish(db, row["user_id"], scheduled, now, status)
        except Exception:
            # Isolate malformed persisted schedules and individual DB/send failures.
            LOGGER.warning("Weekly report processing failed for one subscription")
    return sent


async def run_weekly_worker(bot, db, *, access_lock: asyncio.Lock, allowed_user_ids: frozenset,
                            public_signup: bool, stop_event: asyncio.Event) -> None:
    await init_weekly(db)
    while not stop_event.is_set():
        try:
            await dispatch_due(bot, db, access_lock=access_lock, allowed_user_ids=allowed_user_ids,
                               public_signup=public_signup, now=datetime.now(UTC), stop_event=stop_event)
        except Exception:
            LOGGER.warning("Weekly report worker iteration failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass
