"""Monthly obligations. A schedule never inserts expenses without confirmation."""
from __future__ import annotations

import calendar
import re
import secrets
from datetime import date, datetime, timezone

import aiosqlite
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .db import Database
from .finance import money
from .inputs import EXPENSE_CATEGORIES, category_name, month_label, parse_amount, shift_month, to_minor, today, valid_month
from .keyboards import CANCEL_MENU, MAIN_MENU, categories, inline


SCHEMA = """
CREATE TABLE IF NOT EXISTS recurring_schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    budget_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    amount_minor INTEGER NOT NULL CHECK(amount_minor > 0),
    day_of_month INTEGER NOT NULL CHECK(day_of_month BETWEEN 1 AND 31),
    start_month TEXT NOT NULL,
    disabled_on TEXT,
    create_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(budget_id, create_key)
);
CREATE INDEX IF NOT EXISTS idx_recurring_budget ON recurring_schedules(budget_id);
CREATE TABLE IF NOT EXISTS recurring_payments (
    schedule_id INTEGER NOT NULL REFERENCES recurring_schedules(id),
    due_on TEXT NOT NULL,
    paid_on TEXT NOT NULL,
    transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(id) ON DELETE CASCADE,
    actor_user_id INTEGER,
    PRIMARY KEY(schedule_id, due_on)
);
"""


class RecurringForm(StatesGroup):
    name = State()
    amount = State()
    category = State()
    day = State()
    month = State()
    confirm = State()


def due_date(month: str, day_of_month: int) -> date:
    """Preserve the requested day: January 31 -> February 28 -> March 31."""
    month = valid_month(month)
    if type(day_of_month) is not int or not 1 <= day_of_month <= 31:
        raise ValueError("День платежа — целое число от 1 до 31")
    year, number = map(int, month.split("-"))
    return date(year, number, min(day_of_month, calendar.monthrange(year, number)[1]))


def _name(value: str) -> str:
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise ValueError("Название — одна строка, до 80 символов")
    value = " ".join(value.split())
    if not value or len(value) > 80:
        raise ValueError("Название — от 1 до 80 символов")
    return value


async def init_recurring(db: Database) -> None:
    """Call once after Database.init(); existing schedules are preserved."""
    async with aiosqlite.connect(db.path) as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()


async def create_schedule(db: Database, budget_id: int, *, name: str, category: str,
                          amount_minor: int, day_of_month: int, start_month: str,
                          create_key: str | None = None) -> int:
    name, category, start_month = _name(name), category_name(category), valid_month(start_month)
    due_date(start_month, day_of_month)
    if type(amount_minor) is not int or not 0 < amount_minor <= 99_999_999_999:
        raise ValueError("Некорректная сумма платежа")
    key = create_key or secrets.token_hex(16)
    async with aiosqlite.connect(db.path) as conn:
        await conn.execute(
            "INSERT INTO recurring_schedules(budget_id,name,category,amount_minor,day_of_month,start_month,create_key,created_at) "
            "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(budget_id,create_key) DO NOTHING",
            (budget_id, name, category, amount_minor, day_of_month, start_month, key, datetime.now(timezone.utc).isoformat()),
        )
        row = await (await conn.execute("SELECT id FROM recurring_schedules WHERE budget_id=? AND create_key=?", (budget_id, key))).fetchone()
        await conn.commit()
        return row[0]


async def list_schedules(db: Database, budget_id: int) -> list[dict]:
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        rows = await (await conn.execute("SELECT * FROM recurring_schedules WHERE budget_id=? ORDER BY disabled_on IS NOT NULL,id", (budget_id,))).fetchall()
        return [dict(row) for row in rows]


def _valid_occurrence(schedule: dict, due_on: date) -> bool:
    month = due_on.strftime("%Y-%m")
    return (month >= schedule["start_month"]
            and due_on == due_date(month, schedule["day_of_month"])
            and (not schedule["disabled_on"] or due_on.isoformat() <= schedule["disabled_on"]))


