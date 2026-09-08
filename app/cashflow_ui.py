"""A confirmed cash snapshot and dated plans, never automatic ledger entries."""
from __future__ import annotations

import re
import secrets
from datetime import date, datetime, timedelta

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from . import cashflow, family, savings
from .coaching import _money as money
from .inputs import parse_amount, to_minor, today
from .keyboards import CANCEL_MENU, MAIN_MENU, inline


class ForecastForm(StatesGroup):
    balance = State()
    daily = State()
    buffer = State()
    event_name = State()
    event_amount = State()
    event_date = State()
    confirm = State()


KINDS = {'income': 'Ожидаемое поступление', 'expense': 'Разовый расход', 'saving': 'Взнос в накопления'}
ICONS = {'income': '➕', 'expense': '➖', 'saving': '🌱'}
BALANCE_PROMPT = (
    'Сколько денег сейчас доступно для этого бюджета, ₽? Можно 0; дефицит можно указать со знаком минус.\n'
    'Сверьте счета и наличные. Не включайте кредитные лимиты и деньги, уже отделенные в резерв или на цели.\n'
    'Расчет начнется с этого остатка: прошедшие операции повторно не вычитаются. /cancel — отмена.'
)
DAILY_PROMPT = (
    'Сколько планируете на обычные траты в день, ₽? Можно 0.\n'
    'Не включайте регулярные платежи из «Платежей», разовые расходы и взносы, которые отдельно зададите по датам.'
)
BUFFER_PROMPT = (
    'Какую дополнительную часть введенного остатка хотите оставить неприкосновенной, ₽? Можно 0.\n'
    'Она еще входит в указанную сумму. Не повторяйте здесь резерв и накопления, уже исключенные из остатка.'
)
DISCLAIMER = 'Это сценарий по вашим записям, без подключения к банкам. Ожидаемый доход еще не получен и может измениться.'


def amount(text, *, positive=False, signed=False):
    if signed and text.startswith('-'):
        return -to_minor(parse_amount(text[1:].strip(), allow_zero=True), allow_zero=True)
    return to_minor(parse_amount(text, allow_zero=not positive), allow_zero=not positive)


def event_date(text: str, as_of: date) -> str:
    try:
        parsed = datetime.strptime(text, '%d.%m.%Y').date()
    except ValueError as exc:
        raise ValueError('Введите дату в формате ДД.ММ.ГГГГ.') from exc
    if not as_of <= parsed <= as_of + timedelta(days=89):
        raise ValueError('Дата — с сегодняшнего дня до 89 дней вперед.')
    return parsed.isoformat()


def day_label(value):
    try:
        return date.fromisoformat(value).strftime('%d.%m.%Y')
    except (TypeError, ValueError):
        return 'дата требует проверки'


async def keyboard(db, message, rows):
    budget_id, revision = await family.active_budget_context(db, message.chat.id)
    return inline([[(label, f'scope:{budget_id}:{revision}:cf:{action}') for label, action in row] for row in rows])


async def monthly_savings_note(db, budget_id):
    as_of = today(db.timezone)
    goals = await savings.list_goals(db, budget_id)
    reserve = await savings.get_reserve(db, budget_id)
    total = sum(g['monthly_minor'] for g in goals if savings.goal_plan(g, as_of)['remaining_minor'])
    if reserve and savings.reserve_plan(reserve)['remaining_minor']:
        total += reserve['monthly_minor']
    if not total:
        return ''
    return (f'\nВ «Накоплениях» выбрано {money(total)} / месяц на цели и резерв. '
            'Эта сумма справочная: для прогноза добавьте взносы с датами и не повторяйте их в других расходах.\n')


