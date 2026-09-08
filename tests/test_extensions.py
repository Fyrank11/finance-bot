import asyncio

from app import bot as app, family
from app.db import Database
from test_bot import ChatHarness


def test_setup_closed_by_default_and_self_id_available(tmp_path, monkeypatch):
    async def run():
        db = Database(tmp_path/'setup.db')
        await db.init()
        monkeypatch.setattr(app,'db',db,raising=False)
        monkeypatch.setattr(app,'allowed_user_ids',frozenset())
        chat = ChatHarness()
        await chat.text('/whoami',user=42)
        assert '42' in chat.session.calls[-1].text
        await chat.text('кофе 350',user=42)
        assert 'нет доступа' in chat.session.calls[-1].text
        assert (await db.summary(42))['expense']==0
    asyncio.run(run())


def test_family_revocation_invalidates_draft_and_actor_is_recorded(tmp_path,monkeypatch):
    async def run():
        db = Database(tmp_path/'family-chat.db')
        await db.init()
        monkeypatch.setattr(app,'db',db,raising=False)
        monkeypatch.setattr(app,'allowed_user_ids',frozenset({1,2}))
        shared = await family.create_household(db,1)
        code = await family.create_invite(db,1)
        await family.join_household(db,2,code,'Оля')
        chat = ChatHarness()
        await chat.text('кофе 350',user=2)
        save = chat.button('Сохранить')
        await family.remove_member(db,1,2)
        await chat.tap(save,user=2)
        assert (await db.summary(shared))['expense']==0
        assert (await db.summary(2))['expense']==0
        await chat.text('кофе 350',user=1)
        await chat.tap(chat.button('Сохранить'),user=1)
        rows = await db.transactions(shared,await db.selected_month(shared))
        assert len(rows)==1 and rows[0]['actor_user_id']==1 and rows[0]['actor_name']=='Test'
        await chat.text('/search кафе',user=2)
        assert 'Найдено: 0' in chat.session.calls[-1].text
        await chat.text('/search кафе',user=1)
        assert 'Найдено: 1' in chat.session.calls[-1].text
        await chat.text('📊 Мой бюджет',user=1)
        assert 'Семейный бюджет' in chat.session.calls[-1].text
        assert 'Остаток после' in chat.session.calls[-1].text
    asyncio.run(run())


def test_old_panels_and_old_confirmation_cannot_change_another_budget(tmp_path,monkeypatch):
    async def run():
        db = Database(tmp_path/'stale.db')
        await db.init()
        monkeypatch.setattr(app,'db',db,raising=False)
        monkeypatch.setattr(app,'allowed_user_ids',frozenset({1}))
        chat = ChatHarness()
        await chat.text('⚙️ Настройки')
        old_opening = chat.button('Начальные деньги')
        await chat.text('🎯 Лимиты')
        old_budget = chat.button('Задать')
        await chat.text('/payments')
        old_rec = chat.button('Добавить платёж')
        await family.create_household(db,1)
        await chat.text('/menu')
        for callback in (old_opening,old_budget,old_rec):
            await chat.tap(callback)
            assert 'другого бюджета' in chat.session.calls[-1].text
        assert (await db.summary(-1))['opening']==0
        await chat.text('кофе 350')
        old_save = chat.button('Сохранить')
        await chat.tap(chat.button('Сумма'))
        await chat.text('10000')
        current_save = chat.button('Сохранить')
        assert current_save != old_save
        await chat.tap(old_save)
        assert (await db.summary(-1))['expense']==0
        await chat.tap(current_save)
        assert (await db.summary(-1))['expense']==10000
    asyncio.run(run())