async def obligations(db: Database, budget_id: int, as_of: date) -> dict:
    """All unpaid occurrences since start, through the end of as_of's month.

    Disabling only removes occurrences after disabled_on. A deleted linked
    expense reopens the occurrence, so a payment cannot remain silently paid.
    """
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        schedules = await (await conn.execute("SELECT * FROM recurring_schedules WHERE budget_id=?", (budget_id,))).fetchall()
        paid = {(row[0], row[1]) for row in await (await conn.execute(
            "SELECT p.schedule_id,p.due_on FROM recurring_payments p "
            "JOIN recurring_schedules s ON s.id=p.schedule_id "
            "JOIN transactions t ON t.id=p.transaction_id AND t.user_id=s.budget_id "
            "WHERE s.budget_id=?", (budget_id,),
        )).fetchall()}
    last_month = as_of.strftime("%Y-%m")
    items = []
    for row in schedules:
        schedule = dict(row)
        month = schedule["start_month"]
        while month <= last_month:
            due = due_date(month, schedule["day_of_month"])
            if schedule["disabled_on"] and due.isoformat() > schedule["disabled_on"]:
                break
            if (schedule["id"], due.isoformat()) not in paid:
                items.append({
                    "schedule_id": schedule["id"], "name": schedule["name"], "category": schedule["category"],
                    "amount_minor": schedule["amount_minor"], "due_on": due.isoformat(),
                    "status": "overdue" if due < as_of else "today" if due == as_of else "upcoming",
                })
            if month == last_month:
                break
            month = shift_month(month, 1)
    items.sort(key=lambda item: (item["due_on"], item["schedule_id"]))
    return {"total_minor": sum(item["amount_minor"] for item in items), "items": items,
            "overdue_minor": sum(item["amount_minor"] for item in items if item["status"] == "overdue")}


async def mark_paid(db: Database, budget_id: int, schedule_id: int, due_on: str,
                    *, actor_user_id: int | None = None, actor_name: str = "") -> dict:
    """Atomically record today's actual expense and its occurrence; safe to retry."""
    due = date.fromisoformat(due_on)
    due_on = due.isoformat()
    current = today(db.timezone)
    if due > due_date(current.strftime("%Y-%m"), 31):
        raise ValueError("Можно отметить платёж только до конца текущего месяца")
    async with aiosqlite.connect(db.path, timeout=30) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            row = await (await conn.execute("SELECT * FROM recurring_schedules WHERE budget_id=? AND id=?", (budget_id, schedule_id))).fetchone()
            if not row or not _valid_occurrence(dict(row), due):
                raise ValueError("Платёж не найден или расписание для этой даты отключено")
            existing = await (await conn.execute(
                "SELECT p.transaction_id FROM recurring_payments p JOIN transactions t ON t.id=p.transaction_id "
                "WHERE p.schedule_id=? AND p.due_on=?", (schedule_id, due_on),
            )).fetchone()
            if existing:
                await conn.commit()
                return {"created": False, "transaction_id": existing[0]}
            # Old database clients may delete transactions with foreign_keys off.
            await conn.execute("DELETE FROM recurring_payments WHERE schedule_id=? AND due_on=?", (schedule_id, due_on))
            actor = actor_user_id if actor_user_id is not None else budget_id if budget_id > 0 else None
            columns = {item[1] for item in await (await conn.execute("PRAGMA table_info(transactions)")).fetchall()}
            fields = ["user_id", "kind", "category", "amount", "note", "amount_minor", "occurred_on", "created_at"]
            values = [budget_id, "expense", row["category"], row["amount_minor"] / 100,
                      f"{row['name']} · платёж за {due.strftime('%d.%m.%Y')}", row["amount_minor"],
                      current.isoformat(), datetime.now(timezone.utc).isoformat(timespec="seconds")]
            if "actor_user_id" in columns:
                fields.append("actor_user_id")
                values.append(actor)
            if "actor_name" in columns:
                fields.append("actor_name")
                values.append(" ".join(actor_name.split())[:80])
            cursor = await conn.execute(f"INSERT INTO transactions({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", values)
            transaction_id = cursor.lastrowid
            await conn.execute("INSERT INTO recurring_payments(schedule_id,due_on,paid_on,transaction_id,actor_user_id) VALUES(?,?,?,?,?)",
                               (schedule_id, due_on, current.isoformat(), transaction_id, actor))
            await conn.commit()
            return {"created": True, "transaction_id": transaction_id}
        except BaseException:
            await conn.rollback()
            raise


async def linked_payment(db: Database, budget_id: int, transaction_id: int) -> bool:
    """Whether an expense belongs to a paid occurrence within this budget."""
    async with aiosqlite.connect(db.path) as conn:
        row = await (await conn.execute(
            "SELECT 1 FROM recurring_payments p JOIN recurring_schedules s ON s.id=p.schedule_id "
            "JOIN transactions t ON t.id=p.transaction_id AND t.user_id=s.budget_id "
            "WHERE s.budget_id=? AND p.transaction_id=?", (budget_id, transaction_id),
        )).fetchone()
        return bool(row)


