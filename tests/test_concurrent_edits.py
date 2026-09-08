import asyncio
import sqlite3

from app.db import Database


def test_stale_shared_edit_and_delete_preserve_newer_amount(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        transaction_id = await db.add_transaction(-1, "expense", "продукты", 100, actor_user_id=101)
        first_draft = await db.transaction(-1, transaction_id)
        assert first_draft["version"] == 1
        assert await db.edit_transaction(
            -1, transaction_id, kind="expense", category="продукты", amount=500,
            note="Сумму исправил второй участник", expected_version=first_draft["version"],
        )
        assert not await db.edit_transaction(
            -1, transaction_id, kind="expense", category="продукты", amount=first_draft["amount"],
            note="Первый участник менял только комментарий", expected_version=first_draft["version"],
        )
        assert not await db.delete_transaction(-1, transaction_id, expected_version=first_draft["version"])
        row = await db.transaction(-1, transaction_id)
        assert row["amount_minor"] == 50000
        assert row["note"] == "Сумму исправил второй участник"
        assert row["version"] == 2
        assert await db.delete_transaction(-1, transaction_id, expected_version=row["version"])
        assert not await db.delete_transaction(-1, transaction_id, expected_version=row["version"])
    asyncio.run(scenario())


def test_two_concurrent_edits_with_same_version_cannot_both_save(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        transaction_id = await db.add_transaction(-1, "expense", "продукты", 100, actor_user_id=101)
        results = await asyncio.gather(*(
            db.edit_transaction(-1, transaction_id, kind="expense", category="продукты", amount=amount, expected_version=1)
            for amount in (200, 500)
        ))
        assert sorted(results) == [False, True]
        row = await db.transaction(-1, transaction_id)
        assert row["version"] == 2
        assert row["amount"] == (200 if results[0] else 500)
    asyncio.run(scenario())


def test_version_checks_keep_budget_isolation_and_optional_api(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        transaction_id = await db.add_transaction(101, "expense", "продукты", 100)
        assert not await db.edit_transaction(202, transaction_id, kind="expense", category="продукты", amount=200, expected_version=1)
        assert not await db.delete_transaction(202, transaction_id, expected_version=1)
        assert (await db.transaction(101, transaction_id))["version"] == 1
        # Older internal clients still work, while invalidating any open drafts.
        assert await db.edit_transaction(101, transaction_id, kind="expense", category="продукты", amount=200)
        assert (await db.transaction(101, transaction_id))["version"] == 2
        assert not await db.edit_transaction(101, transaction_id, kind="expense", category="продукты", amount=300, expected_version=1)
        assert await db.delete_transaction(101, transaction_id)
    asyncio.run(scenario())


def test_version_migration_preserves_legacy_data_and_existing_versions(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE transactions(id INTEGER PRIMARY KEY,user_id INTEGER,kind TEXT,category TEXT,amount REAL,note TEXT,created_at TEXT)")
        conn.execute("INSERT INTO transactions VALUES(1,101,'expense','продукты',100.25,'старая запись','2025-09-01T10:00:00')")

    async def scenario():
        db = Database(path)
        await db.init()
        row = await db.transaction(101, 1)
        assert row["version"] == 1
        assert row["amount_minor"] == 10025
        assert row["note"] == "старая запись"
        assert row["occurred_on"] == "2025-09-01"
        assert await db.edit_transaction(101, 1, kind="expense", category="продукты", amount=100.25, note="уточнено", occurred_on=row["occurred_on"], expected_version=1)
        await db.init()
        await db.init()
        row = await db.transaction(101, 1)
        assert row["version"] == 2
        assert row["amount_minor"] == 10025
        assert row["note"] == "уточнено"
        assert row["occurred_on"] == "2025-09-01"
    asyncio.run(scenario())
