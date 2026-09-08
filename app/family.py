"""Explicit, isolated household budgets and their Telegram setup flow.

The existing money tables use ``user_id`` as a scope key: a positive Telegram
id is personal; ``-household_id`` is shared. Callers must resolve the scope for
the acting Telegram user before every operation, never trust a callback id.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager

import aiosqlite
from aiogram.fsm.state import State, StatesGroup

from .keyboards import CANCEL_MENU, MAIN_MENU, inline

MAX_MEMBERS = 5
INVITE_TTL_SECONDS = 24 * 60 * 60

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS family_households (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id INTEGER NOT NULL UNIQUE CHECK(owner_user_id > 0),
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS family_members (
    user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
    household_id INTEGER NOT NULL REFERENCES family_households(id),
    display_name TEXT NOT NULL,
    joined_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_family_members_household
ON family_members(household_id);
CREATE TABLE IF NOT EXISTS family_scopes (
    user_id INTEGER PRIMARY KEY CHECK(user_id > 0),
    active_household_id INTEGER REFERENCES family_households(id),
    revision INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS family_invites (
    digest TEXT PRIMARY KEY,
    household_id INTEGER NOT NULL REFERENCES family_households(id),
    created_by INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_by INTEGER,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1))
);
"""


class FamilyError(ValueError):
    """A safe, user-facing family permission or validation error."""


class FamilyForm(StatesGroup):
    code = State()
    confirm = State()


