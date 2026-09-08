from __future__ import annotations

import asyncio
import calendar
import re
import secrets
import weakref
from datetime import timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, Message

from .config import load_settings
from .db import Database
from .export import export_xlsx
from .finance import affordability, allocation, credit_card_advice, money
from .inputs import (AMOUNT, EXPENSE_CATEGORIES, INCOME_CATEGORIES, category_name,
                     month_label, parse_amount, parse_date, quick_entry, shift_month, today, valid_month)
from .keyboards import CANCEL_MENU, EXTRA_MENU, MAIN_MENU, categories, inline
from . import family, recurring
from .insights import comparison, limit_status, search_transactions

router = Router()
db: Database
allowed_user_ids: frozenset[int] = frozenset()


class Form(StatesGroup):
    amount = State()
    category = State()
    confirm = State()
    date = State()
    note = State()
    budget_category = State()
    budget_amount = State()
    opening = State()
    month = State()
    debt = State()
    goal = State()
    afford = State()
    credit = State()
    search = State()


class AccessMiddleware(BaseMiddleware):
    # One polling process per token: serialize access changes and money actions
    # across family members, including reads/exports. Per-user FSM locks alone
    # would permit an excluded member's in-flight handler to keep writing.
    _locks = weakref.WeakKeyDictionary()

    async def __call__(self, handler, event, data):
        loop = asyncio.get_running_loop()
        lock = self._locks.setdefault(loop, asyncio.Lock())
        async with lock:
            return await self._handle(handler, event, data)

    async def _handle(self, handler, event, data):
        user = event.from_user
        message = event.message if isinstance(event, CallbackQuery) else event
        if message and message.chat.type == 'private' and isinstance(event, Message) and user:
            command = (event.text or '').split('@', 1)[0].split(' ', 1)[0]
            if command == '/whoami':
                await event.answer(f'Ваш Telegram ID: {user.id}\nДобавьте этот номер в ALLOWED_USER_IDS при настройке бота.')
                return
            if command == '/start' and not allowed_user_ids:
                await event.answer('Бот готов к настройке. Отправьте /whoami, добавьте свой ID в ALLOWED_USER_IDS и перезапустите бота.')
                return
        if not user or user.id not in allowed_user_ids:
            await event.answer("У вас нет доступа к этому боту.")
            return
        if not message or message.chat.type != "private":
            await event.answer("Для личного бюджета откройте личный чат с ботом.")
            return
        state = data.get('state')
        if state is None:
            return await handler(event, data)
        budget_id, revision = await family.active_budget_context(db, user.id)
        scope = f'{budget_id}:{revision}'
        stored = await state.get_data()
        if stored.get('_scope') not in (None, scope):
            await state.clear()
            if isinstance(event, CallbackQuery) and not (event.data or '').startswith('family:'):
                await event.answer('Бюджет или доступ изменился. Откройте /menu заново.')
                return
        try:
            return await handler(event, data)
        finally:
            # A family action intentionally changes the active context and
            # generates a fresh panel. All other forms retain their origin.
            if isinstance(event, CallbackQuery) and (event.data or '').startswith('family:'):
                current_budget, current_revision = await family.active_budget_context(db, user.id)
                scope = f'{current_budget}:{current_revision}'
            await state.update_data(_scope=scope)


router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


def catalog(kind: str) -> tuple[str, ...]:
    return INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES


async def scoped_keyboard(message: Message, rows):
    budget_id, revision = await family.active_budget_context(db, message.chat.id)
    return inline([[(label,f'scope:{budget_id}:{revision}:{action}') for label,action in row] for row in rows])


def transaction_text(data: dict) -> str:
    kind = "Доход" if data["kind"] == "income" else "Расход"
    day = ".".join(reversed(data["occurred_on"].split("-")))
    note = f"\nКомментарий: {data['note']}" if data.get("note") else ""
    author = f"\nАвтор: {data['actor_name']}" if data.get('actor_name') else ''
    return f"{kind}: {money(data['amount'])}\nКатегория: {data['category']}\nДата: {day}{note}{author}"


