import asyncio
from datetime import datetime
import sqlite3
from types import SimpleNamespace

import pytest

from app.access import init_access, is_registered, register_user
from app.db import Database
from app import family


def test_registration_is_idempotent_and_persists_after_restart(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await init_access(db)
        assert not await is_registered(db, 101)
        assert await register_user(db, 101)
        with sqlite3.connect(db.path) as connection:
            original = connection.execute("SELECT * FROM access_users").fetchall()
        assert original[0][0] == 101
        assert datetime.fromisoformat(original[0][1]).utcoffset().total_seconds() == 0
        assert not await register_user(db, 101)

        restarted = Database(db.path)
        await restarted.init()
        await init_access(restarted)
        await init_access(restarted)
        assert await is_registered(restarted, 101)
        assert not await is_registered(restarted, 202)
        assert not await register_user(restarted, 101)
        with sqlite3.connect(db.path) as connection:
            assert connection.execute("SELECT * FROM access_users").fetchall() == original

    asyncio.run(scenario())


def test_concurrent_registration_creates_exactly_one_record(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await init_access(db)
        results = await asyncio.gather(*(register_user(db, 101) for _ in range(12)))
        assert results.count(True) == 1
        assert results.count(False) == 11
        assert await is_registered(db, 101)
        with sqlite3.connect(db.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM access_users").fetchone()[0] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("user_id", [True, False, 0, -1, -261928189, 1.0, "101", None, 2**63])
def test_invalid_ids_are_rejected_before_opening_database(tmp_path, user_id):
    async def scenario():
        # The parent directory intentionally does not exist: a DB call would fail.
        db = SimpleNamespace(path=tmp_path / "missing" / "budget.db")
        for operation in (register_user, is_registered):
            with pytest.raises(ValueError, match="Telegram ID"):
                await operation(db, user_id)
        assert not db.path.exists()

    asyncio.run(scenario())


def test_signup_does_not_copy_or_modify_private_or_family_data(tmp_path):
    def snapshot(db):
        with sqlite3.connect(db.path) as connection:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name!='access_users' ORDER BY name"
            )]
            return {name: connection.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall() for name in names}

    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await db.set_opening(101, 12345.67)
        await db.select_month(101, "2025-09")
        await db.set_budget(101, "2025-09", "продукты", 500)
        private_id = await db.add_transaction(101, "expense", "продукты", 100, occurred_on="2025-09-01")
        await db.add_debt(101, "Личный долг", 200, "i_owe")
        await db.add_goal(101, "Личная цель", 1000, 300)
        shared = await family.create_household(db, 101, display_name="Владелец")
        invite = await family.create_invite(db, 101)
        await family.join_household(db, 202, invite, "Участник")
        await db.set_opening(shared, 456)
        await db.select_month(shared, "2025-08")
        shared_id = await db.add_transaction(shared, "expense", "продукты", 250, actor_user_id=101, occurred_on="2025-09-01")
        before = snapshot(db)

        await init_access(db)
        for user_id in (101, 202, 303):
            assert await register_user(db, user_id)
        await init_access(db)
        assert not await register_user(db, 303)
        assert snapshot(db) == before
        assert await family.active_budget_id(db, 303) == 303
        assert await family.household_info(db, 303) is None
        assert await family.active_budget_id(db, 101) == shared
        assert await family.active_budget_id(db, 202) == shared
        assert await db.transaction(303, private_id) is None
        assert await db.transaction(303, shared_id) is None
        summary = await db.summary(303, "2025-09")
        assert summary["income"] == summary["expense"] == summary["balance"] == 0
        with sqlite3.connect(db.path) as connection:
            assert connection.execute("SELECT * FROM preferences WHERE user_id=303").fetchone() is None
            assert [row[1] for row in connection.execute("PRAGMA table_info(access_users)")] == ["user_id", "registered_at"]
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO access_users VALUES(?,?)", (shared, "2025-09-01T00:00:00+00:00"))

    asyncio.run(scenario())
