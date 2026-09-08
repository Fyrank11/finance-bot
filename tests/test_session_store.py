import asyncio
from dataclasses import replace
import json
import sqlite3

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey
import pytest

from app import session_store
from app.session_store import SQLiteStorage


KEY = StorageKey(bot_id=123, chat_id=101, user_id=101)


class Draft(StatesGroup):
    amount = State()
    confirmation = State()


def test_fsm_draft_survives_close_and_restart_and_can_be_cleared(tmp_path):
    async def scenario():
        path = tmp_path / "nested" / "budget.db"
        storage = SQLiteStorage(path)
        state = FSMContext(storage, KEY)
        await state.set_state(Draft.amount)
        await state.update_data(
            kind="expense", amount=120.05, category="Продукты",
            _scope={"budget_id": -77, "family_revision": 3},
        )
        await storage.close()

        restored = SQLiteStorage(path)
        await restored.init()
        await restored.init()
        state = FSMContext(restored, KEY)
        assert await state.get_state() == "Draft:amount"
        assert await state.get_data() == {
            "kind": "expense", "amount": 120.05, "category": "Продукты",
            "_scope": {"budget_id": -77, "family_revision": 3},
        }
        await state.set_state(Draft.confirmation)
        assert (await state.get_data())["amount"] == 120.05
        await state.set_data({"amount": 200})
        assert await state.get_state() == "Draft:confirmation"
        await state.clear()
        await restored.close()
        again = SQLiteStorage(path)
        assert await again.get_state(KEY) is None
        assert await again.get_data(KEY) == {}

    asyncio.run(scenario())


def test_every_storage_key_field_isolates_sessions(tmp_path):
    async def scenario():
        storage = SQLiteStorage(tmp_path / "budget.db")
        keys = [
            KEY, replace(KEY, bot_id=124), replace(KEY, chat_id=102),
            replace(KEY, user_id=102), replace(KEY, thread_id=0),
            replace(KEY, thread_id=7), replace(KEY, business_connection_id=""),
            replace(KEY, business_connection_id="0"), replace(KEY, destiny="alternate"),
            replace(KEY, business_connection_id="a:b", destiny="c"),
            replace(KEY, business_connection_id="a", destiny="b:c"),
        ]
        for index, key in enumerate(keys):
            await storage.set_state(key, f"State:{index}")
            await storage.set_data(key, {"owner": index})
        restored = SQLiteStorage(storage.path)
        for index, key in enumerate(keys):
            assert await restored.get_state(key) == f"State:{index}"
            assert await restored.get_data(key) == {"owner": index}

    asyncio.run(scenario())


def test_data_is_detached_from_callers_and_returned_values(tmp_path):
    async def scenario():
        storage = SQLiteStorage(tmp_path / "budget.db")
        original = {"draft": {"amount": 100, "tags": ["еда"]}}
        await storage.set_data(KEY, original)
        original["draft"]["amount"] = 999
        read = await storage.get_data(KEY)
        assert read["draft"]["amount"] == 100
        read["draft"]["tags"].append("не сохранено")
        returned = await storage.update_data(KEY, {"note": {"text": "обед"}})
        returned["note"]["text"] = "не сохранено"
        single = await storage.get_value(KEY, "draft")
        single["amount"] = 999
        assert await storage.get_data(KEY) == {
            "draft": {"amount": 100, "tags": ["еда"]}, "note": {"text": "обед"},
        }
        assert await storage.get_value(KEY, "missing", "default") == "default"

    asyncio.run(scenario())


def test_concurrent_partial_updates_and_state_change_do_not_lose_data(tmp_path):
    async def scenario():
        path = tmp_path / "budget.db"
        first, second = SQLiteStorage(path), SQLiteStorage(path)
        await asyncio.gather(first.init(), second.init())
        await first.set_data(KEY, {"_scope": {"budget_id": 101, "family_revision": 0}})
        await asyncio.gather(
            *( (first if i % 2 else second).update_data(KEY, {f"field_{i}": i})
               for i in range(32)),
            first.set_state(KEY, Draft.confirmation),
        )
        result = await second.get_data(KEY)
        assert result == {
            "_scope": {"budget_id": 101, "family_revision": 0},
            **{f"field_{i}": i for i in range(32)},
        }
        assert await first.get_state(KEY) == "Draft:confirmation"

    asyncio.run(scenario())


