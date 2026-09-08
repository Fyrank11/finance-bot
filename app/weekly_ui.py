"""Voluntary weekly Telegram reports; settings belong to the recipient."""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram.fsm.state import State, StatesGroup

from . import family, weekly
from .keyboards import CANCEL_MENU, MAIN_MENU, inline

DAYS = ('Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье')
ZONES = (
    ('Москва', 'Europe/Moscow'), ('Калининград', 'Europe/Kaliningrad'),
    ('Екатеринбург', 'Asia/Yekaterinburg'), ('Омск', 'Asia/Omsk'),
    ('Новосибирск', 'Asia/Novosibirsk'), ('Иркутск', 'Asia/Irkutsk'),
    ('Якутск', 'Asia/Yakutsk'), ('Владивосток', 'Asia/Vladivostok'),
    ('Магадан', 'Asia/Magadan'), ('Камчатка', 'Asia/Kamchatka'),
    ('UTC', 'UTC'),
)


class WeeklyForm(StatesGroup):
    day = State()
    time = State()
    zone = State()
    confirm = State()


def now():
    return datetime.now(timezone.utc)


def budget_label(budget_id):
    return 'семейный бюджет' if budget_id < 0 else 'личный бюджет'


async def keyboard(db, message, rows):
    budget_id, revision = await family.active_budget_context(db, message.chat.id)
    return inline([[(label, f'scope:{budget_id}:{revision}:wk:{action}') for label, action in row] for row in rows])


async def show_home(message, db, budget_id):
    subscription = await weekly.get_subscription(db, message.chat.id)
    text = ('📬 Обзор недели\n\nДоходы, расходы и их изменение за последние 7 завершенных дней, '
            'крупные категории и предстоящие платежи. Все суммы — по внесенным записям.\n\n')
    if subscription and subscription['enabled']:
        text += (f"Рассылка включена: {budget_label(subscription['budget_id'])}.\n"
                 f"{DAYS[subscription['weekday']]}, {subscription['hour']:02d}:{subscription['minute']:02d} "
                 f"({subscription['timezone']}).\n")
        if subscription.get('next_due_at'):
            due = datetime.fromisoformat(subscription['next_due_at']).astimezone(ZoneInfo(subscription['timezone']))
            text += f"Следующий обзор: {due.strftime('%d.%m.%Y %H:%M')}.\n"
        text += 'Переключение бюджета в меню не меняет выбранную рассылку.\n\n'
    else:
        text += 'Рассылка выключена. Ее можно включить только после подтверждения расписания.\n\n'
    text += (f'Сейчас выбран: {budget_label(budget_id)}.\n'
             '«Показать обзор» — отчет по этому бюджету сейчас. '
             '«Настроить рассылку» — выбрать его для следующих отчетов. Отключить можно в любой момент.')
    rows = [[('Показать обзор', 'preview')], [('Настроить рассылку', 'setup')]]
    if subscription and subscription['enabled']:
        rows.append([('🔕 Отключить рассылку', 'off')])
    await message.answer(text, reply_markup=await keyboard(db, message, rows))


async def begin(message, state, db, budget_id):
    subscription = await weekly.get_subscription(db, message.chat.id)
    await state.clear()
    token = secrets.token_hex(4)
    await state.update_data(weekly_draft={
        'token': token, 'budget_id': budget_id,
        'version': subscription['version'] if subscription else None,
    })
    await state.set_state(WeeklyForm.day)
    rows = [[(label, f'day:{token}:{index}')] for index, label in enumerate(DAYS)]
    await message.answer(f'Рассылка: {budget_label(budget_id)}.\nВ какой день присылать обзор?\n/cancel — отмена.',
                         reply_markup=await keyboard(db, message, rows))


async def choose_zone(message, state, db):
    draft = (await state.get_data())['weekly_draft']
    await state.set_state(WeeklyForm.zone)
    rows = [[(label, f"zone:{draft['token']}:{index}") for index, (label, _) in enumerate(ZONES)][i:i + 2]
            for i in range(0, len(ZONES), 2)]
    await message.answer('Выберите часовой пояс или введите его название, например Europe/Moscow или Asia/Almaty. '
                         'Время отчета будет местным для этого пояса.', reply_markup=await keyboard(db, message, rows))


async def confirm(message, state, db):
    draft = (await state.get_data())['weekly_draft']
    due = weekly.next_due(now(), weekday=draft['weekday'], hour=draft['hour'], minute=draft['minute'], timezone=draft['timezone'])
    local_due = due.astimezone(ZoneInfo(draft['timezone']))
    await state.set_state(WeeklyForm.confirm)
    await message.answer(
        f"Включить личные сообщения с обзором?\n\nБюджет: {budget_label(draft['budget_id'])}.\n"
        f"{DAYS[draft['weekday']]}, {draft['hour']:02d}:{draft['minute']:02d} ({draft['timezone']}).\n"
        f"Первый отчет: {local_due.strftime('%d.%m.%Y %H:%M')}.\n\n"
        'Отчет содержит суммы вашего выбранного бюджета и придет в этот чат. '
        f'Доставка обычно в течение минуты от выбранного времени. Даты учета: {db.timezone}. Отключение: /weekly.',
        reply_markup=await keyboard(db, message, [[('✅ Включить', f"confirm:{draft['token']}")], [('Отмена', f"cancel:{draft['token']}")]]),
    )


