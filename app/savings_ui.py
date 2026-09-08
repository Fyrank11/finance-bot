"""Phone-first savings plans: user-chosen amounts, no financial product selection."""
from __future__ import annotations

import re
import secrets
from datetime import date
from pathlib import Path

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from . import family, savings
from .coaching import _money as money
from .inputs import month_label, parse_amount, shift_month, to_minor, today, valid_month
from .keyboards import CANCEL_MENU, MAIN_MENU, inline

POSTER = Path(__file__).parent / 'assets' / 'savings-poster.png'
NOTICE = 'Расчет без доходности и изменения стоимости цели. Размер взноса и место для накоплений выбираете вы.'
PRACTICES = (
    '🌱 Привычки накоплений\n\n'
    '1. У каждой цели — своя сумма и срок. Одни деньги не учитывайте одновременно в резерве и на другую цель.\n\n'
    '2. Выбирайте посильный взнос. Учтите обычные траты, платежи по долгам и известные нерегулярные расходы.\n\n'
    '3. Резерв — отдельно. 3–6 месяцев необходимых расходов — распространенный ориентир, а не обязательная норма. '
    'Размер зависит от устойчивости дохода и обстоятельств семьи; начать можно с небольшой суммы.\n\n'
    '4. Регулярность помогает. Сверяйте взнос с датами поступлений и платежей. '
    'Неполученную премию не считайте гарантированным доходом.\n\n'
    '5. Пересматривайте план, когда меняются доходы, расходы или цели.\n\n'
    'Общие принципы планирования, без подбора финансовых продуктов.\n'
    'Источники:\nhttps://www.consumerfinance.gov/an-essential-guide-to-building-an-emergency-fund/\n'
    'https://www.moneyhelper.org.uk/en/savings/types-of-savings/emergency-savings-how-much-is-enough'
)


class SavingsForm(StatesGroup):
    goal_name = State()
    goal_target = State()
    goal_saved = State()
    goal_deadline = State()
    goal_monthly = State()
    saved = State()
    budget = State()
    reserve = State()
    confirm = State()


BUDGET_FIELDS = (
    ('income_minor', 'Какой доход после налогов вы закладываете на месяц?\nНе включайте неполученные разовые премии и заемные деньги. Можно 0.'),
    ('expenses_minor', 'Сколько планируете на ВСЕ обычные расходы за месяц?\nВключите жилье, еду, транспорт, выбранные траты и обязательные платежи по долгам. Накопления сюда не включайте.'),
    ('irregular_minor', 'Сколько в месяц отдельно выделяете на известные нерегулярные траты: страховку, ремонт, годовые платежи?\nНе повторяйте суммы из обычных расходов или целей в боте. Можно 0.'),
    ('other_savings_minor', 'Сколько в месяц уже направляете на другие накопления ВНЕ целей и резерва в этом разделе?\nПланы в боте будут учтены отдельно. Можно 0.'),
    ('buffer_minor', 'Какую дополнительную сумму хотите оставлять в месячном бюджете нераспределенной?\nЭто запас на неточность плана, отдельно от уже учтенных расходов и резерва. Можно 0.'),
)
RESERVE_FIELDS = (
    ('essential_minor', 'Сколько составляют необходимые расходы за месяц?\nВключите жилье, продукты, лечение и обязательные платежи по долгам. Это ваша оценка; сумма должна быть больше 0.'),
    ('months', 'На сколько месяцев необходимых расходов вы хотите сформировать резерв?\nВведите число от 1 до 36. Часто используют ориентир 3–6 месяцев, но период выбираете вы.'),
    ('saved_minor', 'Сколько уже выделено ТОЛЬКО в резерв?\nНе включайте деньги, которые отмечены на другие цели. Можно 0.'),
    ('monthly_minor', 'Какой ежемесячный взнос в резерв вы выбираете?\nМожно 0, если пока только рассчитываете его размер.'),
)


async def keyboard(db, message, rows):
    budget_id, revision = await family.active_budget_context(db, message.chat.id)
    guides = {'guide:savings', 'guide:goals', 'guide:reserve', 'guide:savings_budget'}
    return inline([[(label, action if action in guides else f'scope:{budget_id}:{revision}:sav:{action}')
                    for label, action in row] for row in rows])


