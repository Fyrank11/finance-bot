from __future__ import annotations

import asyncio
import calendar
import logging
import re
import secrets
import weakref
from datetime import timedelta

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import BotCommand, BufferedInputFile, CallbackQuery, Message

from .backup import backup_before_upgrade
from .access import is_registered, register_user
from .branding import PRIVACY_TEXT, apply_text_profile, welcome_text
from .charts import build_chart_data, render_expense_chart
from .coaching import budget_tips
from .config import load_settings
from .db import Database
from .export import export_xlsx
from .finance import affordability, allocation, credit_card_advice, money
from .inputs import (AMOUNT, EXPENSE_CATEGORIES, INCOME_CATEGORIES, category_name,
                     month_label, parse_amount, parse_date, quick_entry, shift_month, today, valid_month)
from .keyboards import CANCEL_MENU, EXTRA_MENU, LEGACY_MENU_TEXTS, MAIN_MENU, categories, inline
from . import family, recurring, savings_ui, cashflow_ui, weekly, weekly_ui, guides, navigation
from .session_store import SQLiteStorage
from .release_runtime import (InFlightUpdates, get_release_version, init_runtime,
                              mark_ui_seen, refresh_menu_if_needed)
from .insights import comparison, limit_status, search_transactions
from .rate_limit import RateLimiter