async def disable_schedule(db: Database, budget_id: int, schedule_id: int) -> bool:
    async with aiosqlite.connect(db.path) as conn:
        cursor = await conn.execute(
            "UPDATE recurring_schedules SET disabled_on=? WHERE budget_id=? AND id=? AND disabled_on IS NULL",
            (today(db.timezone).isoformat(), budget_id, schedule_id),
        )
        await conn.commit()
        return bool(cursor.rowcount)


async def _show(message: Message, db: Database, budget_id: int, page: int = 0) -> None:
    from .family import active_budget_context
    _, revision = await active_budget_context(db, message.chat.id)
    current = today(db.timezone)
    report = await obligations(db, budget_id, current)
    count = len(report["items"])
    page = min(max(page, 0), max((count - 1) // 8, 0))
    rows = report["items"][page * 8:(page + 1) * 8]
    text = [f"🗓 {'Семейные' if budget_id < 0 else 'Личные'} платежи · {current.strftime('%d.%m.%Y')}",
            f"Не оплачено до конца месяца: {money(report['total_minor'] / 100)}",
            "Включены неоплаченные платежи прошлых месяцев."]
    if not rows:
        text.append("\nНеоплаченных платежей пока нет.")
    buttons = []
    for item in rows:
        label = {"overdue": "🔴 срок прошёл", "today": "🟠 сегодня", "upcoming": "🗓 предстоит"}[item["status"]]
        text.append(f"\n{date.fromisoformat(item['due_on']).strftime('%d.%m.%Y')} · {item['name']}\n{money(item['amount_minor'] / 100)} · {label}")
        buttons.append([(f"✅ Оплачено · {item['name'][:26]} · {item['due_on'][8:10]}.{item['due_on'][5:7]}",
                         f"rec:pay:{item['schedule_id']}:{item['due_on']}")])
    navigation = []
    if page:
        navigation.append(("‹ Назад", f"rec:page:{page - 1}"))
    if (page + 1) * 8 < count:
        navigation.append(("Далее ›", f"rec:page:{page + 1}"))
    if navigation:
        buttons.append(navigation)
        text.append(f"\nСтраница {page + 1} из {(count + 7) // 8}")
    buttons.extend([[("➕ Добавить платёж", f"rec:add:{budget_id}:{revision}")], [("⚙️ Мои расписания", "rec:schedules:0")]])
    text.append("\nРасход появится только после «Оплачено» и подтверждения, датой фактической отметки. Напоминания не отправляются.")
    await message.answer("\n".join(text), reply_markup=inline(buttons))


async def _show_schedules(message: Message, db: Database, budget_id: int, page: int) -> None:
    from .family import active_budget_context
    _, revision = await active_budget_context(db, message.chat.id)
    schedules = await list_schedules(db, budget_id)
    page = min(max(page, 0), max((len(schedules) - 1) // 8, 0))
    lines = ["⚙️ Ежемесячные платежи"]
    buttons = []
    for row in schedules[page * 8:(page + 1) * 8]:
        status = "отключён" if row["disabled_on"] else "активен"
        lines.append(f"\n{row['name']} · {money(row['amount_minor'] / 100)}\nКаждое {row['day_of_month']}-е число · {status}")
        if not row["disabled_on"]:
            buttons.append([(f"Отключить · {row['name'][:35]}", f"rec:disable:{row['id']}")])
    if not schedules:
        lines.append("\nРасписаний пока нет.")
    navigation = []
    if page:
        navigation.append(("‹ Назад", f"rec:schedules:{page - 1}"))
    if (page + 1) * 8 < len(schedules):
        navigation.append(("Далее ›", f"rec:schedules:{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.extend([[("➕ Добавить платёж", f"rec:add:{budget_id}:{revision}")], [("К платежам", "rec:home")]])
    await message.answer("\n".join(lines), reply_markup=inline(buttons))


async def _ask_month(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    draft = data["rec_draft"]
    await state.set_state(RecurringForm.month)
    await message.answer("С какого месяца учитывать платёж?\nВведите ММ.ГГГГ или выберите кнопку. Если срок в первом месяце уже прошёл, он появится как неоплаченный.",
                         reply_markup=inline([[("С текущего месяца", f"rec:month:{draft['token']}:current")],
                                               [("Со следующего месяца", f"rec:month:{draft['token']}:next")]]))


async def _confirm_create(message: Message, state: FSMContext) -> None:
    draft = (await state.get_data())["rec_draft"]
    first = due_date(draft["start_month"], draft["day_of_month"])
    await state.set_state(RecurringForm.confirm)
    await message.answer(
        f"Добавить ежемесячный платёж?\n\n{draft['name']} · {money(draft['amount_minor'] / 100)}\n"
        f"Категория: {draft['category']}\nКаждое {draft['day_of_month']}-е число, начиная с {month_label(draft['start_month'])}.\n"
        f"Первый срок: {first.strftime('%d.%m.%Y')}.\nЕсли такого числа нет, срок — последний день месяца.\n\nСамо расписание не списывает деньги.",
        reply_markup=inline([[("Сохранить расписание", f"rec:save:{draft['token']}"), ("Отмена", "rec:cancel")]]),
    )


async def handle_message(message: Message, state: FSMContext, db: Database, budget_id: int) -> bool:
    text = (message.text or "").strip()
    current = await state.get_state()
    own_state = bool(current and current.startswith("RecurringForm:"))
    command = text.split(" ", 1)[0].split("@", 1)[0]
    if text == "🗓 Платежи" or command == "/payments":
        await state.clear()
        await _show(message, db, budget_id)
        return True
    if not own_state:
        return False
    if command in ("/cancel", "/menu", "/start", "/help") or text in ("❌ Отмена", "🏠 Меню"):
        return False  # Global navigation is owned by the parent handler.
    data = await state.get_data()
    draft = data.get("rec_draft", {})
    if data.get("rec_budget_id") != budget_id:
        await state.clear()
        await message.answer("Бюджет сменился. Откройте платежи снова: /payments", reply_markup=MAIN_MENU)
        return True
    try:
        if current == RecurringForm.name.state:
            draft["name"] = _name(text)
            await state.update_data(rec_draft=draft)
            await state.set_state(RecurringForm.amount)
            await message.answer("Сумма ежемесячного платежа в рублях? Например: 61 000 или 499,90.")
        elif current == RecurringForm.amount.state:
            draft["amount_minor"] = to_minor(parse_amount(text))
            await state.update_data(rec_draft=draft)
            await state.set_state(RecurringForm.category)
            await message.answer("Выберите категорию расхода или напишите свою:", reply_markup=categories(EXPENSE_CATEGORIES, f"rec:category:{draft['token']}"))
        elif current == RecurringForm.category.state:
            draft["category"] = category_name(text)
            await state.update_data(rec_draft=draft)
            await state.set_state(RecurringForm.day)
            await message.answer("Какого числа платить каждый месяц? Введите число от 1 до 31. Если такого числа нет, выберем последний день месяца.")
        elif current == RecurringForm.day.state:
            if not re.fullmatch(r"[0-9]{1,2}", text) or not 1 <= int(text) <= 31:
                raise ValueError("Введите целое число от 1 до 31")
            draft["day_of_month"] = int(text)
            await state.update_data(rec_draft=draft)
            await _ask_month(message, state)
        elif current == RecurringForm.month.state:
            if not re.fullmatch(r"[0-9]{2}\.[0-9]{4}", text):
                raise ValueError("Введите месяц в формате ММ.ГГГГ, например 09.2026")
            month, year = text.split(".")
            draft["start_month"] = valid_month(f"{year}-{month}")
            await state.update_data(rec_draft=draft)
            await _confirm_create(message, state)
        else:
            await message.answer("Подтвердите действие кнопкой или отмените ввод: /cancel")
    except (ValueError, KeyError) as exc:
        await message.answer(str(exc) if isinstance(exc, ValueError) else "Ввод устарел. Начните снова: /payments")
    return True


async def handle_callback(callback: CallbackQuery, state: FSMContext, db: Database, budget_id: int) -> bool:
    raw = callback.data or ""
    if not raw.startswith("rec:"):
        return False
    if not isinstance(callback.message, Message):
        await callback.answer("Откройте платежи снова: /payments")
        return True
    message, parts = callback.message, raw.split(":")
    current = await state.get_state()
    if current and not current.startswith("RecurringForm:"):
        await callback.answer("Сначала завершите текущий ввод или нажмите /cancel")
        return True
    try:
        action = parts[1]
        stored = await state.get_data()
        draft = stored.get("rec_draft", {})
        if action in ("category", "month", "save", "confirm"):
            if stored.get("rec_budget_id") != budget_id or len(parts) < 3 or parts[2] != draft.get("token"):
                await callback.answer("Подтверждение устарело. Откройте /payments")
                return True
        if action in ("home", "cancel", "page"):
            await state.clear()
            await _show(message, db, budget_id, int(parts[2]) if action == "page" else 0)
        elif action == "schedules":
            await state.clear()
            await _show_schedules(message, db, budget_id, int(parts[2]))
        elif action == "add":
            from .family import active_budget_context
            active = await active_budget_context(db, callback.from_user.id)
            if len(parts) != 4 or active != (int(parts[2]),int(parts[3])) or active[0] != budget_id:
                await callback.answer('Эта кнопка из другого бюджета. Откройте /payments заново.')
                return True
            await state.clear()
            await state.update_data(rec_budget_id=budget_id, rec_draft={"token": secrets.token_hex(8), "action": "create"})
            await state.set_state(RecurringForm.name)
            await message.answer("Как называется ежемесячный платёж? Например: ипотека, кредит или подписка.\nДо 80 символов.", reply_markup=CANCEL_MENU)
        elif action == "category" and current == RecurringForm.category.state:
            index = int(parts[3])
            if not 0 <= index < len(EXPENSE_CATEGORIES):
                raise ValueError
            draft["category"] = category_name(EXPENSE_CATEGORIES[index])
            await state.update_data(rec_draft=draft)
            await state.set_state(RecurringForm.day)
            await message.answer("Какого числа платить? Введите число от 1 до 31. Если числа нет, срок — последний день месяца.")
        elif action == "month" and current == RecurringForm.month.state:
            if parts[3] not in ("current", "next"):
                raise ValueError
            draft["start_month"] = shift_month(today(db.timezone).strftime("%Y-%m"), int(parts[3] == "next"))
            await state.update_data(rec_draft=draft)
            await _confirm_create(message, state)
        elif action == "save" and current == RecurringForm.confirm.state and draft.get("action") == "create":
            await create_schedule(db, budget_id, **{key: draft[key] for key in ("name", "category", "amount_minor", "day_of_month", "start_month")}, create_key=draft["token"])
            await state.clear()
            await message.answer("✅ Расписание сохранено.", reply_markup=MAIN_MENU)
            await _show(message, db, budget_id)
        elif action in ("pay", "disable"):
            schedule_id = int(parts[2])
            schedule = next((item for item in await list_schedules(db, budget_id) if item["id"] == schedule_id), None)
            if not schedule:
                raise ValueError("Платёж не найден")
            token = secrets.token_hex(8)
            pending = {"action": action, "token": token, "schedule_id": schedule_id}
            if action == "pay":
                due_on = date.fromisoformat(parts[3]).isoformat()
                report = await obligations(db, budget_id, today(db.timezone))
                if not any(item["schedule_id"] == schedule_id and item["due_on"] == due_on for item in report["items"]):
                    await callback.answer("Платёж уже отмечен или для этой даты недоступен")
                    return True
                pending["due_on"] = due_on
                text = (f"Вы действительно оплатили «{schedule['name']}»?\n"
                        f"Сумма: {money(schedule['amount_minor'] / 100)}\nСрок: {date.fromisoformat(due_on).strftime('%d.%m.%Y')}\n\n"
                        f"Добавлю расход за {today(db.timezone).strftime('%d.%m.%Y')}. Если вы уже внесли его вручную, отмените действие, чтобы не задвоить расход.")
                label = "Да, оплачено"
            else:
                if schedule["disabled_on"]:
                    await callback.answer("Расписание уже отключено")
                    return True
                text = (f"Отключить «{schedule['name']}»?\nБудущие платежи после сегодняшней даты исчезнут. "
                        "Уже наступившие неоплаченные платежи и сохранённые расходы останутся.")
                label = "Да, отключить"
            await state.clear()
            await state.update_data(rec_budget_id=budget_id, rec_draft=pending)
            await state.set_state(RecurringForm.confirm)
            await message.answer(text, reply_markup=inline([[(label, f"rec:confirm:{token}"), ("Отмена", "rec:cancel")]]))
        elif action == "confirm" and current == RecurringForm.confirm.state:
            if draft.get("action") == "pay":
                result = await mark_paid(db, budget_id, draft["schedule_id"], draft["due_on"],
                                         actor_user_id=callback.from_user.id, actor_name=callback.from_user.first_name)
                text = "✅ Платёж отмечен, расход сохранён." if result["created"] else "Этот платёж уже отмечен. Повторный расход не создан."
            elif draft.get("action") == "disable":
                await disable_schedule(db, budget_id, draft["schedule_id"])
                text = "Расписание отключено. Наступившие неоплаченные платежи сохранены."
            else:
                raise ValueError
            await state.clear()
            await message.answer(text, reply_markup=MAIN_MENU)
            await _show(message, db, budget_id)
        else:
            await callback.answer("Кнопка устарела. Откройте /payments")
            return True
        await callback.answer()
    except (ValueError, IndexError, KeyError):
        await callback.answer("Кнопка устарела или платёж недоступен. Откройте /payments")
    return True
