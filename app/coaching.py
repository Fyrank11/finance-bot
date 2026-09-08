"""Small, explainable budget suggestions calculated locally from recorded facts.

Amounts stay in integer kopecks. This module receives an already-authorized
personal or household budget ID; callers must resolve that ID from the user.
"""
from __future__ import annotations

import calendar
from datetime import date
from fractions import Fraction

import aiosqlite

from .insights import comparison
from .inputs import month_label, valid_month
from .recurring import obligations


def _money(minor: int) -> str:
    """Format without converting totals or fractional kopecks through floats."""
    rubles, kopecks = divmod(abs(minor), 100)
    amount = f"{rubles:,}".replace(",", " ")
    if kopecks:
        amount += f",{kopecks:02d}"
    return f"{'−' if minor < 0 else ''}{amount} ₽"


async def budget_tips(db, budget_id: int, month: str, as_of: date) -> list[str]:
    """Return at most four plain-text tips for one authorized budget.

    A negative calculated balance is never interpreted as a debt. Scheduled
    obligations affect only the current month; historical reports must not
    treat today's payment status as the status on a past date. No assumption
    is made that the user has recorded all of their income or expenses.
    """
    month = valid_month(month)
    current_month = as_of.strftime("%Y-%m")
    if month > current_month:
        return [
            f"{month_label(month)} ещё не наступил. Можно заранее задать лимиты "
            "и расписания платежей. Советы по фактическим расходам появятся после записей за этот месяц."
        ]

    year, number = map(int, month.split("-"))
    cutoff = as_of if month == current_month else date(year, number, calendar.monthrange(year, number)[1])
    start, end = month + "-01", cutoff.isoformat()
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        totals = await (await conn.execute(
            "SELECT COUNT(*) count, "
            "COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE 0 END),0) income, "
            "COALESCE(SUM(CASE WHEN kind='expense' THEN amount_minor ELSE 0 END),0) expense "
            "FROM transactions WHERE user_id=? AND occurred_on BETWEEN ? AND ?",
            (budget_id, start, end),
        )).fetchone()
        categories = dict(await (await conn.execute(
            "SELECT category,SUM(amount_minor) FROM transactions "
            "WHERE user_id=? AND kind='expense' AND occurred_on BETWEEN ? AND ? GROUP BY category",
            (budget_id, start, end),
        )).fetchall())
        limits = dict(await (await conn.execute(
            "SELECT category,limit_minor FROM budgets WHERE user_id=? AND month=?", (budget_id, month),
        )).fetchall())
        opening = await (await conn.execute(
            "SELECT opening_minor FROM preferences WHERE user_id=?", (budget_id,),
        )).fetchone()
        net = await (await conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE -amount_minor END),0) "
            "FROM transactions WHERE user_id=? AND occurred_on<=?", (budget_id, end),
        )).fetchone()

    income, expense = totals["income"], totals["expense"]
    balance = (opening[0] if opening else 0) + net[0]
    tips: list[str] = []
    if not totals["count"]:
        tips.append(
            f"За {month_label(month).lower()} пока нет операций. Добавьте доходы и расходы, "
            "а в настройках укажите остаток до первой записи. Тогда советы будут опираться на ваши суммы."
        )
    elif not income:
        tips.append(
            f"За выбранный месяц записано расходов на {_money(expense)}, а доходов пока нет. "
            "Проверьте полноту записей и начальный остаток: отсутствие записи о доходе не означает, что его не было."
        )
    elif not expense:
        tips.append(
            f"За выбранный месяц записано доходов на {_money(income)}, а расходов пока нет. "
            "Добавляйте покупки и оплаченные счета — по одним доходам оценить расходы нельзя."
        )

    if month == current_month:
        pending = await obligations(db, budget_id, as_of)
        unpaid = pending["total_minor"]
        if unpaid and balance < unpaid:
            tips.append(
                f"По записям остаток — {_money(balance)}. В расписании не отмечены оплаченными "
                f"платежи до конца месяца на {_money(unpaid)} (включая прошлые сроки): "
                f"расчётная нехватка — {_money(unpaid - balance)}. "
                "Сверьте остаток и отметки об оплате, затем распределите платежи по датам поступлений. "
                "Будущие доходы здесь не учтены."
            )
        elif balance < 0:
            tips.append(
                f"Расчётный остаток по внесённым операциям и начальному остатку — {_money(balance)}. "
                "Сверьте его с деньгами на счетах, проверьте пропущенные поступления и начальный остаток. "
                "Сам по себе минус в учёте не подтверждает наличие долга."
            )

    exceeded = sorted(
        ((categories.get(category, 0) - limit, category, limit) for category, limit in limits.items()
         if categories.get(category, 0) > limit),
        reverse=True,
    )
    if exceeded:
        excess, category, limit = exceeded[0]
        tips.append(
            f"«{category}»: записано {_money(categories[category])} при лимите {_money(limit)}; "
            f"превышение — {_money(excess)}. Проверьте, не была ли покупка разовой, "
            "и учтите фактические потребности при следующем планировании."
        )
    else:
        nearly = sorted(
            ((Fraction(categories.get(category, 0), limit), category, limit) for category, limit in limits.items()
             if limit > 0 and categories.get(category, 0) * 100 >= limit * 80),
            reverse=True,
        )
        if nearly:
            _, category, limit = nearly[0]
            tips.append(
                f"«{category}»: записано {_money(categories[category])} из лимита {_money(limit)}; "
                f"остаток лимита — {_money(limit - categories[category])}. "
                "Сопоставьте план с необходимыми покупками и при необходимости уточните лимит."
            )

    # Equal elapsed periods, and at least three recorded expense days in each,
    # prevent one early-month purchase from becoming a claimed spending trend.
    compared = await comparison(db, budget_id, month, as_of)
    if expense and compared["days"] >= 7 and compared["previous_minor"]:
        async with aiosqlite.connect(db.path) as conn:
            recorded_days = []
            for period in (month, compared["previous"]):
                row = await (await conn.execute(
                    "SELECT COUNT(DISTINCT occurred_on) FROM transactions WHERE user_id=? "
                    "AND kind='expense' AND occurred_on BETWEEN ? AND ?",
                    (budget_id, period + "-01", period + f"-{compared['days']:02d}"),
                )).fetchone()
                recorded_days.append(row[0])
        delta = compared["change_minor"]
        if min(recorded_days) >= 3 and abs(delta) * 100 >= compared["previous_minor"] * 10:
            tips.append(
                f"За первые {compared['days']} дней выбранного месяца внесено расходов "
                f"на {_money(compared['current_minor'])}, за такой же отрезок предыдущего — "
                f"{_money(compared['previous_minor'])}: {'больше' if delta > 0 else 'меньше'} "
                f"на {_money(abs(delta))}. Перед изменением плана проверьте разовые покупки "
                "и полноту записей за оба периода."
            )

    # This is an explicitly hypothetical option, never a promise of savings or
    # an instruction to cut food, housing, medical or other necessary spending.
    flexible = [(amount, category) for category, amount in categories.items()
                if category in {"кафе и рестораны", "развлечения"}]
    if flexible and len(tips) < 4:
        amount, category = max(flexible)
        example = (amount * 5 + 50) // 100  # Five percent, rounded half up to one kopeck.
        if example:
            tips.append(
                f"Пример для вашего плана: 5% от записанных расходов на «{category}» "
                f"({_money(amount)}) — {_money(example)}. Если вам подходит тратить на эту категорию "
                "меньше, выберите комфортную сумму в «Лимиты». Это пример расчёта, а не прогноз экономии."
            )

    if not tips:
        tips.append(
            f"За выбранный месяц записано доходов на {_money(income)} и расходов на {_money(expense)}. "
            "Сверьте записи с выписками и добавьте расписания обязательных платежей: "
            "это поможет учитывать предстоящие расходы при планировании."
        )
    return tips[:4]