router = Router()
db: Database
allowed_user_ids: frozenset[int] = frozenset()
public_signup = False
rate_limiter = RateLimiter()


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

    @classmethod
    def access_lock(cls):
        loop = asyncio.get_running_loop()
        return cls._locks.setdefault(loop, asyncio.Lock())

    async def __call__(self, handler, event, data):
        user = event.from_user
        message = event.message if isinstance(event, CallbackQuery) else event
        if public_signup and user and message and message.chat.type == 'private':
            text = (event.text or '').strip() if isinstance(event, Message) else ''
            command = text.split('@', 1)[0].split(' ', 1)[0]
            action = (event.data or '') if isinstance(event, CallbackQuery) else ''
            expensive = (command in ('/charts', '/analytics')
                         or text in ('📈 Аналитика', '📁 Скачать Excel')
                         or action == 'analytics' or action.startswith('charts:')
                         or ':charts:' in action or action.endswith(':analytics') or action.endswith(':sav:poster')
                         or action.startswith('guide:') or text == 'ℹ️ Как это работает'
                         or action.endswith((':nav:export', ':nav:analytics')))
            if not rate_limiter.allow(user.id, expensive=expensive):
                if rate_limiter.should_notify(user.id):
                    await event.answer('Слишком много запросов подряд. Подождите минуту и попробуйте снова.')
                return
        async with self.access_lock():
            return await self._handle(handler, event, data)

    async def _handle(self, handler, event, data):
        user = event.from_user
        message = event.message if isinstance(event, CallbackQuery) else event
        private_user = bool(user and not user.is_bot and user.id > 0
                            and message and message.chat.type == 'private')
        if private_user and isinstance(event, Message):
            command = (event.text or '').strip().split('@', 1)[0].split(' ', 1)[0]
            if command == '/privacy':
                await event.answer(PRIVACY_TEXT)
                return
            if command == '/whoami':
                hint = 'Доступ открывается автоматически после /start.' if public_signup else 'Передайте этот номер владельцу бота для подключения.'
                await event.answer(f'Ваш Telegram ID: {user.id}\n{hint}')
                return
            if command == '/start' and public_signup:
                # Telegram authenticates the sender. Never accept an ID, family
                # invitation or balance from /start's optional deep-link payload.
                await register_user(db, user.id)
            elif command == '/start' and not allowed_user_ids:
                await event.answer('Бот готов к настройке. Отправьте /whoami, добавьте свой ID в ALLOWED_USER_IDS и перезапустите бота.')
                return
        if not private_user:
            await event.answer("Для личного бюджета откройте личный чат с ботом.")
            return
        permitted = user.id in allowed_user_ids
        if public_signup and not permitted:
            permitted = await is_registered(db, user.id)
        if not permitted:
            text = ('Чтобы открыть свой личный бюджет, нажмите «Начать» или отправьте /start. '
                    'О хранении данных: /privacy.') if public_signup else "У вас нет доступа к этому боту."
            await event.answer(text)
            return
        state = data.get('state')
        if state is None:
            return await handler(event, data)
        budget_id, revision = await family.active_budget_context(db, user.id)
        scope = f'{budget_id}:{revision}'
        stored = await state.get_data()
        if stored.get('_scope') not in (None, scope):
            await state.clear()
            if isinstance(event, CallbackQuery) and not (event.data or '').startswith(('family:', 'guide:')):
                await event.answer('Бюджет или доступ изменился. Откройте /menu заново.')
                return
        topic = None
        try:
            # Refresh an old reply keyboard in response to activity, never as a
            # startup broadcast. An active form retains its cancellation keyboard.
            await refresh_menu_if_needed(db, event, state, MAIN_MENU)
            if isinstance(event, Message):
                topic = guides.topic_for_message(event.text or '')
            elif isinstance(event, CallbackQuery):
                raw = event.data or ''
                if raw.startswith('scope:'):
                    try:
                        _, selected, selected_revision, action = raw.split(':', 3)
                        raw = action if (int(selected), int(selected_revision)) == (budget_id, revision) else ''
                    except (ValueError, IndexError):
                        raw = ''
                # Navigation callbacks for financial views require current scope.
                if raw.startswith('nav:') and (event.data or '').startswith('scope:'):
                    topic = navigation.TOPICS.get(raw.removeprefix('nav:'))
                elif not raw.startswith('nav:'):
                    topic = guides.topic_for_callback(raw)
            if topic and isinstance(event, Message):
                await guides.maybe_show(db, message, user.id, topic)
            result = await handler(event, data)
            # Callback handlers acknowledge the tap before optional image I/O.
            # A slow help image must not leave Telegram's spinner running.
            if topic and isinstance(event, CallbackQuery):
                await guides.maybe_show(db, message, user.id, topic)
            if isinstance(event, Message) and (event.text or '').split('@', 1)[0].split(' ', 1)[0] in ('/start', '/menu'):
                await mark_ui_seen(db, user.id)
            else:
                await refresh_menu_if_needed(db, event, state, MAIN_MENU)
            return result
        finally:
            # A family action intentionally changes the active context and
            # generates a fresh panel. All other forms retain their origin.
            if isinstance(event, CallbackQuery) and (event.data or '').startswith('family:'):
                current_budget, current_revision = await family.active_budget_context(db, user.id)
                scope = f'{current_budget}:{current_revision}'
            await state.update_data(_scope=scope)
            if topic:
                await state.update_data(_help_topic=topic)


router.message.outer_middleware(AccessMiddleware())
router.callback_query.outer_middleware(AccessMiddleware())


def create_dispatcher(path, *, before_drain=None) -> Dispatcher:
    """Track updates before FSM locks; drain before storage/isolation close."""
    tracker = InFlightUpdates()

    class DrainingStorage(SQLiteStorage):
        async def close(self):
            if before_drain is not None:
                before_drain()
            await tracker.drain(timeout=20)
            await super().close()

    dispatcher = Dispatcher(storage=DrainingStorage(path), events_isolation=SimpleEventIsolation(), disable_fsm=True)
    dispatcher.update.outer_middleware(tracker)
    dispatcher.update.outer_middleware(dispatcher.fsm)
    return dispatcher


def catalog(kind: str) -> tuple[str, ...]:
    return INCOME_CATEGORIES if kind == "income" else EXPENSE_CATEGORIES