@pytest.mark.parametrize("payload", ['{"private draft":', '[]', 'null', '{"amount":NaN}', '{"amount":1e999}'])
def test_corrupt_json_discards_state_and_data_without_logging_contents(tmp_path, caplog, payload):
    async def scenario():
        storage = SQLiteStorage(tmp_path / "budget.db")
        await storage.set_state(KEY, Draft.confirmation)
        await storage.set_data(KEY, {"amount": 999})
        with sqlite3.connect(storage.path) as connection:
            connection.execute("UPDATE fsm_sessions SET data=?", (payload,))
        assert await storage.get_state(KEY) is None
        assert await storage.get_data(KEY) == {}
        assert "invalid persisted conversation" in caplog.text
        assert payload not in caplog.text
        assert "999" not in caplog.text

    asyncio.run(scenario())


def test_unknown_schema_discards_whole_draft_and_new_state_has_no_old_data(tmp_path):
    async def scenario():
        storage = SQLiteStorage(tmp_path / "budget.db")
        await storage.set_data(KEY, {"amount": 999})
        with sqlite3.connect(storage.path) as connection:
            connection.execute("UPDATE fsm_sessions SET schema_version=999")
        await storage.set_state(KEY, Draft.amount)
        assert await storage.get_state(KEY) == "Draft:amount"
        assert await storage.get_data(KEY) == {}

    asyncio.run(scenario())


def test_invalid_input_leaves_valid_draft_untouched(tmp_path):
    async def scenario():
        storage = SQLiteStorage(tmp_path / "budget.db")
        await storage.set_state(KEY, Draft.amount)
        await storage.set_data(KEY, {"amount": 100})
        for invalid in ({"amount": float("nan")}, {"amount": object()}, {1: "bad key"}):
            with pytest.raises((TypeError, ValueError)):
                await storage.update_data(KEY, invalid)
        with pytest.raises(TypeError):
            await storage.set_state(KEY, 12)
        assert await storage.get_state(KEY) == "Draft:amount"
        assert await storage.get_data(KEY) == {"amount": 100}

    asyncio.run(scenario())


def test_expiry_removes_only_drafts_and_mutation_refreshes_lifetime(tmp_path, monkeypatch):
    async def scenario():
        clock = [1_000_000.0]
        monkeypatch.setattr(session_store.time, "time", lambda: clock[0])
        storage = SQLiteStorage(tmp_path / "budget.db", ttl_seconds=100)
        await storage.set_state(KEY, Draft.amount)
        await storage.set_data(KEY, {"amount": 123})
        with sqlite3.connect(storage.path) as connection:
            connection.execute("CREATE TABLE access_users(user_id INTEGER PRIMARY KEY)")
            connection.execute("INSERT INTO access_users VALUES(101)")
        clock[0] += 90
        assert await storage.get_state(KEY) == "Draft:amount"
        await storage.update_data(KEY, {"note": "refresh"})
        clock[0] += 99
        assert (await storage.get_data(KEY))["amount"] == 123
        # Reads do not keep an abandoned draft alive.
        clock[0] += 1
        assert await storage.get_state(KEY) is None
        assert await storage.get_data(KEY) == {}
        with sqlite3.connect(storage.path) as connection:
            assert connection.execute("SELECT user_id FROM access_users").fetchall() == [(101,)]

    asyncio.run(scenario())


def test_lazy_cleanup_is_bounded_but_addressed_expired_session_is_always_removed(tmp_path, monkeypatch):
    async def scenario():
        clock = [1_000_000.0]
        monkeypatch.setattr(session_store.time, "time", lambda: clock[0])
        storage = SQLiteStorage(tmp_path / "budget.db")
        await storage.init()
        with sqlite3.connect(storage.path) as connection:
            connection.executemany(
                "INSERT INTO fsm_sessions VALUES(?,1,'Draft:amount','{}',?)",
                [(json.dumps([123, i, i, None, None, "default"], separators=(",", ":")), clock[0] - 1)
                 for i in range(250)],
            )
        await storage.get_data(replace(KEY, chat_id=999, user_id=999))
        with sqlite3.connect(storage.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM fsm_sessions").fetchone()[0] == 150
        assert await storage.get_state(replace(KEY, chat_id=249, user_id=249)) is None
        with sqlite3.connect(storage.path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM fsm_sessions").fetchone()[0] == 149

    asyncio.run(scenario())