def amount(text: str, *, positive: bool = False) -> int:
    return to_minor(parse_amount(text, allow_zero=not positive), allow_zero=not positive)


def deadline(text: str, as_of: date) -> str:
    if re.fullmatch(r'\d{1,3}', text):
        months = int(text)
        if not 1 <= months <= 600:
            raise ValueError('Введите от 1 до 600 месяцев.')
        return shift_month(as_of.strftime('%Y-%m'), months - 1)
    match = re.fullmatch(r'(\d{2})\.(\d{4})', text)
    if not match:
        raise ValueError('Введите количество месяцев, например 18, или месяц цели: ММ.ГГГГ.')
    result = valid_month(f'{match[2]}-{match[1]}')
    distance = (int(result[:4]) - as_of.year) * 12 + int(result[5:]) - as_of.month + 1
    if not 1 <= distance <= 600:
        raise ValueError('Месяц цели должен быть от текущего до 600 месяцев вперед.')
    return result


def goal_text(goal: dict, as_of: date) -> str:
    plan = savings.goal_plan(goal, as_of)
    term = month_label(goal['due_month']) if goal.get('due_month') else 'не задан'
    text = (f"🎯 {goal['name']}\nЦель: {money(goal['target_minor'])}\n"
            f"Уже отмечено: {money(goal['saved_minor'])}\nОсталось: {money(plan['remaining_minor'])}\n"
            f"Месяц цели: {term}\nВаш взнос: {money(goal['monthly_minor'])} / месяц")
    if not plan['remaining_minor']:
        text += '\n✅ По отмеченной сумме цель достигнута. Ее взнос больше не вычитается из месячного плана.'
    elif plan['months_left'] is None:
        text += '\nЗадайте срок, чтобы увидеть необходимый ежемесячный взнос.'
    elif plan['months_left'] == 0:
        text += '\nМесяц цели уже прошел. Уточните накопленную сумму или измените срок.'
    else:
        text += (f"\nДля срока нужно: {money(plan['required_minor'])} / месяц"
                 f"\nВ расчет включены текущий и целевой месяцы: {plan['months_left']} взносов.")
    if plan['remaining_minor'] and plan['chosen_months']:
        text += f"\nПри выбранном взносе осталось: {plan['chosen_months']} ежемесячных взносов."
    return text + '\n\n' + NOTICE


async def show_home(message, db, budget_id):
    as_of = today(db.timezone)
    goals = await savings.list_goals(db, budget_id)
    reserve = await savings.get_reserve(db, budget_id)
    budget = await savings.get_budget_plan(db, budget_id)
    capacity = savings.capacity_plan(budget, goals, reserve, as_of)
    text = (f"🌱 Накопления и цели · {'семейный' if budget_id < 0 else 'личный'} бюджет\n\n"
            'Здесь можно рассчитать взнос для цели, размер резерва и сопоставить планы с возможностями бюджета.\n\n')
    active = [g for g in goals if savings.goal_plan(g, as_of)['remaining_minor']]
    text += f'Незавершенных целей: {len(active)}\n'
    if capacity:
        text += (f"По подтвержденному плану до взносов: {money(capacity['capacity_minor'])} / месяц\n"
                 f"Выбрано на цели и резерв: {money(capacity['committed_minor'])} / месяц\n"
                 f"После выбранных взносов: {money(capacity['unassigned_minor'])} / месяц\n")
        if capacity['unassigned_minor'] < 0:
            text += 'План не сходится. Сравните другой взнос, срок или сумму цели.\n'
    else:
        text += 'Возможности бюджета: план еще не подтвержден.\n'
    text += '\nЗаписи накоплений не создают доход или расход и не переводят деньги. Не учитывайте одни деньги на нескольких целях.'
    await message.answer(text, reply_markup=await keyboard(db, message, [
        [('🎯 Мои цели', 'goals:0'), ('🛟 Резерв', 'reserve')],
        [('🧮 Возможности бюджета', 'budget')], [('💡 Привычки накоплений', 'habits')],
        [('🖼 Постер', 'poster')],
        [('ℹ️ Как это работает', 'guide:savings')],
    ]))