async def new_draft(message: Message, state: FSMContext, kind: str | None = None, entry: dict | None = None) -> None:
    await state.clear()
    draft = {"kind": kind or "expense", "occurred_on": today(db.timezone).isoformat(), "note": "",
             "source_message_id": message.message_id, "token": secrets.token_hex(4)}
    draft.update(entry or {})
    await state.update_data(draft=draft)
    if entry:
        await confirm(message, state)
    else:
        await state.set_state(Form.amount)
        await message.answer("Введите сумму в рублях. Например: 850 или 1 250,50.\nМожно сразу: 850 продукты", reply_markup=CANCEL_MENU)


async def choose_category(message: Message, state: FSMContext) -> None:
    draft = (await state.get_data())["draft"]
    await state.set_state(Form.category)
    await message.answer("Выберите категорию или напишите свою:", reply_markup=categories(catalog(draft["kind"]), f"cat:{draft['token']}"))


async def confirm(message: Message, state: FSMContext) -> None:
    draft = (await state.get_data())["draft"]
    draft['token'] = secrets.token_hex(4)
    await state.update_data(draft=draft)
    await state.set_state(Form.confirm)
    prefix = f"draft:{draft['token']}:"
    await message.answer(transaction_text(draft) + "\n\nПроверьте запись:", reply_markup=inline([
        [("✅ Сохранить", prefix + "save")],
        [("Сумма", prefix + "amount"), ("Категория", prefix + "category")],
        [("Дата", prefix + "date"), ("Комментарий", prefix + "note")],
        [("Сменить доход / расход", prefix + "kind"), ("Отмена", prefix + "cancel")],
    ]))


