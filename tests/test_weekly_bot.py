"""Real dispatcher opt-in and preview flows; no outbound Telegram network."""
import asyncio
from datetime import datetime, timezone

from app import bot as app, family, weekly, weekly_ui
from app.db import Database
from app.rate_limit import RateLimiter
from test_bot import ChatHarness
from test_persistent_bot import PersistentChatHarness

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)


async def setup(tmp_path, monkeypatch):
    db = Database(tmp_path / 'weekly-bot.db')
    await db.init()
    monkeypatch.setattr(app, 'db', db, raising=False)
    monkeypatch.setattr(app, 'allowed_user_ids', frozenset({1, 2}))
    monkeypatch.setattr(app, 'public_signup', False)
    monkeypatch.setattr(app, 'rate_limiter', RateLimiter(limit=1000, expensive_limit=100))
    monkeypatch.setattr(weekly_ui, 'now', lambda: NOW)
    return db, ChatHarness()


def output(chat, since=0):
    return '\n'.join(getattr(call, 'text', '') or '' for call in chat.session.calls[since:])


async def wizard(chat, *, user=1, save=True):
    await chat.text('/weekly', user=user)
    await chat.tap(chat.button('Настроить рассылку'), user=user)
    await chat.tap(chat.button('Воскресенье'), user=user)
    await chat.text('19:30', user=user)
    await chat.tap(chat.button('Москва'), user=user)
    token = chat.button('Включить')
    if save:
        await chat.tap(token, user=user)
    return token


async def close(chat):
    await chat.bot.session.close()
    await chat.dispatcher.storage.close()


def test_preview_does_not_subscribe_confirmation_retry_and_optout(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await db.add_transaction(1, 'expense', 'продукты', 850, occurred_on='2026-09-07')
        await chat.text('/weekly')
        await chat.tap(chat.button('Показать обзор'))
        assert '850 ₽' in output(chat)
        assert '01.09.2026–07.09.2026' in output(chat)
        assert await weekly.get_subscription(db, 1) is None
        token = await wizard(chat, save=False)
        assert await weekly.get_subscription(db, 1) is None
        await chat.tap(token)
        row = await weekly.get_subscription(db, 1)
        assert (row['budget_id'], row['weekday'], row['hour'], row['minute'], row['timezone']) == (1, 6, 19, 30, 'Europe/Moscow')
        assert datetime.fromisoformat(row['next_due_at']) == datetime(2026, 9, 13, 16, 30, tzinfo=timezone.utc)
        await chat.tap(token)
        assert await weekly.get_subscription(db, 1) == row
        await chat.tap(chat.button('Отключить рассылку'))
        assert not (await weekly.get_subscription(db, 1))['enabled']
        assert len(await db.transactions(1, '2026-09')) == 1
        await close(chat)
    asyncio.run(scenario())


def test_bad_inputs_and_cancel_never_enable_notifications(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        await chat.text('/weekly')
        await chat.tap(chat.button('Настроить рассылку'))
        await chat.tap(chat.button('Среда'))
        await chat.text('24:00')
        assert 'Введите время' in output(chat)
        await chat.text('7:05')
        await chat.text('../not-a-zone')
        assert 'Не нашел' in output(chat)
        await chat.text('Asia/Almaty')
        token = chat.button('Включить')
        await chat.text('/cancel')
        await chat.tap(token)
        for data in ('wk:setup', 'scope:1:0:wk:', 'scope:1:0:wk:day:fake:99', 'scope:1:0:wk:off:extra'):
            await chat.tap(data)
        assert await weekly.get_subscription(db, 1) is None
        await close(chat)
    asyncio.run(scenario())


def test_forged_or_copied_confirmation_cannot_subscribe_another_user(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        token = await wizard(chat, save=False)
        await chat.tap(token, user=2)
        await chat.tap(token.replace('scope:1:0:', 'scope:2:0:'), user=2)
        assert await weekly.get_subscription(db, 2) is None
        await chat.tap(token)
        assert (await weekly.get_subscription(db, 1))['enabled']
        await close(chat)
    asyncio.run(scenario())


def test_pinned_family_subscription_is_not_retargeted_by_menu_and_can_be_disabled_after_leaving(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        shared = await family.create_household(db, 1)
        code = await family.create_invite(db, 1)
        await family.join_household(db, 2, code)
        await wizard(chat, user=2)
        assert (await weekly.get_subscription(db, 2))['budget_id'] == shared
        await family.switch_budget(db, 2, shared=False)
        await chat.text('/weekly', user=2)
        row = await weekly.get_subscription(db, 2)
        assert row['budget_id'] == shared
        await family.remove_member(db, 1, 2)
        await chat.text('/weekly', user=2)
        await chat.tap(chat.button('Отключить рассылку'), user=2)
        assert not (await weekly.get_subscription(db, 2))['enabled']
        await close(chat)
    asyncio.run(scenario())


def test_concurrent_settings_change_invalidates_old_confirmation(tmp_path, monkeypatch):
    async def scenario():
        db, chat = await setup(tmp_path, monkeypatch)
        token = await wizard(chat, save=False)
        await weekly.set_subscription(db, 1, budget_id=1, weekday=0, hour=9, minute=0, timezone='UTC', enabled=True, expected_version=None, now=NOW)
        await chat.tap(token)
        row = await weekly.get_subscription(db, 1)
        assert (row['weekday'], row['hour'], row['timezone'], row['version']) == (0, 9, 'UTC', 1)
        assert 'Настройки уже изменились' in output(chat)
        await close(chat)
    asyncio.run(scenario())


def test_weekly_setup_continues_after_production_dispatcher_restart(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setattr(weekly_ui, 'now', lambda: NOW)
        chat = await PersistentChatHarness.create(tmp_path / 'persistent-weekly.db', monkeypatch)
        await chat.text('/weekly')
        await chat.tap(chat.button('Настроить рассылку'))
        await chat.tap(chat.button('Понедельник'))
        chat = await chat.restart(monkeypatch)
        await chat.text('08:15')
        await chat.tap(chat.button('Екатеринбург'))
        await chat.tap(chat.button('Включить'))
        subscription = await weekly.get_subscription(chat.db, 1)
        assert subscription['enabled'] and subscription['timezone'] == 'Asia/Yekaterinburg'
        await chat.close()
    asyncio.run(scenario())