async def show_goals(message, db, budget_id, offset=0):
    goals = await savings.list_goals(db, budget_id)
    rows = [[('➕ Новая цель', 'new')]]
    for goal in goals[offset:offset + 8]:
        done = '✅ ' if goal['saved_minor'] >= goal['target_minor'] else ''
        rows.append([(done + goal['name'][:45], f"goal:{goal['id']}")])
    nav = []
    if offset: nav.append(('‹ Назад', f'goals:{max(0, offset - 8)}'))
    if offset + 8 < len(goals): nav.append(('Далее ›', f'goals:{offset + 8}'))
    if nav: rows.append(nav)
    rows.append([('К накоплениям', 'home')])
    rows.append([('ℹ️ Как это работает', 'guide:goals')])
    await message.answer('🎯 Мои цели\n' + ('Выберите цель или создайте новую.' if goals else 'Пока нет целей. Начните с суммы и срока.'), reply_markup=await keyboard(db, message, rows))


async def show_goal(message, db, budget_id, goal_id):
    goal = await savings.get_goal(db, budget_id, goal_id)
    if not goal or goal['is_archived']:
        await message.answer('Цель не найдена или уже в архиве. Откройте «Мои цели».')
        return
    await message.answer(goal_text(goal, today(db.timezone)), reply_markup=await keyboard(db, message, [
        [('Обновить накопленное', f'saved:{goal_id}')], [('Изменить план', f'edit:{goal_id}')],
        [('В архив', f'archive:{goal_id}'), ('Все цели', 'goals:0')],
        [('ℹ️ Как это работает', 'guide:goals')],
    ]))


async def show_budget(message, db, budget_id):
    row = await savings.get_budget_plan(db, budget_id)
    text = '🧮 Возможности бюджета\n\n'
    if row:
        capacity = savings.capacity_plan(row, await savings.list_goals(db, budget_id), await savings.get_reserve(db, budget_id), today(db.timezone))
        text += (f"План подтвержден: {row['confirmed_on']}\n"
                 f"Доход после налогов: {money(row['income_minor'])}\n"
                 f"Обычные расходы и платежи по долгам: {money(row['expenses_minor'])}\n"
                 f"На нерегулярные траты: {money(row['irregular_minor'])}\n"
                 f"Другие накопления вне раздела: {money(row['other_savings_minor'])}\n"
                 f"Нераспределенный запас: {money(row['buffer_minor'])}\n\n"
                 f"До взносов на цели и резерв: {money(capacity['capacity_minor'])}\n"
                 f"Выбрано на цели и резерв: {money(capacity['committed_minor'])}\n"
                 f"После взносов: {money(capacity['unassigned_minor'])} / месяц\n\n")
        if (today(db.timezone) - date.fromisoformat(row['confirmed_on'])).days > 31:
            text += 'План подтвержден больше месяца назад. Сверьте, не изменились ли суммы.\n\n'
    else:
        text += 'Нужны пять сумм вашего месячного плана. Несколько операций в боте не подтверждают полную картину доходов и расходов.\n\n'
    text += ('Это план, а не доступный баланс счета. Даже при положительном остатке сверяйте даты платежей и поступлений. '
             'При нестабильном доходе не рассчитывайте на неполученные премии. Все суммы можно изменить.')
    await message.answer(text, reply_markup=await keyboard(db, message, [[('Задать / изменить план', 'budget_edit')], [('К накоплениям', 'home')], [('ℹ️ Как это работает', 'guide:savings_budget')]]))


async def show_reserve(message, db, budget_id):
    row = await savings.get_reserve(db, budget_id)
    text = '🛟 Резерв\n\n'
    if row:
        plan = savings.reserve_plan(row)
        text += (f"Необходимые расходы: {money(row['essential_minor'])} / месяц\n"
                 f"Выбранный период: {row['months']} мес.\nРазмер резерва: {money(plan['target_minor'])}\n"
                 f"Уже отмечено: {money(row['saved_minor'])}\nОсталось: {money(plan['remaining_minor'])}\n"
                 f"Ваш взнос: {money(row['monthly_minor'])} / месяц\n")
        if plan['remaining_minor'] and plan['chosen_months']:
            text += f"При этом взносе осталось: {plan['chosen_months']} месяцев.\n"
        elif not plan['remaining_minor']:
            text += '✅ Выбранный размер достигнут. Взнос в резерв не вычитается из месячного плана.\n'
        text += f"Обновлено: {row['updated_on']}\n\n"
    text += ('Период резерва выбираете вы. 3–6 месяцев необходимых расходов — распространенный ориентир, а не норма для всех. '
             'Не учитывайте здесь деньги на другие цели. Расчет без доходности и роста расходов.')
    await message.answer(text, reply_markup=await keyboard(db, message, [[('Задать / изменить резерв', 'reserve_edit')], [('К накоплениям', 'home')], [('ℹ️ Как это работает', 'guide:reserve')]]))


