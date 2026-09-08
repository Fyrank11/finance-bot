import asyncio
from datetime import date

from app.coaching import budget_tips
from app.db import Database
from app.recurring import create_schedule, mark_paid


def test_empty_and_future_months_do_not_invent_advice(tmp_path):
    async def run():
        db = Database(tmp_path / "empty.db")
        await db.init()
        empty = await budget_tips(db, 1, "2024-03", date(2024, 3, 8))
        assert len(empty) == 1 and "пока нет операций" in empty[0]
        assert "до первой записи" in empty[0]
        await create_schedule(db, 1, name="Аренда", category="аренда", amount_minor=100_000,
                              day_of_month=5, start_month="2024-04")
        future = await budget_tips(db, 1, "2024-04", date(2024, 3, 8))
        assert len(future) == 1 and "ещё не наступил" in future[0]
        assert "нехватка" not in " ".join(future)
    asyncio.run(run())


def test_current_obligations_use_exact_balance_and_budget_scope(tmp_path):
    async def run():
        db = Database(tmp_path / "scope.db")
        await db.init()
        await db.set_opening(-1, 100.01)
        await db.add_transaction(-1, "income", "зарплата", 500.02, occurred_on="2024-03-01", actor_user_id=101)
        await db.add_transaction(-1, "expense", "продукты", 0.03, occurred_on="2024-03-02", actor_user_id=202)
        await create_schedule(db, -1, name="Общий счёт", category="дом и быт", amount_minor=60_001,
                              day_of_month=20, start_month="2024-03")
        # Personal and other household information must not alter family tips.
        for other in (101, 202, -2):
            await db.set_opening(other, 98765.43)
            await db.add_transaction(other, "expense", "личное", 98765.43, occurred_on="2024-03-01")
            await db.set_budget(other, "2024-03", "личное", 0)
            await create_schedule(db, other, name="Личный счёт", category="личное", amount_minor=9_876_543,
                                  day_of_month=20, start_month="2024-03")
        text = " ".join(await budget_tips(db, -1, "2024-03", date(2024, 3, 8)))
        assert "остаток — 600 ₽" in text
        assert "на 600,01 ₽" in text and "нехватка — 0,01 ₽" in text
        assert "Будущие доходы здесь не учтены" in text
        assert "98 765" not in text and "личное" not in text
    asyncio.run(run())


def test_paid_obligations_and_historical_months_do_not_create_false_shortfall(tmp_path, monkeypatch):
    monkeypatch.setattr("app.recurring.today", lambda timezone: date(2024, 3, 8))

    async def run():
        db = Database(tmp_path / "paid.db")
        await db.init()
        await db.add_transaction(1, "income", "зарплата", 100, occurred_on="2024-03-01")
        schedule = await create_schedule(db, 1, name="Счёт", category="дом и быт", amount_minor=10_000,
                                         day_of_month=8, start_month="2024-03")
        await mark_paid(db, 1, schedule, "2024-03-08")
        paid = " ".join(await budget_tips(db, 1, "2024-03", date(2024, 3, 8)))
        assert "нехватка" not in paid and "минус" not in paid
        await create_schedule(db, 1, name="Ещё счёт", category="дом и быт", amount_minor=99_999,
                              day_of_month=9, start_month="2024-03")
        historical = " ".join(await budget_tips(db, 1, "2024-03", date(2024, 4, 1)))
        assert "нехватка" not in historical and "не отмечены" not in historical
    asyncio.run(run())


def test_zero_limit_and_one_kopeck_excess_are_not_lost(tmp_path):
    async def run():
        db = Database(tmp_path / "limits.db")
        await db.init()
        await db.set_opening(1, 100)
        await db.add_transaction(1, "income", "зарплата", 100, occurred_on="2024-03-01")
        await db.add_transaction(1, "expense", "здоровье", 10.23, occurred_on="2024-03-02")
        await db.set_budget(1, "2024-03", "здоровье", 10.22)
        text = " ".join(await budget_tips(db, 1, "2024-03", date(2024, 3, 8)))
        assert "при лимите 10,22 ₽" in text and "превышение — 0,01 ₽" in text
        assert "сократите" not in text and "5%" not in text
        await db.set_budget(1, "2024-03", "здоровье", 0)
        text = " ".join(await budget_tips(db, 1, "2024-03", date(2024, 3, 8)))
        assert "при лимите 0 ₽" in text and "превышение — 10,23 ₽" in text
    asyncio.run(run())


