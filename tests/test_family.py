import asyncio
import hashlib
import sqlite3
from types import SimpleNamespace

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from app.db import Database
from app import family


async def database(tmp_path):
    db = Database(tmp_path / "family.db")
    await db.init()
    await family.init_family(db)
    return db


def test_personal_money_stays_private_and_scope_persists(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        assert await family.active_budget_context(db, 101) == (101, 0)
        await db.add_transaction(101, "expense", "продукты", 100, occurred_on="2025-09-01")
        await db.add_transaction(202, "income", "зарплата", 200, occurred_on="2025-09-01")
        shared = await family.create_household(db, 101, display_name="Гриша")
        assert shared < 0
        assert (await db.summary(shared, "2025-09"))["expense"] == 0
        code = await family.create_invite(db, 101)
        preview = await family.preview_invite(db, 202, code)
        assert preview["household_id"] == -shared
        assert await family.active_budget_id(db, 202) == 202  # Preview does not join.
        assert await family.household_info(db, 202) is None
        assert await family.join_household(db, 202, code, "Оля") == shared
        await db.add_transaction(shared, "expense", "продукты", 300, occurred_on="2025-09-01")
        assert (await db.summary(101, "2025-09"))["expense"] == 100
        assert (await db.summary(202, "2025-09"))["income"] == 200
        assert (await db.summary(shared, "2025-09"))["expense"] == 300
        old = await family.active_budget_context(db, 202)
        await family.switch_budget(db, 202, False)
        assert await family.active_budget_id(db, 202) == 202
        await family.switch_budget(db, 202, True)
        current = await family.active_budget_context(db, 202)
        assert current[0] == old[0] and current[1] > old[1]
        await family.init_family(db)
        assert await family.active_budget_context(db, 202) == current
        with pytest.raises(family.FamilyError):
            await family.switch_budget(db, 303, True)
        with pytest.raises(family.FamilyError):
            await family.active_budget_id(db, shared)
    asyncio.run(scenario())


def test_invite_is_hashed_expiring_revocable_and_one_time(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await family.create_household(db, 101)
        old_code = await family.create_invite(db, 101, now=1_000)
        with sqlite3.connect(db.path) as conn:
            row = conn.execute("SELECT digest,expires_at FROM family_invites").fetchone()
            assert row == (hashlib.sha256(old_code.encode()).hexdigest(), 1_000 + family.INVITE_TTL_SECONDS)
            assert old_code not in str(conn.execute("SELECT * FROM family_invites").fetchall())
        await family.preview_invite(db, 202, old_code, now=1_001)
        with pytest.raises(family.FamilyError, match="истёк"):
            await family.join_household(db, 202, old_code, now=1_000 + family.INVITE_TTL_SECONDS)
        new_code = await family.create_invite(db, 101, now=2_000)
        with pytest.raises(family.FamilyError):
            await family.preview_invite(db, 202, old_code, now=2_001)
        await family.revoke_invites(db, 101)
        with pytest.raises(family.FamilyError):
            await family.preview_invite(db, 202, new_code, now=2_001)
        code = await family.create_invite(db, 101, now=3_000)
        results = await asyncio.gather(
            family.join_household(db, 202, code, now=3_001),
            family.join_household(db, 303, code, now=3_001),
            return_exceptions=True,
        )
        assert sum(isinstance(result, int) for result in results) == 1
        assert sum(isinstance(result, family.FamilyError) for result in results) == 1
        assert len((await family.household_info(db, 101))["members"]) == 2
    asyncio.run(scenario())


def test_membership_permission_capacity_and_duplicate_join(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        shared = await family.create_household(db, 101)
        other_shared = await family.create_household(db, 999)
        other_code = await family.create_invite(db, 999)
        for user in (202, 303, 404, 505):
            code = await family.create_invite(db, 101)
            await family.join_household(db, user, code)
        assert len((await family.household_info(db, 101))["members"]) == family.MAX_MEMBERS
        with pytest.raises(family.FamilyError, match="5 участников"):
            await family.create_invite(db, 101)
        with pytest.raises(family.FamilyError):
            await family.create_household(db, 202)
        with pytest.raises(family.FamilyError):
            await family.join_household(db, 202, other_code)
        assert await family.active_budget_id(db, 202) == shared
        assert await family.active_budget_id(db, 999) == other_shared
        with pytest.raises(family.FamilyError):
            await family.create_invite(db, 202)
        with pytest.raises(family.FamilyError):
            await family.revoke_invites(db, 202)
        with pytest.raises(family.FamilyError):
            await family.remove_member(db, 202, 303)
        with pytest.raises(family.FamilyError):
            await family.remove_member(db, 101, 999)
        with pytest.raises(family.FamilyError):
            await family.remove_member(db, 101, 101)
        with pytest.raises(family.FamilyError):
            await family.leave_household(db, 101)
    asyncio.run(scenario())


def test_removal_and_leave_revoke_access_and_old_invitations(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        shared = await family.create_household(db, 101)
        code = await family.create_invite(db, 101)
        await family.join_household(db, 202, code)
        await db.add_transaction(202, "expense", "продукты", 11, occurred_on="2025-09-01")
        shared_transaction = await db.add_transaction(shared, "expense", "продукты", 22, occurred_on="2025-09-01")
        pending_code = await family.create_invite(db, 101)
        old_context = await family.active_budget_context(db, 202)
        await family.remove_member(db, 101, 202)
        context = await family.active_budget_context(db, 202)
        assert context[0] == 202 and context[1] > old_context[1]
        assert await family.household_info(db, 202) is None
        assert await db.transaction(context[0], shared_transaction) is None
        assert not await db.delete_transaction(context[0], shared_transaction)
        assert (await db.summary(202, "2025-09"))["expense"] == 11
        assert await db.transaction(shared, shared_transaction)
        with pytest.raises(family.FamilyError):
            await family.switch_budget(db, 202, True)
        with pytest.raises(family.FamilyError):
            await family.join_household(db, 202, pending_code)
        new_code = await family.create_invite(db, 101)
        await family.join_household(db, 202, new_code)
        pending_code = await family.create_invite(db, 101)
        with pytest.raises(family.FamilyError):
            await family.leave_household(db, 202, expected_household_id=999)
        await family.leave_household(db, 202, expected_household_id=-shared)
        assert await family.active_budget_id(db, 202) == 202
        with pytest.raises(family.FamilyError):
            await family.preview_invite(db, 303, pending_code)
        assert await db.transaction(shared, shared_transaction)
    asyncio.run(scenario())


class FamilyChat:
    def __init__(self, db):
        self.db = db
        self.storage = MemoryStorage()
        self.messages = []
        self.alerts = []

    def state(self, user):
        return FSMContext(storage=self.storage, key=StorageKey(bot_id=1, chat_id=user, user_id=user))

    def message(self, user, text=""):
        async def answer(text, **kwargs):
            self.messages.append({"user": user, "text": text, **kwargs})
        return SimpleNamespace(text=text, chat=SimpleNamespace(id=user, type="private"),
                               from_user=SimpleNamespace(id=user, full_name=f"Участник {user}"), answer=answer)

    async def text(self, text, user=101):
        return await family.handle_message(self.message(user, text), self.state(user), self.db)

    async def tap(self, data, user=101):
        async def answer(text="", **kwargs):
            self.alerts.append({"user": user, "text": text, **kwargs})
        callback = SimpleNamespace(data=data, message=self.message(user),
                                   from_user=SimpleNamespace(id=user, full_name=f"Участник {user}"), answer=answer)
        return await family.handle_callback(callback, self.state(user), self.db)

    def button(self, label, user=101):
        for item in reversed(self.messages):
            if item["user"] != user:
                continue
            markup = item.get("reply_markup")
            for row in getattr(markup, "inline_keyboard", []):
                for button in row:
                    if label in button.text:
                        return button.callback_data
        raise AssertionError(f"Button not found: {label}")


def test_ui_requires_confirmation_and_rejects_stale_or_other_users_callbacks(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        chat = FamilyChat(db)
        await chat.tap("family::create_yes")
        await chat.tap("family:абв:create_yes")
        assert await family.household_info(db, 101) is None
        assert not await chat.text("продукты 850")
        assert await chat.text("/family")
        await chat.tap(chat.button("Создать общий"))
        assert await family.household_info(db, 101) is None
        create_yes = chat.button("Да, создать")
        await chat.tap(create_yes, user=202)
        assert await family.household_info(db, 202) is None
        await chat.tap(create_yes)
        shared = await family.active_budget_id(db, 101)
        assert shared < 0
        await chat.tap(create_yes)
        assert "устарела" in chat.alerts[-1]["text"]
        await chat.tap(chat.button("Создать код"))
        code_message = next(item["text"] for item in reversed(chat.messages) if "Одноразовый код приглашения:" in item["text"])
        code = code_message.splitlines()[1]
        assert all(item["user"] == 101 for item in chat.messages)  # No invitation was sent to someone else.
        await chat.text("👥 Семья", user=202)
        await chat.tap(chat.button("Ввести код", user=202), user=202)
        await chat.text(code, user=202)
        assert await family.household_info(db, 202) is None
        await chat.tap(chat.button("Да, вступить", user=202), user=202)
        assert await family.active_budget_id(db, 202) == shared
        await chat.tap(chat.button("Выйти из общего", user=202), user=202)
        assert await family.active_budget_id(db, 202) == shared
        leave_yes = chat.button("Да, выйти", user=202)
        await chat.text("/family", user=202)
        await chat.tap(leave_yes, user=202)
        assert "устарела" in chat.alerts[-1]["text"]
        assert await family.active_budget_id(db, 202) == shared
        await chat.text("/family")
        await chat.tap(chat.button("Исключить участника"))
        await chat.tap(chat.button("Участник 202"))
        assert await family.active_budget_id(db, 202) == shared
        await chat.tap(chat.button("Да, исключить"))
        assert await family.active_budget_id(db, 202) == 202
        await chat.tap(chat.button("Общий бюджет", user=202), user=202)
        assert await family.active_budget_id(db, 202) == 202
        await chat.storage.close()
    asyncio.run(scenario())


def test_stale_removal_confirmation_cannot_remove_rejoined_member(tmp_path):
    async def scenario():
        db = await database(tmp_path)
        await family.create_household(db, 101)
        await family.join_household(db, 202, await family.create_invite(db, 101))
        info = await family.household_info(db, 101)
        revision = next(m["revision"] for m in info["members"] if m["user_id"] == 202)
        await family.leave_household(db, 202)
        await family.join_household(db, 202, await family.create_invite(db, 101))
        with pytest.raises(family.FamilyError, match="изменились"):
            await family.remove_member(db, 101, 202, expected_revision=revision)
        assert await family.active_budget_id(db, 202) < 0
    asyncio.run(scenario())