async def start_wizard(message, state, db, budget_id, kind, existing=None):
    await state.clear()
    draft = {'kind': kind, 'token': secrets.token_hex(4), 'values': {}, 'version': existing.get('version') if existing else None}
    if existing and kind == 'goal': draft['goal_id'] = existing['id']
    await state.update_data(savings_draft=draft)
    if kind == 'goal':
        await state.set_state(SavingsForm.goal_name)
        prompt = 'Как назовем цель? Одна строка, до 120 символов.\nНапример: отпуск. /cancel — отмена.'
        if existing: prompt = 'Изменение плана: заново подтвердим пять полей.\nСейчас: ' + existing['name'] + '\n\n' + prompt
    else:
        await state.set_state(SavingsForm.budget if kind == 'budget' else SavingsForm.reserve)
        prompt = (BUDGET_FIELDS if kind == 'budget' else RESERVE_FIELDS)[0][1]
    await message.answer(prompt, reply_markup=CANCEL_MENU)


async def confirm(message, state, db):
    draft = (await state.get_data())['savings_draft']
    values, kind = draft['values'], draft['kind']
    if kind == 'goal':
        text = goal_text(values, today(db.timezone))
    elif kind == 'saved': text = f"Отметить на цели «{draft['name']}»: {money(values['saved_minor'])}?\nЭто заменит отмеченную сумму; перевод денег и расход не создаются."
    elif kind == 'archive': text = f"Убрать «{draft['name']}» в архив?\nОна перестанет учитываться в плане. История расходов не изменится."
    elif kind == 'budget':
        available = values['income_minor'] - sum(values[k] for k in ('expenses_minor', 'irregular_minor', 'other_savings_minor', 'buffer_minor'))
        text = (f"Подтвердите месячный план:\nДоход: {money(values['income_minor'])}\nРасходы: {money(values['expenses_minor'])}\n"
                f"Нерегулярные траты: {money(values['irregular_minor'])}\nДругие накопления: {money(values['other_savings_minor'])}\n"
                f"Нераспределенный запас: {money(values['buffer_minor'])}\nДо взносов на цели и резерв: {money(available)}\n\n"
                'Цели и резерв в боте будут вычтены отдельно. Эти суммы не добавляются в историю операций.')
    else:
        plan = savings.reserve_plan(values)
        text = (f"Подтвердите резерв:\nРасходы: {money(values['essential_minor'])} / месяц\nПериод: {values['months']} мес.\n"
                f"Размер: {money(plan['target_minor'])}\nУже отмечено: {money(values['saved_minor'])}\n"
                f"Ваш взнос: {money(values['monthly_minor'])} / месяц\n\nОдни деньги не должны одновременно входить в резерв и другие цели.")
    await state.set_state(SavingsForm.confirm)
    await message.answer(text, reply_markup=await keyboard(db, message, [[('✅ Сохранить', f"confirm:{draft['token']}")], [('Отмена', f"cancel:{draft['token']}")]]))