async def scoped_keyboard(message: Message, rows):
    budget_id, revision = await family.active_budget_context(db, message.chat.id)
    return inline([[(label, action if action.startswith('guide:') else f'scope:{budget_id}:{revision}:{action}') for label,action in row] for row in rows])


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
    await state.update_data(draft=draft, _help_topic=draft['kind'])
    await guides.maybe_show(db, message, message.from_user.id, draft['kind'])
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
    section_links = await navigation.section_rows(db, message, [
        ('📝 История', 'history'), ('🎯 Лимиты', 'limits'),
        ('🔎 Поиск', 'search'), ('📁 Скачать Excel', 'export'),
    ])
    await message.answer(
        f"📊 {scope_label} · {month_label(month)}\n\nДоходы: {money(data['income'])}\nРасходы: {money(data['expense'])}"
        f"\nРазница за месяц: {money(data['net'])}\nРасчётный остаток: {money(data['balance'])}"
        f"\n\nОстаток включает начальные {money(data['opening'])} и все операции до конца выбранного месяца."
        f"\n\nТекущие долги: {money(debt)}\nНа цели осталось: {money(goals_remaining)}"
        f"{upcoming}\n\nТоп расходов:\n{cats}", reply_markup=inline([
            month_buttons,
            [("Текущий месяц", "current"), ("Выбрать месяц", "pick_month")],
            [("📈 Графики и сравнение", "analytics")],
            *section_links, [('ℹ️ Как это работает', 'guide:budget')],
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
    buttons.append([('ℹ️ Как это работает', 'guide:history')])
    await message.answer(f"📝 {month_label(month)}\n" + ("Нажмите на операцию, чтобы изменить или удалить её." if rows else "Операций пока нет."), reply_markup=inline(buttons))


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
    await message.answer("Лимиты сохраняются отдельно для каждого месяца.", reply_markup=await scoped_keyboard(message, [[("Задать / изменить лимит", f"budget:{month}")], [('ℹ️ Как это работает', 'guide:limits')]]))


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
    await message.answer_document(BufferedInputFile(content, filename=f"budget_{month}.xlsx"), caption=f"Бюджет · {month_label(month)}. Снимок данных на момент выгрузки.", reply_markup=inline([[('ℹ️ Как это работает', 'guide:export')]]))


async def show_chart(message: Message, budget_id: int, month: str | None = None) -> None:
    month = valid_month(month) if month else await db.selected_month(budget_id)
    snapshot = await build_chart_data(db, budget_id, month, today(db.timezone))
    scope = 'Семейный бюджет' if budget_id < 0 else 'Личный бюджет'
    try:
        content = await asyncio.to_thread(render_expense_chart, snapshot, scope_label=scope)
    except Exception:
        # Do not log user data or turn a renderer failure into a broken budget flow.
        logging.getLogger(__name__).warning('Expense chart rendering failed.')
        await message.answer('График сейчас не удалось подготовить. Числа доступны в «Мой бюджет», подсказки — /tips.', reply_markup=MAIN_MENU)
        return
    await message.answer_photo(
        BufferedInputFile(content, filename=f'expenses_{month}.png'),
        caption=f'📊 {scope} · {month_label(month)}\nСнимок по внесённым операциям. Дни без записей не подтверждают отсутствие трат.',
        reply_markup=await scoped_keyboard(message, [[('💡 Подсказки к этому месяцу', f'tips:{month}')], [('ℹ️ Как это работает', 'guide:analytics')]]),
    )


async def show_tips(message: Message, budget_id: int, month: str | None = None) -> None:
    month = valid_month(month) if month else await db.selected_month(budget_id)
    tips = await budget_tips(db, budget_id, month, today(db.timezone))
    scope = 'Семейный бюджет' if budget_id < 0 else 'Личный бюджет'
    body = '\n\n'.join(f'{index}. {tip}' for index, tip in enumerate(tips, 1))
    await message.answer(
        f'💡 Подсказки · {month_label(month)}\n{scope}\n\n{body}'
        '\n\nРасчёты по вашим записям. Начните с одного подходящего шага.',
        reply_markup=await scoped_keyboard(message, [[('📊 График этого месяца', f'charts:{month}')], [('ℹ️ Как это работает', 'guide:tips')]]),
    )


async def show_analytics(message: Message, budget_id: int) -> None:
    month = await db.selected_month(budget_id)
    await show_chart(message, budget_id, month)
    data = await comparison(db, budget_id, month, today(db.timezone))
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
        '\n\nСравниваются одинаковые по длительности отрезки по внесённым операциям.',
        reply_markup=await scoped_keyboard(message, [[('💡 Что можно улучшить', f'tips:{month}')],
            [('📬 Обзор недели', 'nav:weekly'), ('ℹ️', 'guide:weekly')],
            [('📁 Скачать Excel', 'nav:export'), ('ℹ️', 'guide:export')],
            [('ℹ️ Как это работает', 'guide:analytics')]]))


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
    buttons.append([('ℹ️ Как это работает', 'guide:search')])
    await message.answer(
        f"🔎 «{query}» · все месяцы\nНайдено: {result['count']}\n"
        f"Доходы: {money(result['income_minor']/100)}\nРасходы: {money(result['expense_minor']/100)}",
        reply_markup=inline(buttons) if buttons else MAIN_MENU)


def metrics(data: dict) -> tuple[float, float]:
    return data["debts"].get("i_owe", 0), sum(max(g["target"] - g["saved"], 0) for g in data["goals"])


async def show_settings(message):
    await message.answer('Настройки бюджета:', reply_markup=await scoped_keyboard(message, [
        [('Начальные деньги', 'opening'), ('ℹ️', 'guide:opening')],
        [('Выбрать месяц', 'pick_month'), ('ℹ️', 'guide:month')],
        [('ℹ️ Как это работает', 'guide:settings')],
    ]))


async def current_help_topic(state):
    current, stored = await state.get_state(), await state.get_data()
    if current:
        group, _, field = current.partition(':')
        simple = {'ForecastForm': 'forecast', 'WeeklyForm': 'weekly', 'RecurringForm': 'payments', 'FamilyForm': 'family'}
        if group in simple:
            return simple[group]
        if group == 'SavingsForm':
            kind = stored.get('savings_draft', {}).get('kind')
            return {'budget': 'savings_budget', 'reserve': 'reserve'}.get(kind, 'goals')
        if group == 'Form':
            explicit = {'budget_category': 'limits', 'budget_amount': 'limits', 'opening': 'opening',
                        'month': 'month', 'debt': 'debts', 'goal': 'goals', 'afford': 'afford', 'credit': 'credit', 'search': 'search'}
            if field in explicit:
                return explicit[field]
            if stored.get('draft', {}).get('kind') in ('income', 'expense'):
                return stored['draft']['kind']
    topic = stored.get('_help_topic')
    return topic if topic in navigation.GUIDE_CATALOG else None


async def open_navigation(callback, state, budget_id, data, *, scoped):
    if not data.startswith('nav:'):
        return False
    parts = data.split(':')
    public_actions = {'help', 'begin', 'welcome', 'privacy'}
    routes = set(navigation.TOPICS) | set(navigation.HUBS) | public_actions
    if (len(parts) not in (2, 3) or parts[1] not in routes
            or (len(parts) == 3 and (parts[1] != 'help' or not re.fullmatch(r'\d{1,2}', parts[2])))):
        await callback.answer('Кнопка устарела. Откройте /menu.')
        return True
    action, message = parts[1], callback.message
    if not scoped and action not in public_actions:
        await callback.answer('Откройте раздел из текущего меню: /menu')
        return True
    # Reading instructions never abandons a partially completed transaction.
    if action == 'help':
        await navigation.show_help(message, db, int(parts[2]) if len(parts) == 3 else 0)
    elif action == 'welcome':
        await callback.answer()
        await guides.show_welcome(db, message, callback.from_user.id, force=True)
        return True
    elif action == 'privacy':
        await message.answer(PRIVACY_TEXT)
    else:
        await state.clear()
        if action == 'begin':
            await message.answer('Выберите «Доход» или «Расход» либо напишите «кофе 350». Подтвердите запись перед сохранением.', reply_markup=MAIN_MENU)
        elif action in navigation.HUBS:
            await navigation.show_hub(message, db, action)
        elif action == 'summary':
            await show_summary(message, budget_id)
        elif action == 'history':
            await show_history(message, budget_id, await db.selected_month(budget_id))
        elif action == 'limits':
            await show_limits(message, budget_id)
        elif action == 'export':
            await send_export(message, budget_id)
        elif action == 'payments':
            await recurring._show(message, db, budget_id)
        elif action == 'savings':
            await savings_ui.show_home(message, db, budget_id)
        elif action == 'goals':
            await savings_ui.show_goals(message, db, budget_id)
        elif action == 'forecast':
            await cashflow_ui.show_home(message, db, budget_id)
        elif action == 'weekly':
            await weekly_ui.show_home(message, db, budget_id)
        elif action == 'analytics':
            await show_analytics(message, budget_id)
        elif action == 'tips':
            await show_tips(message, budget_id)
        elif action == 'family':
            await family.show_family(message, state, db, callback.from_user.id)
        elif action == 'settings':
            await show_settings(message)
        elif action == 'search':
            await state.set_state(Form.search)
            await message.answer('Введите категорию, слово из комментария или метку: например #отпуск. Поиск по всем месяцам.', reply_markup=CANCEL_MENU)
        else:
            target, prompt = {
                'debts': (Form.debt, 'Сумма, направление, название. Пример: 30000 должен кредитка или 5000 мне Алексей'),
                'afford': (Form.afford, 'Сколько стоит покупка?'),
                'credit': (Form.credit, 'Введите сумму покупки.'),
            }[action]
            await state.set_state(target)
            await message.answer(prompt, reply_markup=CANCEL_MENU)
    await callback.answer()
    return True


@router.message()
async def handle_message(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    user_id = await family.active_budget_id(db, message.from_user.id)
    command = text.split("@", 1)[0].split(" ", 1)[0]
    if text == 'ℹ️ Как это работает':
        topic = await current_help_topic(state)
        if topic in navigation.GUIDE_CATALOG:
            await guides.maybe_show(db, message, message.from_user.id, topic, force=True)
        else:
            await navigation.show_help(message, db)
        return
    if command in ("/start", "/cancel", "/menu") or text in ("❌ Отмена", "🏠 Меню"):
        await state.clear()
        if command == '/start':
            await guides.show_welcome(db, message, message.from_user.id)
            await message.answer(welcome_text(family_budget=user_id < 0), reply_markup=MAIN_MENU)
            return
        mode = 'Семейный' if user_id < 0 else 'Личный'
        await message.answer(f"{mode} бюджет. Добавьте доход или расход кнопкой либо напишите «кофе 350».\nНачальные деньги — в настройках. /help — подсказки.", reply_markup=MAIN_MENU)
        return
    if command == '/help':
        topic = text.partition(' ')[2].strip()
        if topic in navigation.GUIDE_CATALOG:
            await guides.maybe_show(db, message, message.from_user.id, topic, force=True)
        else:
            await navigation.show_help(message, db)
        return
    menu_texts = {b.text for row in MAIN_MENU.keyboard + EXTRA_MENU.keyboard for b in row} | LEGACY_MENU_TEXTS
    if text in menu_texts or command in ('/family', '/payments', '/search', '/analytics', '/charts', '/tips', '/savings', '/forecast', '/weekly'):
        await state.clear()
    if text in ('🧭 Планы', '⚙️ Настройки и помощь'):
        await navigation.show_hub(message, db, 'plans' if text == '🧭 Планы' else 'settings_help')
        return
    if await family.handle_message(message, state, db):
        return
    if await recurring.handle_message(message, state, db, user_id):
        return
    if await savings_ui.handle_message(message, state, db, user_id):
        return
    if await cashflow_ui.handle_message(message, state, db, user_id):
        return
    if await weekly_ui.handle_message(message, state, db, user_id):
        return
    if text == '📈 Аналитика' or command == '/analytics':
        await show_analytics(message, user_id)
        return
    if command == '/charts':
        await show_chart(message, user_id)
        return
    if text == '💡 Подсказки' or command == '/tips':
        await show_tips(message, user_id)
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
            await show_settings(message)
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
    if await guides.handle_callback(callback, state, db):
        return
    if (callback.data or '').startswith('nav:'):
        await open_navigation(callback, state, await family.active_budget_id(db, callback.from_user.id), callback.data, scoped=False)
        return
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
    if await open_navigation(callback, state, user_id, data, scoped=scoped):
        return
    if await savings_ui.handle_callback(callback, state, db, user_id, data, scoped=scoped):
        return
    if await cashflow_ui.handle_callback(callback, state, db, user_id, data, scoped=scoped):
        return
    if await weekly_ui.handle_callback(callback, state, db, user_id, data, scoped=scoped):
        return
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
        elif parts[0] in ('charts', 'tips'):
            if not scoped or len(parts) != 2:
                await callback.answer('Откройте аналитику заново: /analytics')
                return
            month = valid_month(parts[1])
            await state.clear()
            # Acknowledge before rendering or uploading the image.
            await callback.answer()
            if parts[0] == 'charts':
                await show_chart(message, user_id, month)
            else:
                await show_tips(message, user_id, month)
            return
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
    global db, allowed_user_ids, public_signup
    settings = load_settings()
    allowed_user_ids = settings.allowed_user_ids
    public_signup = settings.public_signup
    db = Database(settings.db_path, settings.timezone)
    await asyncio.to_thread(backup_before_upgrade, settings.db_path, include_savings=True)
    await db.init()
    await init_runtime(db)
    logging.getLogger(__name__).info("Database initialized: %s", settings.db_path.resolve())
    logging.getLogger(__name__).info("Access mode: %s", "self-service /start" if public_signup else "allowlist")
    logging.getLogger(__name__).info("Release: %s; persistent dialogs enabled", get_release_version())
    logging.getLogger(__name__).info("Visual guides enabled: %s topics; compact menu", len(guides.GUIDE_CATALOG))
    weekly_stop = asyncio.Event()
    dispatcher = create_dispatcher(db.path, before_drain=weekly_stop.set)
    await dispatcher.storage.init()
    dispatcher.include_router(router)
    async with Bot(settings.bot_token) as bot:
        await bot.set_my_commands([
            BotCommand(command='menu', description='Открыть бюджет'),
            BotCommand(command='savings', description='Накопления, цели и резерв'),
            BotCommand(command='forecast', description='Прогноз до следующего дохода'),
            BotCommand(command='weekly', description='Обзор недели и его расписание'),
            BotCommand(command='payments', description='Регулярные платежи'),
            BotCommand(command='analytics', description='Графики и сравнение расходов'),
            BotCommand(command='charts', description='График расходов за месяц'),
            BotCommand(command='tips', description='Подсказки по вашему бюджету'),
            BotCommand(command='search', description='Поиск записей и меток'),
            BotCommand(command='family', description='Семейный бюджет'),
            BotCommand(command='help', description='Как пользоваться'),
            BotCommand(command='privacy', description='Как хранятся ваши данные'),
            BotCommand(command='whoami', description='Узнать свой Telegram ID'),
        ])
        await apply_text_profile(bot, public_signup=public_signup)
        weekly_task = asyncio.create_task(weekly.run_weekly_worker(
            bot, db, access_lock=AccessMiddleware.access_lock(), allowed_user_ids=allowed_user_ids,
            public_signup=public_signup, stop_event=weekly_stop,
        ), name='weekly-reports')
        logging.getLogger(__name__).info('Weekly worker started; delivery requires explicit subscription')
        try:
            await dispatcher.start_polling(bot, close_bot_session=False)
        finally:
            weekly_stop.set()
            try:
                await asyncio.wait_for(asyncio.shield(weekly_task), timeout=12)
            except asyncio.TimeoutError:
                weekly_task.cancel()
                await asyncio.gather(weekly_task, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(main())