async def show_home(message, db, budget_id):
    report = await cashflow.forecast(db, budget_id, today(db.timezone))
    profile = report['profile']
    text = f"🔮 Прогноз · {'семейный' if budget_id < 0 else 'личный'} бюджет\n\n"
    if profile:
        text += (f"Последняя сверка: {day_label(profile['confirmed_on'])}\n"
                 f"Подтвержденный остаток: {money(profile['balance_minor'])}\n"
                 f"Обычные траты: {money(profile['daily_minor'])} / день\n"
                 f"Дополнительный запас: {money(profile['buffer_minor'])}\n\n")
    if report['stale']:
        reason = {
            'missing_profile': 'Сначала подтвердите доступный остаток и план обычных расходов.',
            'date_changed': 'После последней сверки наступил новый день. Подтвердите текущий остаток.',
            'ledger_changed': 'После сверки изменились записи бюджета. Подтвердите текущий остаток.',
            'overdue_outflows': ('У расходов или взносов в накопления прошел срок. Уточните планы: '
                                 'уберите выполненные или отмененные; для будущего платежа создайте новый план с актуальной датой.'),
            'invalid_events': 'У планов некорректная дата. Уберите их из «Планов по датам» и при необходимости создайте заново.',
        }.get(report['stale_reason'], 'Сверьте текущий остаток, чтобы обновить прогноз.')
        text += reason + ('\nЧисловой прогноз появится после уточнения планов.\n'
                          if report['stale_reason'] in {'overdue_outflows', 'invalid_events'}
                          else '\nЧисловой прогноз появится после сверки.\n')
    else:
        text += f"Сейчас после дополнительного запаса: {money(report['available_after_buffer_minor'])}\n"
        if report['next_income_on']:
            last = report['rows'][-1]
            text += (f"Ближайшее ожидаемое поступление: {day_label(report['next_income_on'])}\n"
                     f"В этот день до поступления, с учетом расходов: {money(last['before_income_minor'])}\n"
                     f"После поступления, если оно придет: {money(last['end_minor'])}\n")
        else:
            text += (f"Поступления не заданы в горизонте: план на 30 дней, по {day_label(report['horizon_end'])}.\n"
                     f"Остаток в конце периода: {money(report['rows'][-1]['end_minor'])}\n")
        if report['first_shortfall_on']:
            text += f"⚠️ Первое возможное снижение ниже выбранного запаса: {day_label(report['first_shortfall_on'])}.\n"
        else:
            text += 'По внесенному плану остаток не опускается ниже выбранного запаса.\n'
        text += 'Внутри дня расходы учитываются раньше поступлений; проверьте время платежей.\n'
    if report.get('overdue_events'):
        text += (f"\nСрок прошел у планов: {len(report['overdue_events'])}. Уточните их в «Планах по датам». "
                 'Непоступившие доходы не учитываются; расходы и взносы с прошедшим сроком требуют уточнения для расчета.\n')
    if report.get('outside_horizon_events'):
        text += f"За пределами показанного периода: {len(report['outside_horizon_events'])} планов.\n"
    if report.get('invalid_events'):
        text += f"Планы с некорректной датой: {len(report['invalid_events'])}. Расчет приостановлен — уберите их и задайте заново в «Планах по датам».\n"
    if report.get('recurring_items'):
        text += '\nРегулярные неоплаченные платежи из раздела «Платежи» учтены автоматически; просроченные — сегодня. Не добавляйте их повторно.\n'
    text += await monthly_savings_note(db, budget_id)
    text += '\n' + DISCLAIMER
    await message.answer(text, reply_markup=await keyboard(db, message, [
        [('💰 Сверить остаток', 'sync'), ('⚙️ Настроить расходы', 'setup')],
        [('➕ Ожидаемое поступление', 'new:income')],
        [('🗓 Планы по датам', 'events:0'), ('📅 По дням', 'days:0')],
    ]))