async def handle_message(message, state, db, budget_id):
    text = (message.text or '').strip()
    command = text.split('@', 1)[0].split(' ', 1)[0]
    if command == '/weekly' or text == '📬 Обзор недели':
        await state.clear()
        await show_home(message, db, budget_id)
        return True
    current = await state.get_state()
    if not current or not current.startswith('WeeklyForm:'):
        return False
    draft = (await state.get_data()).get('weekly_draft')
    if not draft:
        await state.clear()
        await show_home(message, db, budget_id)
        return True
    if current == WeeklyForm.time.state:
        match = re.fullmatch(r'([01]?\d|2[0-3]):([0-5]\d)', text)
        if not match:
            await message.answer('Введите время в формате ЧЧ:ММ, например 19:30.')
            return True
        draft.update(hour=int(match[1]), minute=int(match[2]))
        await state.update_data(weekly_draft=draft)
        await choose_zone(message, state, db)
    elif current == WeeklyForm.zone.state:
        try:
            if len(text) > 80 or not re.fullmatch(r'[A-Za-z_+\-/0-9]+', text):
                raise ValueError
            ZoneInfo(text)
        except (ZoneInfoNotFoundError, ValueError):
            await message.answer('Не нашел часовой пояс. Выберите город кнопкой или введите название, например Asia/Almaty.')
            return True
        draft['timezone'] = text
        await state.update_data(weekly_draft=draft)
        await confirm(message, state, db)
    else:
        await message.answer('Выберите кнопку в текущем шаге. /cancel — отмена.')
    return True


async def handle_callback(callback, state, db, budget_id, data, *, scoped):
    if not data.startswith('wk:'):
        return False
    if not scoped:
        await callback.answer('Откройте обзор заново: /weekly')
        return True
    parts = data.split(':')
    action = parts[1]
    arity = {'home': 2, 'preview': 2, 'setup': 2, 'off': 2, 'day': 4, 'zone': 4, 'confirm': 3, 'cancel': 3}
    if action not in arity or len(parts) != arity[action]:
        await callback.answer('Кнопка устарела. Откройте /weekly.')
        return True
    message, user_id = callback.message, callback.from_user.id
    if action in ('day', 'zone', 'confirm', 'cancel'):
        draft = (await state.get_data()).get('weekly_draft', {})
        expected_state = {'day': WeeklyForm.day.state, 'zone': WeeklyForm.zone.state,
                          'confirm': WeeklyForm.confirm.state, 'cancel': WeeklyForm.confirm.state}[action]
        if (draft.get('token') != parts[2] or draft.get('budget_id') != budget_id
                or await state.get_state() != expected_state):
            await callback.answer('Эта настройка уже закрыта. Откройте /weekly.')
            return True
        if action == 'cancel':
            await state.clear()
            await message.answer('Настройка отменена.', reply_markup=MAIN_MENU)
        elif action == 'confirm':
            ok = await weekly.set_subscription(
                db, user_id, budget_id=budget_id, weekday=draft['weekday'], hour=draft['hour'],
                minute=draft['minute'], timezone=draft['timezone'], enabled=True,
                expected_version=draft['version'], now=now(),
            )
            await state.clear()
            await message.answer('Рассылка включена.' if ok else 'Настройки уже изменились. Откройте /weekly и повторите выбор.', reply_markup=MAIN_MENU)
            await show_home(message, db, budget_id)
        else:
            limit = 7 if action == 'day' else len(ZONES)
            if not re.fullmatch(r'\d{1,2}', parts[3]) or not 0 <= int(parts[3]) < limit:
                await callback.answer('Выберите кнопку из текущего шага.')
                return True
            index = int(parts[3])
            if action == 'day':
                draft['weekday'] = index
                await state.update_data(weekly_draft=draft)
                await state.set_state(WeeklyForm.time)
                await message.answer('В какое время? Введите ЧЧ:ММ, например 19:30. Далее выберем часовой пояс.', reply_markup=CANCEL_MENU)
            else:
                draft['timezone'] = ZONES[index][1]
                await state.update_data(weekly_draft=draft)
                await confirm(message, state, db)
    else:
        await state.clear()
        if action == 'setup':
            await begin(message, state, db, budget_id)
        elif action == 'off':
            subscription = await weekly.get_subscription(db, user_id)
            if subscription and subscription['enabled']:
                ok = await weekly.set_subscription(
                    db, user_id, **{key: subscription[key] for key in ('budget_id', 'weekday', 'hour', 'minute', 'timezone')},
                    enabled=False, expected_version=subscription['version'], now=now(),
                )
                await message.answer('Рассылка выключена.' if ok else 'Настройки изменились. Откройте /weekly и повторите отключение.', reply_markup=MAIN_MENU)
            await show_home(message, db, budget_id)
        elif action == 'preview':
            report = await weekly.build_digest(db, budget_id, now().astimezone(ZoneInfo(db.timezone)).date())
            await message.answer(report, reply_markup=await keyboard(db, message, [[('Настроить рассылку', 'setup'), ('Назад', 'home')]]))
        else:
            await show_home(message, db, budget_id)
    await callback.answer()
    return True
