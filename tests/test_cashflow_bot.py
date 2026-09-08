"""Cash forecasts through the actual dispatcher, authorization and drafts."""
import asyncio
from datetime import date, timedelta
import sqlite3

from app import bot as app, cashflow, cashflow_ui, family, recurring, savings
from app.db import Database
from app.rate_limit import RateLimiter
from test_bot import ChatHarness


DAY = date(2026, 9, 8)


async def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / 'cashflow-bot.db')
    await db.init()
    monkeypatch.setattr(app, 'db', db, raising=False)
    monkeypatch.setattr(app, 'allowed_user_ids', frozenset({1, 2, 3}))
    monkeypatch.setattr(app, 'public_signup', False)
    monkeypatch.setattr(app, 'rate_limiter', RateLimiter(limit=1000, expensive_limit=100))
    monkeypatch.setattr(app, 'today', lambda timezone: DAY)
    monkeypatch.setattr(cashflow_ui, 'today', lambda timezone: DAY)
    return db, ChatHarness()


def responses(chat, since=0):
    return '\n'.join((getattr(call, 'text', '') or '') for call in chat.session.calls[since:])


def ledger(db):
    with sqlite3.connect(db.path) as conn:
        return conn.execute('SELECT * FROM transactions ORDER BY id').fetchall()


async def close(chat):
    await chat.bot.session.close()
    await chat.dispatcher.storage.close()


async def profile(chat, values=('10000', '1000', '1000'), *, user=1, save=True):
    await chat.text('/forecast', user=user)
    await chat.tap(chat.button('Настроить расходы'), user=user)
    for value in values:
        await chat.text(value, user=user)
    token = chat.button('Сохранить')
    if save:
        await chat.tap(token, user=user)
    return token


async def event(chat, kind='income', values=('Зарплата', '50000', '10.09.2026'), *, user=1, save=True):
    await chat.text('/forecast', user=user)
    await chat.tap(chat.button('Планы по датам'), user=user)
    label = {'income': 'Поступление', 'expense': 'Разовый расход', 'saving': 'Взнос в накопления'}[kind]
    await chat.tap(chat.button(label), user=user)
    for value in values:
        await chat.text(value, user=user)
    token = chat.button('Сохранить')
    if save:
        await chat.tap(token, user=user)
    return token


async def shared_budget(db):
    ident = await family.create_household(db, 1)
    code = await family.create_invite(db, 1)
    assert await family.join_household(db, 2, code) == ident
    return ident


