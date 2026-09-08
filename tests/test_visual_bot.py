"""Exercise new panels through real middleware; no Telegram or production data."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendPhoto, SetMyDescription

from app import bot as app, family
from app.branding import apply_text_profile
from app.db import Database
from test_bot import ChatHarness


def test_graph_and_tips_keep_month_and_budget_scope(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path / 'visual.db')
        await db.init()
        monkeypatch.setattr(app, 'db', db, raising=False)
        monkeypatch.setattr(app, 'allowed_user_ids', frozenset({1, 2}))
        monkeypatch.setattr(app, 'today', lambda timezone: date(2025, 9, 8))
        await db.select_month(1, '2025-09')
        await db.set_budget(1, '2025-09', 'продукты', 100)
        await db.add_transaction(1, 'expense', 'продукты', 150, occurred_on='2025-09-01')
        await db.add_transaction(2, 'expense', 'секретная категория', 8888, occurred_on='2025-09-01')
        chat = ChatHarness()
        await chat.text('/charts')
        photo = chat.session.calls[-1]
        assert isinstance(photo, SendPhoto)
        assert photo.photo.data.startswith(b'\x89PNG\r\n\x1a\n')
        assert photo.photo.filename == 'expenses_2025-09.png'
        assert 'Личный бюджет' in photo.caption
        old_tips = chat.button('Подсказки')
        await db.select_month(1, '2025-08')
        await chat.tap(old_tips)
        panel = chat.session.calls[-1].text
        assert 'Сентябрь 2025' in panel and 'превышение — 50 ₽' in panel
        assert 'секретная' not in panel and '8 888' not in panel
        assert await db.selected_month(1) == '2025-08'
        shared = await family.create_household(db, 1)
        await chat.text('/menu')
        await chat.tap(old_tips)
        assert 'другого бюджета' in chat.session.calls[-1].text
        await db.select_month(shared, '2025-09')
        await chat.text('/tips')
        assert 'Семейный бюджет' in chat.session.calls[-1].text
        assert 'продукты' not in chat.session.calls[-1].text
        await chat.bot.session.close()
        await chat.dispatcher.storage.close()
    asyncio.run(run())


def test_private_access_gate_precedes_graphs_and_tips(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path / 'access.db')
        await db.init()
        monkeypatch.setattr(app, 'db', db, raising=False)
        monkeypatch.setattr(app, 'allowed_user_ids', frozenset({1}))
        chat = ChatHarness()
        for command in ('/analytics', '/charts', '/tips'):
            await chat.text(command, user=2)
            assert 'нет доступа' in chat.session.calls[-1].text
            await chat.text(command, user=1, chat_type='group')
            assert 'личный чат' in chat.session.calls[-1].text
        await chat.tap('scope:1:0:charts:2025-09', user=2)
        assert 'нет доступа' in chat.session.calls[-1].text
        assert not any(isinstance(call, SendPhoto) for call in chat.session.calls)
    asyncio.run(run())


def test_renderer_failure_keeps_text_analytics_available(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path / 'fallback.db')
        await db.init()
        monkeypatch.setattr(app, 'db', db, raising=False)
        monkeypatch.setattr(app, 'allowed_user_ids', frozenset({1}))
        await db.select_month(1, '2025-09')
        def fail(*args, **kwargs):
            raise RuntimeError('renderer unavailable')
        monkeypatch.setattr(app, 'render_expense_chart', fail)
        chat = ChatHarness()
        await chat.text('/analytics')
        assert any('не удалось' in getattr(call, 'text', '') for call in chat.session.calls)
        assert 'Сравнение расходов' in chat.session.calls[-1].text
    asyncio.run(run())


def test_profile_network_failure_is_nonfatal():
    async def run():
        bot = AsyncMock()
        bot.set_my_description.side_effect = TelegramNetworkError(
            method=SetMyDescription(description='example'), message='network unavailable'
        )
        assert await apply_text_profile(bot) is False
    asyncio.run(run())