def _user_id(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise FamilyError("Некорректный пользователь.")
    return value


def _name(value: str, default: str = "Участник") -> str:
    return " ".join(str(value).split())[:60] or default


def _now(value: int | None) -> int:
    return int(time.time()) if value is None else int(value)


def _digest(code: str) -> str:
    code = code.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{20,128}", code):
        raise FamilyError("Код не найден, уже использован или истёк. Попросите новый код.")
    return hashlib.sha256(code.encode("ascii")).hexdigest()


@asynccontextmanager
async def _connection(db, *, write: bool = False):
    async with aiosqlite.connect(db.path, timeout=10) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        if write:
            await conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            if write:
                await conn.commit()
        except BaseException:
            if write:
                await conn.rollback()
            raise


async def init_family(db) -> None:
    async with _connection(db) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def _membership(conn, user_id: int):
    return await (await conn.execute(
        "SELECT h.*,m.display_name FROM family_members m "
        "JOIN family_households h ON h.id=m.household_id WHERE m.user_id=?",
        (_user_id(user_id),),
    )).fetchone()


async def household_info(db, telegram_user_id: int) -> dict | None:
    async with _connection(db) as conn:
        household = await _membership(conn, telegram_user_id)
        if not household:
            return None
        members = await (await conn.execute(
            "SELECT m.user_id,m.display_name,COALESCE(s.revision,0) AS revision FROM family_members m "
            "LEFT JOIN family_scopes s ON s.user_id=m.user_id WHERE m.household_id=? ORDER BY m.joined_at,m.user_id",
            (household["id"],),
        )).fetchall()
        return {**dict(household), "members": [dict(row) for row in members]}


async def active_budget_context(db, telegram_user_id: int) -> tuple[int, int]:
    """Resolve membership on every read; revision invalidates abandoned forms."""
    user_id = _user_id(telegram_user_id)
    async with _connection(db) as conn:
        row = await (await conn.execute(
            "SELECT s.revision,h.id FROM family_scopes s "
            "LEFT JOIN family_members m ON m.user_id=s.user_id AND m.household_id=s.active_household_id "
            "LEFT JOIN family_households h ON h.id=m.household_id WHERE s.user_id=?",
            (user_id,),
        )).fetchone()
    return (-row["id"] if row and row["id"] else user_id, row["revision"] if row else 0)


async def active_budget_id(db, telegram_user_id: int) -> int:
    return (await active_budget_context(db, telegram_user_id))[0]


async def _set_scope(conn, user_id: int, household_id: int | None) -> None:
    await conn.execute(
        "INSERT INTO family_scopes(user_id,active_household_id,revision) VALUES(?,?,1) "
        "ON CONFLICT(user_id) DO UPDATE SET active_household_id=excluded.active_household_id,revision=revision+1",
        (user_id, household_id),
    )


async def switch_budget(db, telegram_user_id: int, shared: bool) -> int:
    user_id = _user_id(telegram_user_id)
    async with _connection(db, write=True) as conn:
        household = await _membership(conn, user_id)
        if shared and not household:
            raise FamilyError("У вас нет доступа к общему бюджету. Откройте /family.")
        household_id = household["id"] if shared else None
        await _set_scope(conn, user_id, household_id)
        return -household_id if household_id else user_id


async def create_household(db, telegram_user_id: int, name: str = "Семейный бюджет", display_name: str = "") -> int:
    user_id = _user_id(telegram_user_id)
    async with _connection(db, write=True) as conn:
        if await _membership(conn, user_id):
            raise FamilyError("Вы уже состоите в общем бюджете.")
        cursor = await conn.execute(
            "INSERT INTO family_households(owner_user_id,name,created_at) VALUES(?,?,?)",
            (user_id, _name(name, "Семейный бюджет"), _now(None)),
        )
        household_id = cursor.lastrowid
        await conn.execute("INSERT INTO family_members VALUES(?,?,?,?)", (user_id, household_id, _name(display_name), _now(None)))
        await _set_scope(conn, user_id, household_id)
        return -household_id


async def _owner(conn, user_id: int):
    household = await _membership(conn, user_id)
    if not household or household["owner_user_id"] != user_id:
        raise FamilyError("Это действие доступно только создателю общего бюджета.")
    return household


async def create_invite(db, telegram_user_id: int, *, now: int | None = None) -> str:
    user_id = _user_id(telegram_user_id)
    code = "BF-" + secrets.token_urlsafe(18)
    async with _connection(db, write=True) as conn:
        household = await _owner(conn, user_id)
        count = await (await conn.execute("SELECT COUNT(*) FROM family_members WHERE household_id=?", (household["id"],))).fetchone()
        if count[0] >= MAX_MEMBERS:
            raise FamilyError("В общем бюджете уже 5 участников.")
        await conn.execute("UPDATE family_invites SET revoked=1 WHERE household_id=? AND used_by IS NULL", (household["id"],))
        await conn.execute(
            "INSERT INTO family_invites(digest,household_id,created_by,expires_at) VALUES(?,?,?,?)",
            (_digest(code), household["id"], user_id, _now(now) + INVITE_TTL_SECONDS),
        )
    return code


async def _valid_invite(conn, user_id: int, digest: str, now: int):
    if await _membership(conn, user_id):
        raise FamilyError("Вы уже состоите в общем бюджете. Сначала выйдите из него.")
    row = await (await conn.execute(
        "SELECT i.*,h.name,h.owner_user_id FROM family_invites i "
        "JOIN family_households h ON h.id=i.household_id "
        "JOIN family_members m ON m.user_id=h.owner_user_id AND m.household_id=h.id "
        "WHERE i.digest=? AND i.used_by IS NULL AND i.revoked=0 AND i.expires_at>? AND i.created_by=h.owner_user_id",
        (digest, now),
    )).fetchone()
    if not row:
        raise FamilyError("Код не найден, уже использован или истёк. Попросите новый код.")
    count = await (await conn.execute("SELECT COUNT(*) FROM family_members WHERE household_id=?", (row["household_id"],))).fetchone()
    if count[0] >= MAX_MEMBERS:
        raise FamilyError("В общем бюджете уже 5 участников.")
    return row


async def preview_invite(db, telegram_user_id: int, code: str, *, now: int | None = None) -> dict:
    user_id = _user_id(telegram_user_id)
    async with _connection(db) as conn:
        row = await _valid_invite(conn, user_id, _digest(code), _now(now))
        return {"household_id": row["household_id"], "name": row["name"], "expires_at": row["expires_at"]}


async def _join_digest(db, user_id: int, digest: str, display_name: str, now: int | None = None) -> int:
    _user_id(user_id)
    async with _connection(db, write=True) as conn:
        row = await _valid_invite(conn, user_id, digest, _now(now))
        household_id = row["household_id"]
        await conn.execute("INSERT INTO family_members VALUES(?,?,?,?)", (user_id, household_id, _name(display_name), _now(now)))
        await conn.execute("UPDATE family_invites SET used_by=? WHERE digest=?", (user_id, digest))
        await _set_scope(conn, user_id, household_id)
        return -household_id


async def join_household(db, telegram_user_id: int, code: str, display_name: str = "", *, now: int | None = None) -> int:
    """Only invoke after explicit acceptance; preview_invite never joins."""
    return await _join_digest(db, telegram_user_id, _digest(code), display_name, now)


async def revoke_invites(db, telegram_user_id: int) -> None:
    async with _connection(db, write=True) as conn:
        household = await _owner(conn, _user_id(telegram_user_id))
        await conn.execute("UPDATE family_invites SET revoked=1 WHERE household_id=? AND used_by IS NULL", (household["id"],))


async def _remove_member(conn, household_id: int, target_user_id: int) -> None:
    await conn.execute("DELETE FROM family_members WHERE household_id=? AND user_id=?", (household_id, target_user_id))
    await _set_scope(conn, target_user_id, None)
    # A removed participant cannot regain access using an earlier invitation.
    await conn.execute("UPDATE family_invites SET revoked=1 WHERE household_id=? AND used_by IS NULL", (household_id,))


async def remove_member(db, owner_user_id: int, target_user_id: int, *, expected_revision: int | None = None) -> None:
    _user_id(target_user_id)
    async with _connection(db, write=True) as conn:
        household = await _owner(conn, _user_id(owner_user_id))
        if target_user_id == owner_user_id:
            raise FamilyError("Создатель не может исключить себя.")
        target = await _membership(conn, target_user_id)
        if not target or target["id"] != household["id"]:
            raise FamilyError("Этот человек уже не состоит в вашем общем бюджете.")
        revision = await (await conn.execute("SELECT revision FROM family_scopes WHERE user_id=?", (target_user_id,))).fetchone()
        if expected_revision is not None and (not revision or revision[0] != expected_revision):
            raise FamilyError("Состав или состояние участников изменились. Откройте /family.")
        await _remove_member(conn, household["id"], target_user_id)


async def leave_household(db, telegram_user_id: int, expected_household_id: int | None = None) -> None:
    async with _connection(db, write=True) as conn:
        household = await _membership(conn, _user_id(telegram_user_id))
        if not household or (expected_household_id is not None and household["id"] != expected_household_id):
            raise FamilyError("Это подтверждение устарело. Откройте /family.")
        if household["owner_user_id"] == telegram_user_id:
            raise FamilyError("Создатель остаётся владельцем общего бюджета. Можно переключиться на личный.")
        await _remove_member(conn, household["id"], telegram_user_id)


async def _panel(state, *, pending: str | None = None, **data) -> str:
    await state.clear()
    token = secrets.token_hex(6)
    await state.update_data(family_token=token, family_pending=pending, **data)
    if pending:
        await state.set_state(FamilyForm.confirm)
    return f"family:{token}:"


async def show_family(message, state, db, telegram_user_id: int) -> None:
    household = await household_info(db, telegram_user_id)
    scope = await active_budget_id(db, telegram_user_id)
    prefix = await _panel(state)
    if not household:
        text = (
            "👥 Семейный бюджет\n\nСейчас выбран личный бюджет.\n"
            "Общий бюджет создаётся отдельно: личные записи в него не переносятся. "
            "До 5 участников могут видеть и менять общие доходы, расходы, лимиты, долги и цели.\n\n"
            "Вступление — только по одноразовому коду и вашему подтверждению. "
            "Для входа в бота приглашённый участник также должен быть разрешён в настройках доступа."
        )
        buttons = [[("Создать общий бюджет", prefix + "create")], [("Ввести код приглашения", prefix + "join")]]
    else:
        active = "общий" if scope < 0 else "личный"
        members = "\n".join(
            f"• {member['display_name']} · ID {member['user_id']}" + (" (создатель)" if member["user_id"] == household["owner_user_id"] else "")
            for member in household["members"]
        )
        text = (
            f"👥 {household['name']} · №{household['id']}\nАктивный бюджет: {active}\n\n"
            f"Участники ({len(household['members'])}/{MAX_MEMBERS}):\n{members}\n\n"
            "Общие записи видны всем участникам; личные — только вам. "
            "Новые операции, лимиты, долги и цели относятся к выбранному бюджету. "
            "После переключения незавершённый ввод нужно начать заново."
        )
        buttons = [[("🔒 Личный бюджет", prefix + "personal"), ("👥 Общий бюджет", prefix + "shared")]]
        if household["owner_user_id"] == telegram_user_id:
            buttons += [[("Создать код приглашения", prefix + "invite")], [("Отозвать коды", prefix + "revoke"), ("Исключить участника", prefix + "members")]]
        else:
            buttons += [[("Выйти из общего бюджета", prefix + "leave")]]
    buttons.append([("ℹ️ Как это работает", "guide:family")])
    await message.answer(text, reply_markup=inline(buttons))


async def handle_message(message, state, db) -> bool:
    text = (message.text or "").strip()
    command = text.split(" ", 1)[0].split("@", 1)[0]
    if not message.from_user or message.chat.type != "private":
        return False
    user_id = message.from_user.id
    if command == "/family" or text == "👥 Семья":
        await show_family(message, state, db, user_id)
        return True
    current = await state.get_state()
    if current not in (FamilyForm.code.state, FamilyForm.confirm.state):
        return False
    # Let global commands and menu buttons cancel this flow normally.
    if command.startswith("/") or text in ("❌ Отмена", "🏠 Меню") or text[:1] in ("➕", "➖", "📊", "📝", "🎯", "📁", "⚙", "🤝", "💬", "💳", "🧭") or text == "Ещё":
        return False
    if current == FamilyForm.confirm.state:
        await message.answer("Используйте кнопки подтверждения ниже или /cancel для отмены.")
        return True
    try:
        preview = await preview_invite(db, user_id, text)
        prefix = await _panel(state, pending="join", family_digest=_digest(text), family_household_id=preview["household_id"])
        await message.answer(
            f"Вступить в «{preview['name']}» (№{preview['household_id']})?\n\n"
            "Участники смогут видеть и менять общие записи. Ваши личные операции останутся личными. "
            "После вступления будет выбран общий бюджет.",
            reply_markup=inline([[("Да, вступить", prefix + "join_yes"), ("Отмена", prefix + "menu")]]),
        )
    except FamilyError as error:
        await message.answer(str(error) + "\nВведите новый код или /cancel.")
    return True


async def handle_callback(callback, state, db) -> bool:
    if not (callback.data or "").startswith("family:"):
        return False
    if not callback.message or callback.message.chat.type != "private" or not callback.from_user:
        await callback.answer("Откройте личный чат с ботом.")
        return True
    parts = callback.data.split(":")
    stored = await state.get_data()
    if (len(parts) < 3 or not re.fullmatch(r"[0-9a-f]{12}", parts[1])
            or not secrets.compare_digest(parts[1], str(stored.get("family_token", "")))):
        await callback.answer("Эта кнопка устарела. Откройте /family.")
        return True
    action, user_id, message = parts[2], callback.from_user.id, callback.message
    try:
        if action == "menu":
            await show_family(message, state, db, user_id)
        elif action in ("personal", "shared"):
            await switch_budget(db, user_id, action == "shared")
            await message.answer("Выбран общий бюджет." if action == "shared" else "Выбран личный бюджет.", reply_markup=MAIN_MENU)
            await show_family(message, state, db, user_id)
        elif action == "create":
            if await household_info(db, user_id):
                raise FamilyError("Вы уже состоите в общем бюджете.")
            prefix = await _panel(state, pending="create")
            await message.answer(
                "Создать отдельный общий бюджет?\n\nВы станете его создателем и сможете приглашать участников. "
                "Личные операции останутся личными. После создания будет выбран общий бюджет.",
                reply_markup=inline([[("Да, создать", prefix + "create_yes"), ("Отмена", prefix + "menu")]]),
            )
        elif action == "join":
            if await household_info(db, user_id):
                raise FamilyError("Вы уже состоите в общем бюджете.")
            await _panel(state)
            await state.set_state(FamilyForm.code)
            await message.answer("Пришлите одноразовый код от создателя бюджета.\nКод действует 24 часа; вступление нужно будет подтвердить.", reply_markup=CANCEL_MENU)
        elif action in ("create_yes", "join_yes", "leave_yes", "remove_yes"):
            expected = action.removesuffix("_yes")
            if stored.get("family_pending") != expected or await state.get_state() != FamilyForm.confirm.state:
                raise FamilyError("Подтверждение устарело. Откройте /family.")
            if action == "create_yes":
                await create_household(db, user_id, display_name=callback.from_user.full_name)
                text = "Общий бюджет создан. Личные записи сохранены отдельно."
            elif action == "join_yes":
                await _join_digest(db, user_id, stored["family_digest"], callback.from_user.full_name)
                text = "Вы вступили в общий бюджет. Теперь выбран общий бюджет."
            elif action == "leave_yes":
                await leave_household(db, user_id, stored["family_household_id"])
                text = "Вы вышли. Выбран личный бюджет; доступа к общим данным больше нет. Общие записи сохранены для остальных участников."
            else:
                await remove_member(db, user_id, stored["family_target"], expected_revision=stored["family_target_revision"])
                text = "Участник исключён и больше не имеет доступа к общим данным. Общие записи сохранены. Неиспользованные коды отозваны."
            await message.answer(text, reply_markup=MAIN_MENU)
            await show_family(message, state, db, user_id)
        elif action == "invite":
            code = await create_invite(db, user_id)
            await message.answer(
                f"Одноразовый код приглашения:\n{code}\n\nПередайте его нужному человеку лично. "
                "Он должен открыть /family → «Ввести код приглашения» и подтвердить вступление. "
                "Код действует 24 часа, предыдущие неиспользованные коды отозваны.\n\n"
                "Если ALLOWED_USER_IDS ограничивает доступ, добавьте туда Telegram ID приглашённого. "
                "Бот не отправляет приглашения другим людям."
            )
            await show_family(message, state, db, user_id)
        elif action == "revoke":
            await revoke_invites(db, user_id)
            await message.answer("Все неиспользованные коды отозваны.")
            await show_family(message, state, db, user_id)
        elif action in ("members", "remove", "leave"):
            household = await household_info(db, user_id)
            if not household:
                raise FamilyError("У вас больше нет доступа к общему бюджету.")
            if action == "leave":
                if household["owner_user_id"] == user_id:
                    raise FamilyError("Создатель может переключиться на личный бюджет.")
                prefix = await _panel(state, pending="leave", family_household_id=household["id"])
                await message.answer("Выйти из общего бюджета?\nВы потеряете доступ к нему. Общие записи останутся у остальных участников.", reply_markup=inline([[("Да, выйти", prefix + "leave_yes"), ("Отмена", prefix + "menu")]]))
            else:
                if household["owner_user_id"] != user_id:
                    raise FamilyError("Управлять участниками может только создатель.")
                members = [m for m in household["members"] if m["user_id"] != user_id]
                if action == "members":
                    prefix = await _panel(state)
                    buttons = [[(f"{m['display_name']} · {m['user_id']}", prefix + f"remove:{m['user_id']}")] for m in members]
                    buttons.append([("Назад", prefix + "menu")])
                    await message.answer("Кого исключить?" if members else "Других участников пока нет.", reply_markup=inline(buttons))
                else:
                    target = int(parts[3])
                    member = next((m for m in members if m["user_id"] == target), None)
                    if not member:
                        raise FamilyError("Участник не найден.")
                    prefix = await _panel(state, pending="remove", family_target=target, family_target_revision=member["revision"])
                    await message.answer(f"Исключить {member['display_name']} (ID {target})?\nЧеловек потеряет доступ, общие записи останутся. Неиспользованные приглашения будут отозваны.", reply_markup=inline([[("Да, исключить", prefix + "remove_yes"), ("Отмена", prefix + "menu")]]))
        else:
            raise FamilyError("Откройте актуальное меню: /family.")
        await callback.answer()
    except FamilyError as error:
        await callback.answer(str(error), show_alert=True)
    except (ValueError, IndexError, KeyError):
        await callback.answer("Эта кнопка устарела. Откройте /family.")
    return True