def test_confirmed_profile_and_dated_events_are_not_real_operations(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await db.add_transaction(1, 'expense', 'продукты', 12.34, occurred_on=DAY.isoformat())
        before = ledger(db)
        token = await profile(chat, save=False)
        assert await cashflow.get_profile(db, 1) is None
        await chat.tap(token)
        await chat.tap(token)
        saved = await cashflow.get_profile(db, 1)
        assert (saved['balance_minor'], saved['daily_minor'], saved['buffer_minor'], saved['version']) == (1000000, 100000, 100000, 1)
        token = await event(chat, save=False)
        assert await cashflow.list_events(db, 1) == []
        await chat.tap(token)
        await chat.tap(token)
        await event(chat, 'expense', ('Подарок', '2000', '09.09.2026'))
        await event(chat, 'saving', ('На отпуск', '3000', '10.09.2026'))
        assert len(await cashflow.list_events(db, 1)) == 3
        report = await cashflow.forecast(db, 1, DAY)
        assert report['next_income_on'] == '2026-09-10'
        assert [r['end_minor'] for r in report['rows']] == [800000, 500000, 5100000]
        assert report['rows'][-1]['before_income_minor'] == 100000
        await chat.text('/forecast')
        text = responses(chat, len(chat.session.calls) - 1)
        assert 'до поступления, с учетом расходов: 1 000 ₽' in text
        assert 'если оно придет: 51 000 ₽' in text
        assert ledger(db) == before
        assert 'уже закрыто' in responses(chat)
        await close(chat)
    asyncio.run(scenario())


def test_forecast_requires_explicit_balance_and_reconfirmation_after_ledger_or_day_change(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text('/forecast')
        assert 'Сначала подтвердите доступный остаток' in responses(chat)
        await profile(chat)
        await db.add_transaction(1, 'expense', 'продукты', 1, occurred_on=DAY.isoformat())
        start = len(chat.session.calls)
        await chat.text('/forecast')
        output = responses(chat, start)
        assert 'изменились записи бюджета' in output
        assert 'Остаток в конце периода' not in output
        await chat.tap(chat.button('Сверить остаток'))
        await chat.text('9900')
        assert (await cashflow.get_profile(db, 1))['balance_minor'] == 1000000
        await chat.tap(chat.button('Сохранить'))
        saved = await cashflow.get_profile(db, 1)
        assert (saved['balance_minor'], saved['daily_minor'], saved['buffer_minor'], saved['version']) == (990000, 100000, 100000, 2)
        assert not (await cashflow.forecast(db, 1, DAY))['stale']
        monkeypatch.setattr(cashflow_ui, 'today', lambda timezone: DAY + timedelta(days=1))
        start = len(chat.session.calls)
        await chat.text('/forecast')
        assert 'наступил новый день' in responses(chat, start)
        assert 'Остаток в конце периода' not in responses(chat, start)
        await close(chat)
    asyncio.run(scenario())


def test_journal_changes_during_balance_wizard_block_stale_confirmation(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text('/forecast')
        await chat.tap(chat.button('Сверить остаток'))
        await chat.text('10000')
        await db.add_transaction(1, 'expense', 'продукты', 100, occurred_on=DAY.isoformat())
        await chat.text('1000')
        await chat.text('0')
        await chat.tap(chat.button('Сохранить'))
        assert await cashflow.get_profile(db, 1) is None
        assert 'записи бюджета уже изменились' in responses(chat)
        await close(chat)
    asyncio.run(scenario())


def test_regular_bills_and_savings_reference_are_counted_once(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await recurring.create_schedule(db, 1, name='Связь', category='связь и интернет', amount_minor=50000, day_of_month=9, start_month='2026-09')
        await savings.create_goal(db, 1, name='На отпуск', target_minor=10000000, saved_minor=0, monthly_minor=300000, due_month='2027-09', create_key='goal')
        await profile(chat, ('10000', '0', '1000'))
        await event(chat)
        await event(chat, 'saving', ('На отпуск', '3000', '09.09.2026'))
        start = len(chat.session.calls)
        await chat.text('/forecast')
        output = responses(chat, start)
        assert '3 000 ₽ / месяц' in output
        assert 'учтены автоматически' in output
        report = await cashflow.forecast(db, 1, DAY)
        assert sum(r['recurring_minor'] for r in report['rows']) == 50000
        assert sum(r['saving_minor'] for r in report['rows']) == 300000
        assert report['rows'][-1]['before_income_minor'] == 550000
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_negative_balance_zero_daily_and_extra_buffer_are_visible_deficit(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await profile(chat, ('-100,25', '0', '50'))
        saved = await cashflow.get_profile(db, 1)
        assert saved['balance_minor'] == -10025
        start = len(chat.session.calls)
        await chat.text('🔮 Прогноз')
        output = responses(chat, start)
        assert '−150,25 ₽' in output
        assert 'Первое возможное снижение' in output
        assert '08.09.2026' in output
        assert 'план на 30 дней' in output
        await close(chat)
    asyncio.run(scenario())


def test_close_overdue_event_is_confirmed_and_never_marks_real_income(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await cashflow.add_event(db, 1, kind='income', name='Задержанный аванс', amount_minor=500000, due_on='2026-09-07', create_key='late')
        await profile(chat)
        assert 'Срок прошел у планов: 1' in responses(chat)
        await chat.tap(chat.button('Планы по датам'))
        await chat.tap(chat.button('Задержанный аванс'))
        assert 'Срок прошел, уточните план' in responses(chat)
        await chat.tap(chat.button('Убрать из прогноза'))
        assert len(await cashflow.list_events(db, 1)) == 1
        token = chat.button('Сохранить')
        await chat.tap(token)
        await chat.tap(token)
        assert await cashflow.list_events(db, 1) == []
        closed = (await cashflow.list_events(db, 1, include_closed=True))[0]
        assert (closed['is_closed'], closed['version']) == (1, 2)
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_family_scope_and_copied_callbacks_never_reveal_private_plans(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await event(chat, values=('Личный секрет', '15000', '10.09.2026'))
        private = (await cashflow.list_events(db, 1))[0]
        old_list = chat.button('Планы по датам')
        budget_id = await shared_budget(db)
        start = len(chat.session.calls)
        await chat.tap(old_list)
        assert 'Личный секрет' not in responses(chat, start)
        assert 'доступ изменился' in responses(chat, start)
        await profile(chat, user=2)
        assert await cashflow.get_profile(db, 1) is None
        assert (await cashflow.get_profile(db, budget_id))['balance_minor'] == 1000000
        start = len(chat.session.calls)
        for payload in (f'scope:1:0:cf:event:{private["id"]}', f'scope:3:0:cf:event:{private["id"]}', f'cf:event:{private["id"]}'):
            await chat.tap(payload, user=3)
        assert 'Личный секрет' not in responses(chat, start)
        assert (await cashflow.get_event(db, 1, private['id']))['is_closed'] == 0
        await close(chat)
    asyncio.run(scenario())


def test_two_family_profile_edits_do_not_overwrite_each_other(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        budget_id = await shared_budget(db)
        await profile(chat, ('10000', '100', '0'))
        first = await profile(chat, ('12000', '200', '0'), user=1, save=False)
        second = await profile(chat, ('13000', '300', '0'), user=2, save=False)
        await chat.tap(second, user=2)
        await chat.tap(first, user=1)
        row = await cashflow.get_profile(db, budget_id)
        assert (row['balance_minor'], row['daily_minor'], row['version']) == (1300000, 30000, 2)
        assert 'уже изменились' in responses(chat)
        await close(chat)
    asyncio.run(scenario())


def test_invalid_callbacks_keep_valid_draft_and_cannot_target_huge_ids(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        token = await profile(chat, save=False)
        start = len(chat.session.calls)
        for payload in ('cf:event:9223372036854775808', 'cf:close:0', 'cf:events:-1', 'cf:days:9999', 'cf:home:extra', 'cf:new:unknown', 'cf:confirm:badtoken'):
            await chat.tap('scope:1:0:' + payload)
        assert await cashflow.get_profile(db, 1) is None
        await chat.tap(token)
        assert (await cashflow.get_profile(db, 1))['version'] == 1
        assert 'Некорректный' in responses(chat, start)
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_invalid_inputs_and_cancel_leave_no_partial_events(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text('/forecast')
        await chat.tap(chat.button('Ожидаемое поступление'))
        for value in ('x' * 81, 'A\nB', 'A\x7fB'):
            await chat.text(value)
        await chat.text('Зарплата')
        for value in ('0', '-1', 'abc', '1000000000'):
            await chat.text(value)
        await chat.text('10000,25')
        for value in ('07.09.2026', '07.12.2026', '2026-09-09', '31.02.2027'):
            await chat.text(value)
        await chat.text('09.09.2026')
        assert await cashflow.list_events(db, 1) == []
        token = chat.button('Сохранить')
        await chat.text('/cancel')
        await chat.tap(token)
        assert await cashflow.list_events(db, 1) == []
        assert '80 символов' in responses(chat)
        assert 'до 89 дней вперед' in responses(chat)
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_day_change_before_confirmation_requires_new_balance_and_event_date(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        token = await profile(chat, save=False)
        monkeypatch.setattr(cashflow_ui, 'today', lambda timezone: DAY + timedelta(days=1))
        await chat.tap(token)
        assert await cashflow.get_profile(db, 1) is None
        assert 'Начался новый день' in responses(chat)
        monkeypatch.setattr(cashflow_ui, 'today', lambda timezone: DAY)
        token = await event(chat, values=('Аванс', '5000', '08.09.2026'), save=False)
        monkeypatch.setattr(cashflow_ui, 'today', lambda timezone: DAY + timedelta(days=1))
        await chat.tap(token)
        assert await cashflow.list_events(db, 1) == []
        await close(chat)
    asyncio.run(scenario())


def test_day_and_event_pagination_is_scoped_and_phone_sized(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await profile(chat, ('100000', '100', '0'))
        await chat.tap(chat.button('По дням'))
        text = responses(chat, len(chat.session.calls) - 2)
        assert '08.09.2026' in text and '12.09.2026' in text
        assert '13.09.2026' not in text
        await chat.tap(chat.button('Позже'))
        text = responses(chat, len(chat.session.calls) - 2)
        assert '13.09.2026' in text and '17.09.2026' in text
        for n in range(10):
            await cashflow.add_event(db, 1, kind='expense', name=f'План {n}', amount_minor=10000, due_on='2026-09-10', create_key=f'e{n}')
        await chat.text('/forecast')
        await chat.tap(chat.button('Планы по датам'))
        await chat.tap(chat.button('Далее'))
        assert chat.button('План 9').startswith('scope:1:0:cf:event:')
        for call in chat.session.calls:
            assert len(getattr(call, 'text', '') or '') < 4096
            markup = getattr(call, 'reply_markup', None)
            for row in getattr(markup, 'inline_keyboard', []):
                for button in row:
                    assert len(button.callback_data.encode()) <= 64
        await close(chat)
    asyncio.run(scenario())


def test_overdue_outflow_blocks_positive_forecast_until_explicit_plan_cleanup(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await profile(chat, ('10000', '0', '0'))
        await cashflow.add_event(db, 1, kind='expense', name='Неуточненный ремонт', amount_minor=1500000,
                                 due_on='2026-09-07', create_key='unpaid')
        start = len(chat.session.calls)
        await chat.text('/forecast')
        text = responses(chat, start)
        assert 'У расходов или взносов в накопления прошел срок' in text
        assert 'после уточнения планов' in text
        assert 'Остаток в конце периода' not in text
        assert 'не опускается ниже' not in text
        await chat.tap(chat.button('По дням'))
        assert 'Остаток в конце периода' not in responses(chat, start)
        await chat.tap(chat.button('Планы по датам'))
        await chat.tap(chat.button('Неуточненный ремонт'))
        assert 'расчет прогноза приостановлен' in responses(chat, start)
        await chat.tap(chat.button('Убрать из прогноза'))
        assert (await cashflow.forecast(db, 1, DAY))['stale']
        await chat.tap(chat.button('Сохранить'))
        assert not (await cashflow.forecast(db, 1, DAY))['stale']
        start = len(chat.session.calls)
        await chat.text('/forecast')
        assert 'Остаток в конце периода: 10 000 ₽' in responses(chat, start)
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())


def test_invalid_stored_event_date_blocks_forecast_but_can_be_closed_in_ui(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await profile(chat, ('10000', '0', '0'))
        ident = await cashflow.add_event(db, 1, kind='saving', name='Старая дата', amount_minor=500000,
                                         due_on='2026-09-10', create_key='invalid-date')
        with sqlite3.connect(db.path) as conn:
            conn.execute('UPDATE cashflow_events SET due_on=? WHERE id=?', ('invalid-date', ident))
        start = len(chat.session.calls)
        await chat.text('/forecast')
        text = responses(chat, start)
        assert 'У планов некорректная дата' in text
        assert 'после уточнения планов' in text
        assert 'Остаток в конце периода' not in text
        await chat.tap(chat.button('Планы по датам'))
        await chat.tap(chat.button('Старая дата'))
        assert 'дата требует проверки' in responses(chat, start)
        await chat.tap(chat.button('Убрать из прогноза'))
        await chat.tap(chat.button('Сохранить'))
        assert (await cashflow.get_event(db, 1, ident))['is_closed']
        assert not (await cashflow.forecast(db, 1, DAY))['stale']
        assert ledger(db) == []
        await close(chat)
    asyncio.run(scenario())
