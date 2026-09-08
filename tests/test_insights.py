import asyncio
from datetime import date

from app.db import Database
from app.insights import comparison, limit_status, search_transactions


def test_equal_periods_leap_year_zero_baseline_and_literal_search(tmp_path):
    async def run():
        db = Database(tmp_path / 'insights.db')
        await db.init()
        for user, kind, amount, day, note in [
            (1,'expense',100,'2024-02-08','#Отпуск 100%'),
            (1,'expense',900,'2024-02-09','later'),
            (1,'expense',150,'2024-03-08','#отпуск'),
            (1,'income',20,'2024-03-08','#отпуск возврат'),
            (2,'expense',999,'2024-03-08','#отпуск'),
        ]:
            await db.add_transaction(user,kind,'Продукты',amount,note,occurred_on=day)
        result = await comparison(db,1,'2024-03',date(2024,3,8))
        assert result['days']==8
        assert result['current_minor']==15000
        assert result['previous_minor']==10000
        assert result['change_percent']==50
        assert (await comparison(db,1,'2024-03',date(2024,4,1)))['days']==29
        assert (await comparison(db,1,'2024-04',date(2024,3,8)))['days']==0
        assert (await comparison(db,1,'2024-02',date(2024,3,8)))['change_percent'] is None
        search = await search_transactions(db,1,'#ОТПУСК')
        assert search['count']==3 and search['expense_minor']==25000 and search['income_minor']==2000
        assert (await search_transactions(db,1,'%'))['count']==1
        assert (await search_transactions(db,1,'_'))['count']==0
    asyncio.run(run())


def test_limits_zero_and_thresholds_and_family_source_ids(tmp_path):
    async def run():
        db = Database(tmp_path / 'limits.db')
        await db.init()
        await db.set_budget(1,'2025-09','продукты',1000)
        await db.add_transaction(1,'expense','продукты',800,occurred_on='2025-09-01')
        assert '80%' in await limit_status(db,1,'продукты','2025-09')
        await db.set_budget(1,'2025-09','продукты',0)
        assert 'превышен' in await limit_status(db,1,'продукты','2025-09')
        first = await db.add_transaction(-1,'expense','продукты',10,actor_user_id=1,source_message_id=5)
        second = await db.add_transaction(-1,'expense','продукты',20,actor_user_id=2,source_message_id=5)
        assert first != second
        assert await db.add_transaction(-1,'expense','продукты',20,actor_user_id=2,source_message_id=5) == second
        assert (await db.summary(-1))['expense']==30
    asyncio.run(run())