async def show_summary(message: Message, user_id: int) -> None:
    month = await db.selected_month(user_id)
    data = await db.summary(user_id, month)
    debt, goals_remaining = metrics(data)
    cats = "\n".join(f"• {x['category']}: {money(x['total'])}" for x in data["categories"]) or "Пока нет расходов."
    month_buttons = []
    for label, delta in (("‹ Месяц", -1), ("Месяц ›", 1)):
        try:
            target = valid_month(shift_month(month, delta))
        except ValueError:
            continue
        month_buttons.append((label, f"month:{target}"))
    upcoming = ''
    current_day = today(db.timezone)
    if month == current_day.strftime('%Y-%m'):
        planned = await recurring.obligations(db, user_id, current_day)
        free = round(data['balance'] - planned['total_minor']/100, 2)
        remaining_days = calendar.monthrange(current_day.year, current_day.month)[1]-current_day.day+1
        daily = max(0, int(round(free*100)) // remaining_days)/100
        upcoming = (f"\n\nПредстоящие и неоплаченные платежи: {money(planned['total_minor']/100)}"
                    f"\nОстаток после этих платежей: {money(free)}"
                    f"\nОриентир в день до конца месяца: {money(daily)}"
                    '\nОриентир по внесённым данным, без будущих доходов и резерва на цели.')
    scope_label = 'Семейный бюджет' if user_id < 0 else 'Личный бюджет'
    await message.answer(
        f"📊 {scope_label} · {month_label(month)}\n\nДоходы: {money(data['income'])}\nРасходы: {money(data['expense'])}"
        f"\nРазница за месяц: {money(data['net'])}\nРасчётный остаток: {money(data['balance'])}"
        f"\n\nОстаток включает начальные {money(data['opening'])} и все операции до конца выбранного месяца."
        f"\n\nТекущие долги: {money(debt)}\nНа цели осталось: {money(goals_remaining)}"
        f"{upcoming}\n\nТоп расходов:\n{cats}", reply_markup=inline([
            month_buttons,
            [("Текущий месяц", "current"), ("Выбрать месяц", "pick_month")],
            [("📈 Сравнить расходы", "analytics")],
        ]))


async def show_history(message: Message, user_id: int, month: str, offset: int = 0) -> None:
    rows = await db.transactions(user_id, month, offset=offset, limit=9)
    buttons = [[(f"{r['occurred_on'][8:]} · {'+' if r['kind'] == 'income' else '−'}{money(r['amount'])} · {r['category']}", f"tx:view:{r['id']}")] for r in rows[:8]]
    navigation = []
    if offset:
        navigation.append(("‹ Назад", f"history:{month}:{max(0, offset - 8)}"))
    if len(rows) > 8:
        navigation.append(("Далее ›", f"history:{month}:{offset + 8}"))
    if navigation:
        buttons.append(navigation)
    await message.answer(f"📝 {month_label(month)}\n" + ("Нажмите на операцию, чтобы изменить или удалить её." if rows else "Операций пока нет."), reply_markup=inline(buttons) if buttons else MAIN_MENU)


async def show_limits(message: Message, user_id: int) -> None:
    month = await db.selected_month(user_id)
    report = await db.budget_report(user_id, month)
    lines = []
    for row in report:
        if row["limit"] is None:
            lines.append(f"• {row['category']}: {money(row['spent'])} · лимит не задан")
        else:
            left = round(row["limit"] - row["spent"], 2)
            tail = f"осталось {money(left)}" if left >= 0 else f"перерасход {money(-left)}"
            lines.append(f"{'🟢' if left >= 0 else '🔴'} {row['category']}: {money(row['spent'])} / {money(row['limit'])}, {tail}")
    chunks = ["\n".join(lines[i:i+15]) for i in range(0, len(lines), 15)] or ["Лимиты пока не заданы."]
    for part in chunks:
        await message.answer(f"🎯 {month_label(month)}\n\n{part}")
    await message.answer("Лимиты сохраняются отдельно для каждого месяца.", reply_markup=await scoped_keyboard(message, [[("Задать / изменить лимит", f"budget:{month}")]]))


async def send_export(message: Message, user_id: int) -> None:
    month = await db.selected_month(user_id)
    rows, offset = [], 0
    while True:
        page = await db.transactions(user_id, month, offset=offset, limit=1000)
        rows.extend(page)
        if len(page) < 1000:
            break
        offset += len(page)
    content = await asyncio.to_thread(export_xlsx, await db.summary(user_id, month), rows, await db.budget_report(user_id, month))
    await message.answer_document(BufferedInputFile(content, filename=f"budget_{month}.xlsx"), caption=f"Бюджет · {month_label(month)}. Снимок данных на момент выгрузки.")


async def show_analytics(message: Message, budget_id: int) -> None:
    data = await comparison(db, budget_id, await db.selected_month(budget_id), today(db.timezone))
    if not data['days']:
        await message.answer('Для будущего месяца сравнение ещё недоступно.', reply_markup=MAIN_MENU)
        return
    change = data['change_minor']/100
    detail = 'Расходы не изменились.' if not change else f"{'Больше' if change > 0 else 'Меньше'} на {money(abs(change))}"
    if data['change_percent'] is not None and change:
        detail += f" ({abs(data['change_percent']):.1f}%)."
    elif data['previous_minor'] == 0:
        detail += '\nВ прошлом периоде нет расходов; процент не рассчитываю.'
    await message.answer(
        f"📈 Сравнение расходов за первые {data['days']} дн.\n\n"
        f"{month_label(data['month'])}: {money(data['current_minor']/100)}\n"
        f"{month_label(data['previous'])}: {money(data['previous_minor']/100)}\n\n{detail}"
        '\n\nСравниваются одинаковые по длительности отрезки по внесённым операциям.', reply_markup=MAIN_MENU)


async def show_search(message: Message, state: FSMContext, budget_id: int, query: str, offset: int = 0) -> None:
    result = await search_transactions(db, budget_id, query, offset)
    token = secrets.token_hex(4)
    await state.update_data(search_query=query, search_token=token)
    buttons = [[(f"{r['occurred_on']} · {money(r['amount'])} · {r['category']}", f"tx:view:{r['id']}")] for r in result['rows']]
    nav = []
    if offset:
        nav.append(('‹ Назад', f'search:{token}:{max(0,offset-8)}'))
    if offset+8 < result['count']:
        nav.append(('Далее ›', f'search:{token}:{offset+8}'))
    if nav:
        buttons.append(nav)
    await message.answer(
        f"🔎 «{query}» · все месяцы\nНайдено: {result['count']}\n"
        f"Доходы: {money(result['income_minor']/100)}\nРасходы: {money(result['expense_minor']/100)}",
        reply_markup=inline(buttons) if buttons else MAIN_MENU)


def metrics(data: dict) -> tuple[float, float]:
    return data["debts"].get("i_owe", 0), sum(max(g["target"] - g["saved"], 0) for g in data["goals"])


@router.message()
async def handle_message(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    user_id = await family.active_budget_id(db, message.from_user.id)
    command = text.split("@", 1)[0].split(" ", 1)[0]
    if command in ("/start", "/cancel", "/menu") or text in ("❌ Отмена", "🏠 Меню"):
        await state.clear()
        mode = 'Семейный' if user_id < 0 else 'Личный'
        await message.answer(f"{mode} бюджет. Добавьте доход или расход кнопкой либо напишите «кофе 350».\nНачальные деньги — в настройках. /help — подсказки.", reply_markup=MAIN_MENU)
        return
    if command == "/help":
        await state.clear()
        await message.answer("Запись: кнопка → сумма → категория → подтверждение. Дату и комментарий можно изменить.\n\nкофе 350 рублей\nвчера продукты 1,5к; #дом ужин\n+ зарплата 150000\n\nМожно диктовать текст клавиатуре iPhone. Аудиосообщения и фото чеков пока не распознаются.\n\n/payments — регулярные платежи\n/search #отпуск — поиск по всем месяцам\n/analytics — сравнение расходов\n/family — личный и общий бюджет\n\nМесяц в «Мой бюджет» применяется к истории, лимитам и Excel. Переводы между своими счетами не записывайте как доход или расход. /cancel отменяет ввод.\nПосле перезапуска незавершённый ввод нужно повторить; сохранённые операции остаются в базе.", reply_markup=MAIN_MENU)
        return
    menu_texts = {b.text for row in MAIN_MENU.keyboard + EXTRA_MENU.keyboard for b in row}
    if text in menu_texts or command in ('/family', '/payments', '/search', '/analytics'):
        await state.clear()
    if await family.handle_message(message, state, db):
        return
    if await recurring.handle_message(message, state, db, user_id):
        return
    if text == '📈 Аналитика' or command == '/analytics':
        await show_analytics(message, user_id)
        return
    if text == '🔎 Поиск' or command == '/search':
        query = text.partition(' ')[2].strip() if command == '/search' else ''
        if query:
            try:
                await show_search(message, state, user_id, query)
            except ValueError as exc:
                await message.answer(str(exc))
        else:
            await state.set_state(Form.search)
            await message.answer('Введите категорию, слово из комментария или метку: например #отпуск. Поиск по всем месяцам.', reply_markup=CANCEL_MENU)
        return
    if message.voice or message.photo or message.document:
        await message.answer('Пока принимаю текст. Можно надиктовать «кофе 350» через микрофон клавиатуры iPhone.', reply_markup=MAIN_MENU)
        return
    if text in ("➕ Доход", "➖ Расход"):
        await new_draft(message, state, "income" if text == "➕ Доход" else "expense")
        return
    if text in ("📊 Мой бюджет", "📊 Сводка", "📝 История", "🎯 Лимиты", "📁 Скачать Excel", "⚙️ Настройки", "Ещё"):
        await state.clear()
        if text in ("📊 Мой бюджет", "📊 Сводка"):
            await show_summary(message, user_id)
        elif text == "📝 История":
            await show_history(message, user_id, await db.selected_month(user_id))
        elif text == "🎯 Лимиты":
            await show_limits(message, user_id)
        elif text == "📁 Скачать Excel":
            await send_export(message, user_id)
        elif text == "Ещё":
            await message.answer("Дополнительные разделы:", reply_markup=EXTRA_MENU)
        else:
            await message.answer("Настройки бюджета:", reply_markup=await scoped_keyboard(message, [[("Начальные деньги", "opening")], [("Выбрать месяц", "pick_month")]]))
        return
    legacy = {"🤝 Долг": (Form.debt, "Сумма, направление, название. Пример: 30000 должен кредитка или 5000 мне Алексей"),
              "🎯 Цель": (Form.goal, "Сумма и название. Пример: 250000 отпуск"),
              "💬 Могу позволить?": (Form.afford, "Сколько стоит покупка?"), "💳 Кредитка?": (Form.credit, "Введите сумму покупки.")}
    if text in legacy:
        await state.clear()
        target, prompt = legacy[text]
        await state.set_state(target)
        await message.answer(prompt, reply_markup=CANCEL_MENU)
        return
    if text == "🧭 Распределение":
        await state.clear()
        data = await db.summary(user_id)
        debt, remaining = metrics(data)
        await message.answer(allocation(data["income"], data["expense"], debt, remaining), reply_markup=MAIN_MENU)
        return

    current, stored = await state.get_state(), await state.get_data()
    try:
        draft = stored.get("draft", {})
        if current == Form.amount.state:
            try:
                draft["amount"] = parse_amount(text)
            except ValueError:
                draft.update(quick_entry(text, draft["kind"], current=today(db.timezone)))
            await state.update_data(draft=draft)
            if draft.get("category"):
                await confirm(message, state)
            else:
                await choose_category(message, state)
        elif current == Form.category.state:
            draft["category"] = category_name(text)
            await state.update_data(draft=draft)
            await confirm(message, state)
        elif current == Form.date.state:
            if text.casefold() in ("сегодня", "вчера"):
                draft["occurred_on"] = (today(db.timezone) - timedelta(days=text.casefold() == "вчера")).isoformat()
            else:
                draft["occurred_on"] = parse_date(text, today(db.timezone))
            await state.update_data(draft=draft)
            await confirm(message, state)
        elif current == Form.note.state:
            if len(text) > 500:
                raise ValueError("Комментарий — до 500 символов")
            draft["note"] = "" if text == "-" else text
            await state.update_data(draft=draft)
            await confirm(message, state)
        elif current == Form.confirm.state:
            await message.answer("Подтвердите запись кнопкой «Сохранить» или выберите, что изменить. /cancel — отмена.")
        elif current == Form.budget_category.state:
            await state.update_data(category=category_name(text))
            await state.set_state(Form.budget_amount)
            await message.answer(f"Лимит для «{category_name(text)}» на {month_label(stored['month'])}? Введите сумму; 0 — не тратить, «убрать» — удалить лимит.")
        elif current == Form.budget_amount.state:
            amount = None if text.casefold() == "убрать" else parse_amount(text, allow_zero=True)
            await db.set_budget(user_id, stored["month"], stored["category"], amount)
            await state.clear()
            await message.answer(f"Лимит {'удалён' if amount is None else 'сохранён'}: {stored['category']} · {month_label(stored['month'])}.", reply_markup=MAIN_MENU)
        elif current == Form.search.state:
            await state.clear()
            await show_search(message, state, user_id, text)
        elif current == Form.opening.state:
            amount = parse_amount(text, allow_zero=True)
            await db.set_opening(user_id, amount)
            await state.clear()
            await message.answer(f"Начальные деньги: {money(amount)}. Все сохранённые операции будут прибавляться и вычитаться из этой суммы.", reply_markup=MAIN_MENU)
        elif current == Form.month.state:
            parts = text.split(".")
            if len(parts) != 2:
                raise ValueError("Введите месяц в формате ММ.ГГГГ, например 09.2026")
            month = valid_month(f"{parts[1]}-{parts[0]}")
            await db.select_month(user_id, month)
            await state.clear()
            await message.answer("Месяц выбран.", reply_markup=MAIN_MENU)
            await show_summary(message, user_id)
        elif current in (Form.debt.state, Form.goal.state):
            match = re.fullmatch(rf"({AMOUNT})\s+(.+)", text)
            if not match:
                raise ValueError("Введите сумму и описание, как в примере выше")
            amount, rest = parse_amount(match[1]), match[2]
            if current == Form.debt.state:
                direction, name = rest.split(maxsplit=1)
                direction = {"должен": "i_owe", "мне": "owed_to_me"}.get(direction.casefold())
                if not direction:
                    raise ValueError("Направление: должен или мне")
                await db.add_debt(user_id, name[:120], amount, direction)
            else:
                await db.add_goal(user_id, rest[:120], amount)
            await state.clear()
            await message.answer("Сохранено.", reply_markup=MAIN_MENU)
        elif current in (Form.afford.state, Form.credit.state):
            price = parse_amount(text)
            data = await db.summary(user_id)
            debt, remaining = metrics(data)
            planned = await recurring.obligations(db, user_id, today(db.timezone))
            free = data['balance'] - planned['total_minor']/100
            answer = affordability(price, free, debt, remaining) if current == Form.afford.state else credit_card_advice(price, free, debt)
            answer += f"\nУчтено предстоящих и неоплаченных платежей: {money(planned['total_minor']/100)}."
            await state.clear()
            await message.answer(answer, reply_markup=MAIN_MENU)
        else:
            await new_draft(message, state, entry=quick_entry(text, current=today(db.timezone)))
    except (ValueError, IndexError) as exc:
        await message.answer(str(exc) if str(exc) else "Проверьте формат ввода. /cancel — отмена.")


@router.callback_query()
async def handle_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if await family.handle_callback(callback, state, db):
        return
    user_id = await family.active_budget_id(db, callback.from_user.id)
    if await recurring.handle_callback(callback, state, db, user_id):
        return
    message, data = callback.message, callback.data or ""
    scoped = data.startswith('scope:')
    if data.startswith('scope:'):
        try:
            _, expected_budget, expected_revision, data = data.split(':',3)
            active = await family.active_budget_context(db, callback.from_user.id)
            if active != (int(expected_budget),int(expected_revision)):
                await callback.answer('Эта кнопка из другого бюджета. Откройте нужный раздел заново.')
                return
        except (ValueError,IndexError):
            await callback.answer('Кнопка устарела. Откройте /menu.')
            return
    parts = data.split(":")
    if not scoped and (data == 'opening' or parts[0] == 'budget'):
        await callback.answer('Откройте настройки или лимиты заново: /menu')
        return
    try:
        stored = await state.get_data()
        if parts[0] in ("draft", "cat"):
            draft = stored.get("draft")
            if not draft or len(parts) != 3 or parts[1] != draft["token"]:
                await callback.answer("Эта запись уже закрыта. Добавьте новую операцию.")
                return
            current = await state.get_state()
            if parts[0] == "cat" and current == Form.category.state:
                index = int(parts[2])
                if not 0 <= index < len(catalog(draft["kind"])):
                    raise ValueError
                draft["category"] = category_name(catalog(draft["kind"])[index])
                await state.update_data(draft=draft)
                await confirm(message, state)
            elif parts[0] == "draft" and current == Form.confirm.state:
                action = parts[2]
                if action == "save":
                    fields = {k: draft[k] for k in ("kind", "category", "amount", "note", "occurred_on")}
                    if draft.get("edit_id"):
                        found = await db.edit_transaction(user_id, draft["edit_id"], **fields, expected_version=draft.get('version'))
                        if not found:
                            await state.clear()
                            await callback.answer("Операция изменена или удалена. Откройте её из истории заново.")
                            return
                    else:
                        await db.add_transaction(user_id, **fields, source_message_id=draft["source_message_id"], actor_user_id=callback.from_user.id, actor_name=callback.from_user.first_name if user_id < 0 else '')
                    await state.clear()
                    await message.answer("✅ Сохранено\n" + transaction_text(draft), reply_markup=MAIN_MENU)
                    if draft['kind'] == 'expense':
                        status = await limit_status(db, user_id, draft['category'], draft['occurred_on'][:7])
                        if status:
                            await message.answer(status)
                elif action == "cancel":
                    await state.clear()
                    await message.answer("Запись отменена.", reply_markup=MAIN_MENU)
                elif action == "category":
                    await choose_category(message, state)
                elif action == "kind":
                    if draft.get('edit_id') and await recurring.linked_payment(db,user_id,draft['edit_id']):
                        await callback.answer('Это оплата регулярного платежа. Тип расхода менять нельзя.')
                        return
                    draft["kind"] = "expense" if draft["kind"] == "income" else "income"
                    draft.pop("category", None)
                    await state.update_data(draft=draft)
                    await choose_category(message, state)
                elif action in ("amount", "date", "note"):
                    await state.set_state({"amount": Form.amount, "date": Form.date, "note": Form.note}[action])
                    await message.answer({"amount": "Введите новую сумму.", "date": "Дата: ДД.ММ.ГГГГ, «сегодня» или «вчера».", "note": "Введите комментарий (до 500 символов), либо - чтобы убрать его."}[action])
            else:
                await callback.answer("Сначала завершите текущий шаг ввода.")
                return
        elif data == 'analytics':
            await state.clear()
            await show_analytics(message, user_id)
        elif parts[0] == 'search':
            if stored.get('search_token') != parts[1]:
                await callback.answer('Поиск устарел. Откройте /search снова.')
                return
            await show_search(message, state, user_id, stored['search_query'], max(0,int(parts[2])))
        elif data in ("current", "pick_month", "opening") or parts[0] == "month":
            await state.clear()
            if data == "opening":
                await state.set_state(Form.opening)
                await message.answer("Сколько своих денег у вас было перед самой первой записанной операцией?\nЭто заменит начальную сумму, а не добавит доход. Не вводите текущий баланс, если уже вносили операции. Можно 0.", reply_markup=CANCEL_MENU)
            elif data == "pick_month":
                await state.set_state(Form.month)
                await message.answer("Введите месяц: ММ.ГГГГ, например 09.2026.", reply_markup=CANCEL_MENU)
            else:
                month = today(db.timezone).strftime("%Y-%m") if data == "current" else valid_month(parts[1])
                await db.select_month(user_id, month)
                await show_summary(message, user_id)
        elif parts[0] == "history":
            await state.clear()
            await show_history(message, user_id, valid_month(parts[1]), max(0, int(parts[2])))
        elif parts[0] == "tx":
            row = await db.transaction(user_id, int(parts[2]))
            if not row:
                await callback.answer("Операция не найдена.")
                return
            await state.clear()
            if parts[1] == "view":
                await message.answer(transaction_text(row), reply_markup=inline([[("✏️ Изменить", f"tx:edit:{row['id']}"), ("🗑 Удалить", f"tx:delete:{row['id']}")]]))
            elif parts[1] == "edit":
                if await recurring.linked_payment(db,user_id,row['id']):
                    await message.answer('Это регулярный платёж. При изменении суммы он останется полностью оплаченным; удаление записи вернёт его в неоплаченные.')
                await state.update_data(draft={**row, "edit_id": row["id"], "token": secrets.token_hex(4)})
                await confirm(message, state)
            elif parts[1] == "delete":
                token = secrets.token_hex(4)
                await state.update_data(delete_id=row["id"], delete_token=token, delete_version=row.get('version'))
                await message.answer("Удалить эту операцию?\n" + transaction_text(row), reply_markup=inline([[("Да, удалить", f"delete:{token}"), ("Отмена", "delete_cancel")]]))
        elif parts[0] == "delete":
            if stored.get("delete_token") != parts[1]:
                await callback.answer("Подтверждение устарело.")
                return
            removed = await db.delete_transaction(user_id, stored["delete_id"], expected_version=stored.get('delete_version'))
            await state.clear()
            await message.answer("Операция удалена." if removed else 'Операция изменена или уже удалена. Откройте историю заново.', reply_markup=MAIN_MENU)
        elif data == "delete_cancel":
            await state.clear()
            await message.answer("Удаление отменено.", reply_markup=MAIN_MENU)
        elif parts[0] == "budget":
            month = valid_month(parts[1])
            token = secrets.token_hex(4)
            await state.clear()
            await state.update_data(month=month, budget_token=token)
            await state.set_state(Form.budget_category)
            await message.answer(f"Лимит на {month_label(month)}. Выберите категорию или напишите свою:", reply_markup=categories(EXPENSE_CATEGORIES, f"budgetcat:{token}"))
        elif parts[0] == "budgetcat":
            if stored.get("budget_token") != parts[1] or await state.get_state() != Form.budget_category.state:
                await callback.answer("Выбор устарел. Откройте лимиты снова.")
                return
            index = int(parts[2])
            if not 0 <= index < len(EXPENSE_CATEGORIES):
                raise ValueError
            category = category_name(EXPENSE_CATEGORIES[index])
            await state.update_data(category=category)
            await state.set_state(Form.budget_amount)
            await message.answer(f"Лимит «{category}» на {month_label(stored['month'])}?\nВведите сумму; 0 — не тратить, «убрать» — удалить лимит.")
        else:
            await callback.answer("Откройте актуальное меню: /menu")
            return
        await callback.answer()
    except (ValueError, IndexError, KeyError):
        await callback.answer("Кнопка устарела или данные некорректны. Откройте меню: /menu")


async def main() -> None:
    global db, allowed_user_ids
    settings = load_settings()
    allowed_user_ids = settings.allowed_user_ids
    db = Database(settings.db_path, settings.timezone)
    await db.init()
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(router)
    async with Bot(settings.bot_token) as bot:
        await bot.set_my_commands([
            BotCommand(command='menu', description='Открыть бюджет'),
            BotCommand(command='payments', description='Регулярные платежи'),
            BotCommand(command='analytics', description='Сравнение расходов'),
            BotCommand(command='search', description='Поиск записей и меток'),
            BotCommand(command='family', description='Семейный бюджет'),
            BotCommand(command='help', description='Как пользоваться'),
            BotCommand(command='whoami', description='Узнать свой Telegram ID'),
        ])
        await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
