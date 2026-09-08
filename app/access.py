"""Persistent access records, separate from personal and family budget data."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite


SCHEMA = """
CREATE TABLE IF NOT EXISTS access_users (
    user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
    registered_at TEXT NOT NULL
);
"""


def _validate_user_id(user_id: int) -> None:
    # bool is an int subclass; family budget IDs are negative and are not users.
    if type(user_id) is not int or not 0 < user_id <= 2**63 - 1:
        raise ValueError("Некорректный Telegram ID")


async def init_access(db) -> None:
    async with aiosqlite.connect(db.path) as connection:
        await connection.executescript(SCHEMA)
        await connection.commit()


async def register_user(db, user_id: int) -> bool:
    """Register a Telegram user once; return whether this call created the row."""
    _validate_user_id(user_id)
    async with aiosqlite.connect(db.path) as connection:
        cursor = await connection.execute(
            "INSERT INTO access_users(user_id,registered_at) VALUES(?,?) "
            "ON CONFLICT(user_id) DO NOTHING",
            (user_id, datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        await connection.commit()
        return bool(cursor.rowcount)


async def is_registered(db, user_id: int) -> bool:
    _validate_user_id(user_id)
    async with aiosqlite.connect(db.path) as connection:
        row = await (await connection.execute(
            "SELECT 1 FROM access_users WHERE user_id=?", (user_id,),
        )).fetchone()
        return row is not None
