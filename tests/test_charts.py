import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from io import BytesIO

from PIL import Image

from app.charts import CategoryTotal, ChartData, build_chart_data, render_expense_chart
from app.db import Database


def test_chart_exact_aggregates_scope_cutoff_and_tail(tmp_path):
    async def run():
        db = Database(tmp_path / 'charts.db')
        await db.init()
        for index, category in enumerate(('еда', 'дом', 'одежда', 'спорт', 'кино', 'цветы', 'такси')):
            await db.add_transaction(1, 'expense', category, (index + 1) * .11, occurred_on='2024-02-01')
        await db.add_transaction(1, 'income', 'зарплата', 20.01, occurred_on='2024-02-08')
        await db.add_transaction(1, 'expense', 'еда', 999, occurred_on='2024-02-09')
        await db.add_transaction(2, 'expense', 'еда', 900, occurred_on='2024-02-01')
        await db.add_transaction(-3, 'expense', 'еда', 7.01, occurred_on='2024-02-01', actor_user_id=1)
        await db.add_transaction(-3, 'expense', 'еда', 8.02, occurred_on='2024-02-02', actor_user_id=2)
        result = await build_chart_data(db, 1, '2024-02', date(2024, 2, 8))
        assert result.income_minor == 2001
        assert result.expense_minor == 308
        assert sum(row.amount_minor for row in result.categories) == 308
        assert len(result.categories) == 6
        assert result.categories[-1] == CategoryTotal('Остальные категории', 33)
        assert result.daily_minor == (308, 0, 0, 0, 0, 0, 0, 0)
        assert result.elapsed_days == 8 and result.month_days == 29
        family = await build_chart_data(db, -3, '2024-02', date(2024, 3, 1))
        assert family.expense_minor == 1503
        assert family.daily_minor[:2] == (701, 802)
        assert len(family.daily_minor) == 29
    asyncio.run(run())


def test_chart_aggregation_includes_more_than_history_page_cap(tmp_path):
    async def run():
        import aiosqlite
        db = Database(tmp_path / 'large.db')
        await db.init()
        async with aiosqlite.connect(db.path) as conn:
            await conn.executemany(
                'INSERT INTO transactions(user_id,kind,category,amount,amount_minor,occurred_on,created_at) '
                "VALUES(1,'expense','еда',.01,1,'2024-01-01','2024-01-01')", [() for _ in range(10025)],
            )
            await conn.commit()
        result = await build_chart_data(db, 1, '2024-01', date(2024, 2, 1))
        assert result.expense_minor == 10025
        assert result.categories == (CategoryTotal('еда', 10025),)
        assert sum(result.daily_minor) == 10025
    asyncio.run(run())


def test_empty_income_only_and_future_month_are_not_fabricated(tmp_path):
    async def run():
        db = Database(tmp_path / 'empty.db')
        await db.init()
        await db.add_transaction(1, 'income', 'зарплата', 1000, occurred_on='2024-02-01')
        result = await build_chart_data(db, 1, '2024-02', date(2024, 2, 1))
        assert result.income_minor == 100000 and result.expense_minor == 0
        assert result.categories == () and result.daily_minor == (0,)
        future = await build_chart_data(db, 1, '2024-02', date(2024, 1, 31))
        assert future.elapsed_days == 0 and future.daily_minor == ()
        assert future.expense_minor == future.income_minor == 0
        return result, future
    for result in asyncio.run(run()):
        with Image.open(BytesIO(render_expense_chart(result))) as img:
            assert img.size == (1080, 1440) and img.format == 'PNG'


def test_render_is_deterministic_thread_safe_and_handles_long_literal_labels():
    data = ChartData('2024-02', date(2024, 2, 8), 8, 29, 9000000, 12345,
                     (CategoryTotal('Очень длинная категория с $latex$ и символами & <> безопасно', 12345),),
                     (0, 12345, 0, 0, 0, 0, 0, 0))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: render_expense_chart(data, example=True), range(2)))
    assert results[0] == results[1]
    assert 10000 < len(results[0]) < 2_000_000
    with Image.open(BytesIO(results[0])) as image:
        assert image.size == (1080, 1440)