async def show_days(message, db, budget_id, page=0):
    report = await cashflow.forecast(db, budget_id, today(db.timezone))
    if report['stale']:
        await show_home(message, db, budget_id)
        return
    rows = report['rows']
    page = min(page, max((len(rows) - 1) // 5, 0))
    text = '📅 Прогноз по дням\nСуммы после дополнительного запаса.\n'
    for row in rows[page * 5:(page + 1) * 5]:
        text += (f"\n{day_label(row['date'])}\n"
                 f"Обычные траты: {money(row['daily_minor'])}; платежи и разовые расходы: {money(row['expense_minor'])}\n"
                 f"На накопления: {money(row['saving_minor'])}\n"
                 f"До поступлений: {money(row['before_income_minor'])}\n")
        if row['income_minor']:
            text += f"Ожидается: +{money(row['income_minor'])}; если придет: {money(row['end_minor'])}\n"
        else:
            text += f"Остаток: {money(row['end_minor'])}\n"
    text += '\nРасходы этого дня посчитаны до доходов. ' + DISCLAIMER
    nav = []
    if page:
        nav.append(('‹ Раньше', f'days:{page - 1}'))
    if (page + 1) * 5 < len(rows):
        nav.append(('Позже ›', f'days:{page + 1}'))
    buttons = [nav] if nav else []
    buttons.append([('К прогнозу', 'home')])
    await message.answer(text, reply_markup=await keyboard(db, message, buttons))


async def show_events(message, db, budget_id, page=0):
    events = await cashflow.list_events(db, budget_id)
    page = min(page, max((len(events) - 1) // 8, 0))
    text = ('🗓 Планы по датам\n\n'
            'Ожидаемые поступления, разовые расходы и взносы в накопления. Они не добавляют операции в историю.\n'
            'Регулярные платежи задаются в /payments и учитываются отдельно.\n')
    if not events:
        text += '\nПока нет разовых планов.'
    rows = [[('➕ Поступление', 'new:income'), ('➖ Разовый расход', 'new:expense')],
            [('🌱 Взнос в накопления', 'new:saving')]]
    as_of = today(db.timezone).isoformat()
    for event in events[page * 8:(page + 1) * 8]:
        past = '⏳ ' if event['due_on'] < as_of else ''
        rows.append([(f"{past}{ICONS[event['kind']]} {day_label(event['due_on'])} · {event['name'][:35]}", f"event:{event['id']}")])
    nav = []
    if page:
        nav.append(('‹ Назад', f'events:{page - 1}'))
    if (page + 1) * 8 < len(events):
        nav.append(('Далее ›', f'events:{page + 1}'))
    if nav:
        rows.append(nav)
    rows.append([('К прогнозу', 'home')])
    await message.answer(text, reply_markup=await keyboard(db, message, rows))


def event_text(event):
    return (f"{ICONS[event['kind']]} {KINDS[event['kind']]}\n{event['name']}\n"
            f"Сумма: {money(event['amount_minor'])}\nДата: {day_label(event['due_on'])}")


async def show_event(message, db, budget_id, event_id):
    event = await cashflow.get_event(db, budget_id, event_id)
    if not event or event['is_closed']:
        await message.answer('План не найден или уже убран. Откройте «Планы по датам».')
        return
    text = event_text(event)
    if event['due_on'] < today(db.timezone).isoformat():
        text += '\n\nСрок прошел, уточните план. '
        text += ('Ожидаемый доход не считается полученным и не входит в прогноз.' if event['kind'] == 'income'
                 else 'Пока не уточнен расход или взнос, расчет прогноза приостановлен.')
    text += ('\n\nЧтобы изменить сумму или дату, уберите этот план и создайте новый. '
             'Если событие уже произошло, уберите план, отдельно запишите фактический доход или расход и сверьте остаток. '
             'Перевод в собственные накопления не записывайте как расход.')
    await message.answer(text, reply_markup=await keyboard(db, message, [
        [('Убрать из прогноза', f"close:{event['id']}")], [('Все планы', 'events:0')],
    ]))


async def start_profile(message, state, db, budget_id, *, quick=False):
    existing = await cashflow.get_profile(db, budget_id)
    await state.clear()
    await state.update_data(forecast_draft={
        'kind': 'profile', 'token': secrets.token_hex(4), 'values': {},
        'version': existing['version'] if existing else None,
        'quick': bool(quick and existing),
        'daily_minor': existing['daily_minor'] if existing else 0,
        'buffer_minor': existing['buffer_minor'] if existing else 0,
    })
    await state.set_state(ForecastForm.balance)
    await message.answer(BALANCE_PROMPT, reply_markup=CANCEL_MENU)


async def start_event(message, state, db, budget_id, kind):
    await state.clear()
    await state.update_data(forecast_draft={
        'kind': 'event', 'token': secrets.token_hex(4), 'values': {'kind': kind},
    })
    await state.set_state(ForecastForm.event_name)
    await message.answer(f'{KINDS[kind]}: как назовем? Одна строка, до 80 символов.\n/cancel — отмена.', reply_markup=CANCEL_MENU)


async def confirm(message, state, db):
    draft = (await state.get_data())['forecast_draft']
    values = draft['values']
    if draft['kind'] == 'profile':
        text = (f"Подтвердите сверку на {day_label(values['confirmed_on'])}:\n"
                f"Доступные деньги сейчас: {money(values['balance_minor'])}\n"
                f"Обычные траты: {money(values['daily_minor'])} / день\n"
                f"Дополнительный неприкосновенный запас: {money(values['buffer_minor'])}\n"
                f"Остаток после запаса: {money(values['balance_minor'] - values['buffer_minor'])}\n\n"
                'Указан текущий остаток после уже совершенных покупок. Кредитные лимиты и отдельно отложенные деньги исключены. '
                'Сегодня в прогноз войдет полный выбранный дневной расход; можно задать 0 и внести оставшиеся траты по датам. '
                'Сверка не меняет историю операций.')
    elif draft['kind'] == 'close':
        text = (f"Убрать план «{draft['name']}» из прогноза?\n\n"
                'Это не подтверждает поступление денег или оплату. Запись в истории не создается. '
                'Фактические операции внесите отдельно и сверьте остаток; перевод между своими счетами не является расходом.')
    else:
        text = event_text(values) + '\n\nДобавить в прогноз? Это только план, без записи дохода, расхода или перевода денег.'
        if values['kind'] == 'income':
            text += '\nОжидаемое поступление не гарантировано.'
    await state.set_state(ForecastForm.confirm)
    await message.answer(text, reply_markup=await keyboard(db, message, [
        [('✅ Сохранить', f"confirm:{draft['token']}")], [('Отмена', f"cancel:{draft['token']}")],
    ]))


async def handle_message(message: Message, state: FSMContext, db, budget_id: int) -> bool:
    text = (message.text or '').strip()
    command = text.split('@', 1)[0].split(' ', 1)[0]
    if command == '/forecast' or text == '🔮 Прогноз':
        await state.clear()
        await show_home(message, db, budget_id)
        return True
    current = await state.get_state()
    if not current or not current.startswith('ForecastForm:'):
        return False
    try:
        draft = (await state.get_data()).get('forecast_draft')
        if not draft:
            await state.clear()
            await show_home(message, db, budget_id)
            return True
        values = draft['values']
        if current == ForecastForm.confirm.state:
            await message.answer('Проверьте суммы и нажмите «Сохранить». /cancel — отмена.')
            return True
        if current == ForecastForm.balance.state:
            values['balance_minor'] = amount(text, signed=True)
            values['confirmed_on'] = today(db.timezone).isoformat()
            draft['ledger_fingerprint'] = await cashflow.ledger_fingerprint(db, budget_id)
            if draft['quick']:
                values.update(daily_minor=draft['daily_minor'], buffer_minor=draft['buffer_minor'])
                await state.update_data(forecast_draft=draft)
                await confirm(message, state, db)
                return True
            await state.set_state(ForecastForm.daily)
            prompt = DAILY_PROMPT
        elif current == ForecastForm.daily.state:
            values['daily_minor'] = amount(text)
            await state.set_state(ForecastForm.buffer)
            prompt = BUFFER_PROMPT
        elif current == ForecastForm.buffer.state:
            values['buffer_minor'] = amount(text)
            await state.update_data(forecast_draft=draft)
            await confirm(message, state, db)
            return True
        elif current == ForecastForm.event_name.state:
            if not text or len(text) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in text):
                raise ValueError('Название — одна строка, от 1 до 80 символов.')
            values['name'] = text
            await state.set_state(ForecastForm.event_amount)
            prompt = 'Какая сумма, ₽? Введите число больше 0.'
            if values['kind'] == 'income':
                prompt += '\nУкажите ожидаемую сумму после налогов; она еще не считается полученной.'
        elif current == ForecastForm.event_amount.state:
            values['amount_minor'] = amount(text, positive=True)
            await state.set_state(ForecastForm.event_date)
            prompt = ('На какую дату планируете? ДД.ММ.ГГГГ, с сегодня до 89 дней вперед.\n'
                      f"Например: {today(db.timezone).strftime('%d.%m.%Y')}.")
        elif current == ForecastForm.event_date.state:
            values['due_on'] = event_date(text, today(db.timezone))
            await state.update_data(forecast_draft=draft)
            await confirm(message, state, db)
            return True
        else:
            raise ValueError('Откройте /forecast заново, чтобы продолжить.')
        await state.update_data(forecast_draft=draft)
        await message.answer(prompt)
    except (ValueError, KeyError) as exc:
        await message.answer(str(exc) if isinstance(exc, ValueError) else 'Не удалось продолжить ввод. Откройте /forecast заново.')
    return True


async def handle_callback(callback: CallbackQuery, state: FSMContext, db, budget_id: int, data: str, *, scoped: bool) -> bool:
    if not data.startswith('cf:'):
        return False
    if not scoped:
        await callback.answer('Откройте прогноз заново: /forecast')
        return True
    message, parts = callback.message, data.split(':')
    try:
        action = parts[1]
        no_argument = {'home', 'sync', 'setup'}
        with_argument = {'events', 'days', 'event', 'new', 'close', 'confirm', 'cancel'}
        if action not in no_argument | with_argument or len(parts) != (2 if action in no_argument else 3):
            raise ValueError('Кнопка устарела. Откройте /forecast заново.')
        if action in {'event', 'close'} and (not re.fullmatch(r'\d{1,19}', parts[2]) or not 0 < int(parts[2]) < 2**63):
            raise ValueError('Некорректный номер плана.')
        if action in {'events', 'days'} and (not re.fullmatch(r'\d{1,3}', parts[2]) or not 0 <= int(parts[2]) <= 100):
            raise ValueError('Некорректная страница.')
        if action == 'new' and parts[2] not in KINDS:
            raise ValueError('Некорректный вид плана.')
        if action in {'confirm', 'cancel'}:
            draft = (await state.get_data()).get('forecast_draft', {})
            if not re.fullmatch(r'[0-9a-f]{8}', parts[2]) or draft.get('token') != parts[2] or await state.get_state() != ForecastForm.confirm.state:
                await callback.answer('Это подтверждение уже закрыто. Откройте прогноз заново.')
                return True
            if action == 'cancel':
                await state.clear()
                await message.answer('Изменения отменены.', reply_markup=MAIN_MENU)
            else:
                values, kind = draft['values'], draft['kind']
                ok = True
                if kind == 'profile':
                    if values['confirmed_on'] != today(db.timezone).isoformat():
                        await state.clear()
                        await message.answer('Начался новый день. Сверьте остаток заново: /forecast', reply_markup=MAIN_MENU)
                        await callback.answer()
                        return True
                    ok = await cashflow.set_profile(db, budget_id, expected_version=draft['version'],
                                                    expected_ledger_fingerprint=draft['ledger_fingerprint'], **values)
                elif kind == 'event':
                    # Persisted forms can cross a date boundary: validate again at confirmation.
                    event_date(day_label(values['due_on']), today(db.timezone))
                    await cashflow.add_event(db, budget_id, create_key=draft['token'], **values)
                elif kind == 'close':
                    ok = await cashflow.close_event(db, budget_id, draft['event_id'], expected_version=draft['version'])
                else:
                    raise ValueError('План устарел. Откройте /forecast заново.')
                await state.clear()
                if not ok:
                    await message.answer('План или записи бюджета уже изменились. Откройте актуальные данные и повторите сверку или изменение.', reply_markup=MAIN_MENU)
                else:
                    await message.answer('Сохранено. Денежные операции не создавались.', reply_markup=MAIN_MENU)
                    if kind == 'profile':
                        await show_home(message, db, budget_id)
                    else:
                        await show_events(message, db, budget_id)
        else:
            await state.clear()
            if action == 'home':
                await show_home(message, db, budget_id)
            elif action in {'sync', 'setup'}:
                await start_profile(message, state, db, budget_id, quick=action == 'sync')
            elif action == 'days':
                await show_days(message, db, budget_id, int(parts[2]))
            elif action == 'events':
                await show_events(message, db, budget_id, int(parts[2]))
            elif action == 'event':
                await show_event(message, db, budget_id, int(parts[2]))
            elif action == 'new':
                await start_event(message, state, db, budget_id, parts[2])
            elif action == 'close':
                event = await cashflow.get_event(db, budget_id, int(parts[2]))
                if not event or event['is_closed']:
                    await callback.answer('План не найден или уже убран.')
                    return True
                await state.update_data(forecast_draft={
                    'kind': 'close', 'token': secrets.token_hex(4), 'values': {},
                    'event_id': event['id'], 'version': event['version'], 'name': event['name'],
                })
                await confirm(message, state, db)
        await callback.answer()
    except (ValueError, IndexError, KeyError) as exc:
        await callback.answer(str(exc)[:190] if isinstance(exc, ValueError) and str(exc) else 'Кнопка устарела. Откройте /forecast заново.')
    return True
