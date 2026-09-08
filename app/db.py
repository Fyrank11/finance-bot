from __future__ import annotations

from datetime import datetime, timezone as utc_timezone
from pathlib import Path

import aiosqlite

from .inputs import category_name, shift_month, to_minor, today, valid_month


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
    category TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_transactions_user_date
ON transactions(user_id, created_at);
CREATE TABLE IF NOT EXISTS debts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    direction TEXT NOT NULL CHECK(direction IN ('i_owe','owed_to_me')),
    due_date TEXT,
    is_closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    target REAL NOT NULL CHECK(target > 0),
    saved REAL NOT NULL DEFAULT 0 CHECK(saved >= 0),
    due_date TEXT
);
CREATE TABLE IF NOT EXISTS preferences (
    user_id INTEGER PRIMARY KEY,
    opening_minor INTEGER NOT NULL DEFAULT 0,
    selected_month TEXT
);
CREATE TABLE IF NOT EXISTS budgets (
    user_id INTEGER NOT NULL,
    month TEXT NOT NULL,
    category TEXT NOT NULL,
    limit_minor INTEGER NOT NULL CHECK(limit_minor >= 0),
    PRIMARY KEY(user_id, month, category)
);
"""


class Database:
    def __init__(self, path: Path, timezone: str = "Europe/Moscow"):
        self.path = path
        self.timezone = timezone

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            columns = {row[1] for row in await (await db.execute("PRAGMA table_info(transactions)")).fetchall()}
            for name, definition in {
                "amount_minor": "INTEGER", "occurred_on": "TEXT", "source_message_id": "INTEGER",
                "actor_user_id": "INTEGER",
                "actor_name": "TEXT NOT NULL DEFAULT ''",
                "version": "INTEGER NOT NULL DEFAULT 1",
            }.items():
                if name not in columns:
                    await db.execute(f"ALTER TABLE transactions ADD COLUMN {name} {definition}")
            # Preserve legacy dates (the old app used local naive timestamps) and round once.
            await db.execute("UPDATE transactions SET amount_minor=CAST(ROUND(amount*100) AS INTEGER) WHERE amount_minor IS NULL")
            await db.execute("UPDATE transactions SET occurred_on=substr(created_at,1,10) WHERE occurred_on IS NULL")
            await db.execute("UPDATE transactions SET actor_user_id=user_id WHERE actor_user_id IS NULL AND user_id>0")
            # Canonicalize existing categories without merging or deleting any transactions.
            for row in await (await db.execute("SELECT id,category FROM transactions")).fetchall():
                try:
                    normalized = category_name(row[1])
                except ValueError:
                    normalized = row[1]  # Legacy custom labels must not prevent the upgrade.
                await db.execute("UPDATE transactions SET category=? WHERE id=?", (normalized, row[0]))
            await db.execute("CREATE INDEX IF NOT EXISTS idx_transaction_occurrence ON transactions(user_id,occurred_on)")
            # Message IDs are unique within a Telegram chat, not across family members.
            await db.execute("DROP INDEX IF EXISTS idx_transaction_source")
            await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_transaction_actor_source ON transactions(user_id,actor_user_id,source_message_id) WHERE source_message_id IS NOT NULL")
            await db.commit()
        from .family import init_family
        from .recurring import init_recurring
        from .access import init_access
        await init_family(self)
        await init_recurring(self)
        await init_access(self)

    def _transaction_values(self, kind: str, category: str, amount: float, note: str, occurred_on: str | None) -> tuple:
        if kind not in ("income", "expense"):
            raise ValueError("Некорректный тип операции")
        day = occurred_on or today(self.timezone).isoformat()
        from datetime import date
        parsed = date.fromisoformat(day)
        if parsed.year < 2000 or parsed > today(self.timezone):
            raise ValueError("Некорректная дата операции")
        minor = to_minor(amount)
        return kind, category_name(category), minor / 100, note[:500], minor, day

    async def add_transaction(self, user_id: int, kind: str, category: str, amount: float, note: str = "", *, occurred_on: str | None = None, source_message_id: int | None = None, actor_user_id: int | None = None, actor_name: str = '') -> int:
        values = self._transaction_values(kind, category, amount, note, occurred_on)
        actor_user_id = actor_user_id if actor_user_id is not None else user_id
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                "INSERT INTO transactions(user_id,kind,category,amount,note,amount_minor,occurred_on,created_at,source_message_id,actor_user_id,actor_name) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(user_id,actor_user_id,source_message_id) WHERE source_message_id IS NOT NULL DO NOTHING",
                (user_id, *values, datetime.now(utc_timezone.utc).isoformat(timespec="seconds"), source_message_id, actor_user_id, actor_name[:80]),
            )
            await db.commit()
            if cursor.rowcount:
                return cursor.lastrowid
            row = await (await db.execute("SELECT id FROM transactions WHERE user_id=? AND actor_user_id=? AND source_message_id=?", (user_id, actor_user_id, source_message_id))).fetchone()
            return row[0]

    async def transaction(self, user_id: int, transaction_id: int) -> dict | None:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            row = await (await db.execute("SELECT * FROM transactions WHERE user_id=? AND id=?", (user_id, transaction_id))).fetchone()
            return dict(row) if row else None

    async def edit_transaction(self, user_id: int, transaction_id: int, *, kind: str, category: str, amount: float, note: str = "", occurred_on: str | None = None, expected_version: int | None = None) -> bool:
        values = self._transaction_values(kind, category, amount, note, occurred_on)
        where = "user_id=? AND id=?"
        params = (*values, user_id, transaction_id)
        if expected_version is not None:
            where += " AND version=?"
            params += (expected_version,)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(
                f"UPDATE transactions SET kind=?,category=?,amount=?,note=?,amount_minor=?,occurred_on=?,version=version+1 WHERE {where}",
                params,
            )
            await db.commit()
            return bool(cursor.rowcount)

    async def delete_transaction(self, user_id: int, transaction_id: int, *, expected_version: int | None = None) -> bool:
        where = "user_id=? AND id=?"
        params = (user_id, transaction_id)
        if expected_version is not None:
            where += " AND version=?"
            params += (expected_version,)
        async with aiosqlite.connect(self.path) as db:
            cursor = await db.execute(f"DELETE FROM transactions WHERE {where}", params)
            await db.commit()
            return bool(cursor.rowcount)

    async def transactions(self, user_id: int, month: str, *, offset: int = 0, limit: int = 8) -> list[dict]:
        month = valid_month(month)
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT * FROM transactions WHERE user_id=? AND occurred_on>=? AND occurred_on<? ORDER BY occurred_on DESC,id DESC LIMIT ? OFFSET ?",
                (user_id, month + "-01", shift_month(month, 1) + "-01", limit, max(offset, 0)),
            )).fetchall()
            return [dict(row) for row in rows]

    async def selected_month(self, user_id: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            row = await (await db.execute("SELECT selected_month FROM preferences WHERE user_id=?", (user_id,))).fetchone()
            return row[0] if row and row[0] else today(self.timezone).strftime("%Y-%m")

    async def select_month(self, user_id: int, month: str) -> None:
        month = valid_month(month)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO preferences(user_id,selected_month) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET selected_month=excluded.selected_month", (user_id, month))
            await db.commit()

    async def set_opening(self, user_id: int, amount: float) -> None:
        minor = to_minor(amount, allow_zero=True)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("INSERT INTO preferences(user_id,opening_minor) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET opening_minor=excluded.opening_minor", (user_id, minor))
            await db.commit()

    async def set_budget(self, user_id: int, month: str, category: str, amount: float | None) -> None:
        month, category = valid_month(month), category_name(category)
        async with aiosqlite.connect(self.path) as db:
            if amount is None:
                await db.execute("DELETE FROM budgets WHERE user_id=? AND month=? AND category=?", (user_id, month, category))
            else:
                await db.execute("INSERT INTO budgets VALUES(?,?,?,?) ON CONFLICT(user_id,month,category) DO UPDATE SET limit_minor=excluded.limit_minor", (user_id, month, category, to_minor(amount, allow_zero=True)))
            await db.commit()

    async def budget_report(self, user_id: int, month: str) -> list[dict]:
        month = valid_month(month)
        async with aiosqlite.connect(self.path) as db:
            rows = await (await db.execute("SELECT category,limit_minor FROM budgets WHERE user_id=? AND month=?", (user_id, month))).fetchall()
            actual = dict(await (await db.execute("SELECT category,SUM(amount_minor) FROM transactions WHERE user_id=? AND kind='expense' AND occurred_on>=? AND occurred_on<? GROUP BY category", (user_id, month + "-01", shift_month(month, 1) + "-01"))).fetchall())
        planned = dict(rows)
        return [{"category": c, "limit": planned[c] / 100 if c in planned else None, "spent": actual.get(c, 0) / 100} for c in sorted(planned.keys() | actual.keys())]

    async def add_debt(self, user_id: int, name: str, amount: float, direction: str, due_date: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO debts(user_id,name,amount,direction,due_date) VALUES(?,?,?,?,?)",
                (user_id, name, amount, direction, due_date),
            )
            await db.commit()

    async def add_goal(self, user_id: int, name: str, target: float, saved: float = 0, due_date: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO goals(user_id,name,target,saved,due_date) VALUES(?,?,?,?,?)",
                (user_id, name, target, saved, due_date),
            )
            await db.commit()

    async def summary(self, user_id: int, month: str | None = None) -> dict:
        month = valid_month(month or today(self.timezone).strftime("%Y-%m"))
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT kind, COALESCE(SUM(amount_minor),0) / 100.0 total FROM transactions WHERE user_id=? AND substr(occurred_on,1,7)=? GROUP BY kind",
                (user_id, month),
            )).fetchall()
            totals = {row["kind"]: row["total"] for row in rows}
            categories = await (await db.execute(
                "SELECT category, SUM(amount_minor) / 100.0 total FROM transactions WHERE user_id=? AND kind='expense' AND substr(occurred_on,1,7)=? GROUP BY category ORDER BY total DESC LIMIT 5",
                (user_id, month),
            )).fetchall()
            debts = await (await db.execute(
                "SELECT direction, COALESCE(SUM(amount),0) total FROM debts WHERE user_id=? AND is_closed=0 GROUP BY direction",
                (user_id,),
            )).fetchall()
            goals = await (await db.execute(
                "SELECT name,target,saved,due_date FROM goals WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)
            )).fetchall()
            opening_row = await (await db.execute("SELECT opening_minor FROM preferences WHERE user_id=?", (user_id,))).fetchone()
            opening = opening_row[0] if opening_row else 0
            net = await (await db.execute("SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE -amount_minor END),0) FROM transactions WHERE user_id=? AND occurred_on<?", (user_id, shift_month(month, 1) + "-01"))).fetchone()
        income, expense = totals.get("income", 0), totals.get("expense", 0)
        return {
            "month": month, "income": income, "expense": expense, "net": round(income - expense, 2),
            "balance": (opening + net[0]) / 100, "opening": opening / 100,
            "categories": [dict(row) for row in categories],
            "debts": {row["direction"]: row["total"] for row in debts},
            "goals": [dict(row) for row in goals],
        }
