from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import aiosqlite


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
    created_at TEXT NOT NULL
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
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    async def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    async def add_transaction(self, user_id: int, kind: str, category: str, amount: float, note: str = "") -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO transactions(user_id,kind,category,amount,note,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, kind, category, amount, note, datetime.now().isoformat(timespec="seconds")),
            )
            await db.commit()

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
        month = month or date.today().strftime("%Y-%m")
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            rows = await (await db.execute(
                "SELECT kind, COALESCE(SUM(amount),0) total FROM transactions WHERE user_id=? AND substr(created_at,1,7)=? GROUP BY kind",
                (user_id, month),
            )).fetchall()
            totals = {row["kind"]: row["total"] for row in rows}
            categories = await (await db.execute(
                "SELECT category, SUM(amount) total FROM transactions WHERE user_id=? AND kind='expense' AND substr(created_at,1,7)=? GROUP BY category ORDER BY total DESC LIMIT 5",
                (user_id, month),
            )).fetchall()
            debts = await (await db.execute(
                "SELECT direction, COALESCE(SUM(amount),0) total FROM debts WHERE user_id=? AND is_closed=0 GROUP BY direction",
                (user_id,),
            )).fetchall()
            goals = await (await db.execute(
                "SELECT name,target,saved,due_date FROM goals WHERE user_id=? ORDER BY id DESC LIMIT 10", (user_id,)
            )).fetchall()
        income, expense = totals.get("income", 0), totals.get("expense", 0)
        return {
            "month": month, "income": income, "expense": expense, "balance": income - expense,
            "categories": [dict(row) for row in categories],
            "debts": {row["direction"]: row["total"] for row in debts},
            "goals": [dict(row) for row in goals],
        }