def test_comparison_uses_same_days_and_excludes_other_budgets_and_later_records(tmp_path):
    async def run():
        db = Database(tmp_path / "comparison.db")
        await db.init()
        await db.set_opening(1, 10000)
        await db.add_transaction(1, "income", "зарплата", 10000, occurred_on="2024-03-01")
        for day, amount in (("01", 100.01), ("04", 200.01), ("08", 300.01)):
            await db.add_transaction(1, "expense", "продукты", amount, occurred_on="2024-03-" + day)
            await db.add_transaction(1, "expense", "продукты", 100, occurred_on="2024-02-" + day)
            await db.add_transaction(2, "expense", "продукты", 99999, occurred_on="2024-03-" + day)
        await db.add_transaction(1, "expense", "продукты", 99999, occurred_on="2024-02-09")
        await db.add_transaction(1, "expense", "продукты", 99999, occurred_on="2024-03-09")
        text = " ".join(await budget_tips(db, 1, "2024-03", date(2024, 3, 8)))
        assert "За первые 8 дней" in text
        assert "на 600,03 ₽" in text and "предыдущего — 300 ₽" in text and "больше на 300,03 ₽" in text
        assert "полноту записей за оба периода" in text
        assert "99 999" not in text
    asyncio.run(run())


def test_sparse_and_early_month_records_are_not_a_trend(tmp_path):
    async def run():
        db = Database(tmp_path / "sparse.db")
        await db.init()
        await db.add_transaction(1, "expense", "продукты", 100, occurred_on="2024-02-01")
        await db.add_transaction(1, "expense", "продукты", 900, occurred_on="2024-03-01")
        for as_of in (date(2024, 3, 1), date(2024, 3, 8)):
            text = " ".join(await budget_tips(db, 1, "2024-03", as_of))
            assert "за такой же отрезок" not in text
            assert "отсутствие записи о доходе не означает" in text
            assert "минус в учёте не подтверждает наличие долга" in text
            assert "ежедневно" not in text
        # The earliest supported month also has no previous valid tracking month.
        assert "пока нет операций" in " ".join(await budget_tips(db, 1, "2000-01", date(2024, 3, 8)))
    asyncio.run(run())


def test_optional_saving_example_is_explicit_and_rounds_half_up(tmp_path):
    async def run():
        db = Database(tmp_path / "example.db")
        await db.init()
        await db.add_transaction(1, "income", "зарплата", 1000, occurred_on="2024-03-01")
        await db.add_transaction(1, "expense", "кафе", 123.50, occurred_on="2024-03-01")
        text = " ".join(await budget_tips(db, 1, "2024-03", date(2024, 3, 8)))
        assert "5%" in text and "123,50 ₽" in text and "6,18 ₽" in text
        assert "комфортную сумму" in text and "не прогноз экономии" in text
        assert "сэкономите" not in text
    asyncio.run(run())


def test_advice_is_bounded_and_preserves_priority(tmp_path):
    async def run():
        db = Database(tmp_path / "priority.db")
        await db.init()
        for period, amount in (("2024-02", 50), ("2024-03", 100)):
            for day in ("01", "02", "03"):
                await db.add_transaction(1, "expense", "кафе", amount, occurred_on=period + "-" + day)
        await db.set_budget(1, "2024-03", "кафе", 100)
        await create_schedule(db, 1, name="Счёт", category="дом и быт", amount_minor=10000,
                              day_of_month=20, start_month="2024-03")
        tips = await budget_tips(db, 1, "2024-03", date(2024, 3, 8))
        assert len(tips) == 4
        assert "доходов пока нет" in tips[0]
        assert "нехватка" in tips[1]
        assert "превышение" in tips[2]
        assert "За первые 8 дней" in tips[3]
        assert "5%" not in " ".join(tips)
    asyncio.run(run())