async def handle_message(message: Message, state: FSMContext, db, budget_id: int) -> bool:
    text = (message.text or '').strip()
    command = text.split('@', 1)[0].split(' ', 1)[0]
    if command == '/savings' or text in ('🌱 Накопления', '🌱 Накопления и цели', '🧭 Распределение'):
        await state.clear()
        await show_home(message, db, budget_id)
        return True
    if text in ('🎯 Цель', '🎯 Мои цели'):
        await state.clear()
        await show_goals(message, db, budget_id)
        return True
    current = await state.get_state()
    if not current or not current.startswith('SavingsForm:'):
        return False
    try:
        draft = (await state.get_data()).get('savings_draft')
        if not draft:
            await state.clear()
            await show_home(message, db, budget_id)
            return True
        values = draft['values']
        if current == SavingsForm.confirm.state:
            await message.answer('Проверьте план и нажмите «Сохранить». /cancel — отмена.')
            return True
        if current == SavingsForm.goal_name.state:
            if not text or len(text) > 120 or any(ord(c) < 32 for c in text):
                raise ValueError('Название — одна строка, от 1 до 120 символов.')
            values['name'] = text
            await state.set_state(SavingsForm.goal_target)
            prompt = 'Какая сумма нужна на цель, ₽?\nВведите сумму в сегодняшней оценке; при изменении цены план можно обновить.'
        elif current == SavingsForm.goal_target.state:
            values['target_minor'] = amount(text, positive=True)
            await state.set_state(SavingsForm.goal_saved)
            prompt = 'Сколько уже выделено только на эту цель, ₽?\nБез резерва и денег на другие цели. Можно 0.'
        elif current == SavingsForm.goal_saved.state:
            values['saved_minor'] = amount(text)
            await state.set_state(SavingsForm.goal_deadline)
            prompt = 'На сколько месяцев план? Например: 18.\nЛибо введите месяц цели: ММ.ГГГГ. В расчет включены текущий и целевой месяцы; срок — конец целевого месяца.'
        elif current == SavingsForm.goal_deadline.state:
            values['due_month'] = deadline(text, today(db.timezone))
            values['monthly_minor'] = 0
            required = savings.goal_plan(values, today(db.timezone))['required_minor']
            await state.set_state(SavingsForm.goal_monthly)
            prompt = f'Для указанного срока получается {money(required)} в месяц.\nКакой ежемесячный взнос выбираете вы? Можно 0, чтобы решить позже.\n' + NOTICE
        elif current == SavingsForm.goal_monthly.state:
            values['monthly_minor'] = amount(text)
            await state.update_data(savings_draft=draft)
            await confirm(message, state, db)
            return True
        elif current == SavingsForm.saved.state:
            values['saved_minor'] = amount(text)
            await state.update_data(savings_draft=draft)
            await confirm(message, state, db)
            return True
        else:
            fields = BUDGET_FIELDS if current == SavingsForm.budget.state else RESERVE_FIELDS
            key = fields[len(values)][0]
            if key == 'months':
                if not re.fullmatch(r'\d{1,2}', text) or not 1 <= int(text) <= 36:
                    raise ValueError('Введите целое число от 1 до 36.')
                values[key] = int(text)
            else:
                values[key] = amount(text, positive=key == 'essential_minor')
            await state.update_data(savings_draft=draft)
            if len(values) == len(fields):
                await confirm(message, state, db)
                return True
            prompt = fields[len(values)][1]
        await state.update_data(savings_draft=draft)
        await message.answer(prompt)
    except (ValueError, KeyError, IndexError) as exc:
        await message.answer(str(exc) if isinstance(exc, ValueError) else 'Не удалось продолжить ввод. Откройте /savings заново.')
    return True


async def handle_callback(callback: CallbackQuery, state: FSMContext, db, budget_id: int, data: str, *, scoped: bool) -> bool:
    if not data.startswith('sav:'):
        return False
    if not scoped:
        await callback.answer('Откройте «Накопления и цели» заново: /savings')
        return True
    message, parts = callback.message, data.split(':')
    try:
        action = parts[1]
        with_argument = {'confirm', 'cancel', 'goals', 'goal', 'edit', 'saved', 'archive'}
        no_argument = {'home', 'new', 'budget', 'reserve', 'budget_edit', 'reserve_edit', 'habits', 'poster'}
        if action not in with_argument | no_argument or len(parts) != (3 if action in with_argument else 2):
            raise ValueError('Кнопка устарела. Откройте /savings заново.')
        if action in {'goal', 'edit', 'saved', 'archive'} and (not parts[2].isdigit() or not 0 < int(parts[2]) < 2**63):
            raise ValueError('Некорректный номер цели.')
        if action == 'goals' and (not parts[2].isdigit() or not 0 <= int(parts[2]) <= 50):
            raise ValueError('Некорректная страница целей.')
        if action in ('confirm', 'cancel'):
            draft = (await state.get_data()).get('savings_draft', {})
            if len(parts) != 3 or draft.get('token') != parts[2] or await state.get_state() != SavingsForm.confirm.state:
                await callback.answer('Это подтверждение уже закрыто. Откройте план заново.')
                return True
            if action == 'cancel':
                await state.clear()
                await message.answer('Изменения отменены.', reply_markup=MAIN_MENU)
            else:
                kind, values = draft['kind'], draft['values']
                ok = True
                if kind == 'goal':
                    if draft.get('goal_id'):
                        goal_id = draft['goal_id']
                        ok = await savings.update_goal(db, budget_id, goal_id, expected_version=draft['version'], **values)
                    else:
                        goal_id = await savings.create_goal(db, budget_id, create_key=draft['token'], **values)
                elif kind in ('saved', 'archive'):
                    goal_id = draft['goal_id']
                    ok = await savings.update_goal(db, budget_id, goal_id, expected_version=draft['version'], **values)
                elif kind == 'budget':
                    ok = await savings.set_budget_plan(db, budget_id, expected_version=draft['version'], confirmed_on=today(db.timezone).isoformat(), **values)
                else:
                    ok = await savings.set_reserve(db, budget_id, expected_version=draft['version'], updated_on=today(db.timezone).isoformat(), **values)
                await state.clear()
                if not ok:
                    await message.answer('План уже изменен другим действием. Откройте актуальные данные и повторите изменение.', reply_markup=MAIN_MENU)
                else:
                    await message.answer('План сохранен. Денежные операции не создавались.', reply_markup=MAIN_MENU)
                    if kind in ('goal', 'saved'): await show_goal(message, db, budget_id, goal_id)
                    elif kind == 'reserve': await show_reserve(message, db, budget_id)
                    elif kind == 'budget': await show_budget(message, db, budget_id)
                    else: await show_goals(message, db, budget_id)
        else:
            await state.clear()
            if action == 'home': await show_home(message, db, budget_id)
            elif action == 'goals': await show_goals(message, db, budget_id, max(0, int(parts[2])))
            elif action == 'goal': await show_goal(message, db, budget_id, int(parts[2]))
            elif action == 'new': await start_wizard(message, state, db, budget_id, 'goal')
            elif action == 'budget': await show_budget(message, db, budget_id)
            elif action == 'reserve': await show_reserve(message, db, budget_id)
            elif action == 'budget_edit': await start_wizard(message, state, db, budget_id, 'budget', await savings.get_budget_plan(db, budget_id))
            elif action == 'reserve_edit': await start_wizard(message, state, db, budget_id, 'reserve', await savings.get_reserve(db, budget_id))
            elif action in ('edit', 'saved', 'archive'):
                goal = await savings.get_goal(db, budget_id, int(parts[2]))
                if not goal or goal['is_archived']:
                    await callback.answer('Цель не найдена или уже в архиве.')
                    return True
                if action == 'edit': await start_wizard(message, state, db, budget_id, 'goal', goal)
                else:
                    draft = {'kind': action, 'token': secrets.token_hex(4), 'goal_id': goal['id'], 'version': goal['version'], 'name': goal['name'], 'values': {}}
                    if action == 'archive': draft['values']['is_archived'] = 1
                    await state.update_data(savings_draft=draft)
                    if action == 'archive': await confirm(message, state, db)
                    else:
                        await state.set_state(SavingsForm.saved)
                        await message.answer(f"Сейчас отмечено: {money(goal['saved_minor'])}.\nСколько всего теперь выделено на эту цель? Введите новую общую сумму, можно 0.", reply_markup=CANCEL_MENU)
            elif action == 'habits':
                await message.answer(PRACTICES, reply_markup=await keyboard(db, message, [[('🖼 Постер', 'poster'), ('К накоплениям', 'home')]]))
            elif action == 'poster':
                await callback.answer()
                await message.answer_photo(FSInputFile(POSTER), caption='🌱 Накопления и цели\nОбщие принципы планирования. Взнос и место для накоплений выбираете вы.', reply_markup=await keyboard(db, message, [[('К накоплениям', 'home')]]))
                return True
            else:
                await callback.answer('Откройте /savings заново.')
                return True
        await callback.answer()
    except (ValueError, IndexError, KeyError) as exc:
        await callback.answer(str(exc)[:190] if isinstance(exc, ValueError) and str(exc) else 'Кнопка устарела. Откройте /savings заново.')
    return True
