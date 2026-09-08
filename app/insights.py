"""Budget observations based only on recorded facts, with equal-period comparisons."""
from __future__ import annotations

import calendar
from datetime import date

import aiosqlite

from .inputs import shift_month, valid_month


async def comparison(db, budget_id: int, month: str, as_of: date) -> dict:
    month = valid_month(month)
    previous = shift_month(month, -1)
    y, m = map(int, month.split('-'))
    py, pm = map(int, previous.split('-'))
    # Compare the same number of days, including when the prior month is shorter.
    days = min(calendar.monthrange(y, m)[1], calendar.monthrange(py, pm)[1])
    if month == as_of.strftime('%Y-%m'):
        days = min(days, as_of.day)
    if month > as_of.strftime('%Y-%m'):
        days = 0
    async with aiosqlite.connect(db.path) as conn:
        async def total(period):
            if not days:
                return 0
            row = await (await conn.execute(
                "SELECT COALESCE(SUM(amount_minor),0) FROM transactions "
                "WHERE user_id=? AND kind='expense' AND occurred_on BETWEEN ? AND ?",
                (budget_id, period+'-01', period+f'-{days:02d}'),
            )).fetchone()
            return row[0]
        current_minor = await total(month)
        previous_minor = await total(previous)
    return {'month': month, 'previous': previous, 'days': days,
            'current_minor': current_minor, 'previous_minor': previous_minor,
            'change_minor': current_minor-previous_minor,
            'change_percent': (current_minor-previous_minor)*100/previous_minor if previous_minor else None}


async def search_transactions(db, budget_id: int, query: str, offset: int = 0, limit: int = 8) -> dict:
    query = query.strip().casefold()
    if not query or len(query) > 100:
        raise ValueError('Введите слово или метку длиной до 100 символов, например #отпуск.')
    # SQLite lower()/LIKE do not casefold Cyrillic. A deterministic function keeps
    # matching literal (including %, _, and backslashes) and supports Russian.
    async with aiosqlite.connect(db.path) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.create_function('fold', 1, lambda s: (s or '').casefold(), deterministic=True)
        where = "user_id=? AND instr(fold(category || ' ' || note),?)>0"
        params = (budget_id, query)
        totals = await (await conn.execute(
            f"SELECT COUNT(*) n, COALESCE(SUM(CASE WHEN kind='income' THEN amount_minor ELSE 0 END),0) income, "
            f"COALESCE(SUM(CASE WHEN kind='expense' THEN amount_minor ELSE 0 END),0) expense FROM transactions WHERE {where}", params,
        )).fetchone()
        rows = await (await conn.execute(
            f"SELECT * FROM transactions WHERE {where} ORDER BY occurred_on DESC,id DESC LIMIT ? OFFSET ?",
            (*params, limit, max(0, offset)),
        )).fetchall()
    return {'count': totals['n'], 'income_minor': totals['income'], 'expense_minor': totals['expense'],
            'rows': [dict(r) for r in rows], 'query': query}


async def limit_status(db, budget_id: int, category: str, month: str) -> str:
    from .finance import money
    for row in await db.budget_report(budget_id, month):
        if row['category'] != category or row['limit'] is None:
            continue
        spent, limit = row['spent'], row['limit']
        tail = f"{category}: {money(spent)} из {money(limit)}."
        if spent > limit:
            return f"🔴 Лимит превышен на {money(round(spent-limit,2))}.\n{tail}"
        if spent == limit:
            return f"🟠 Лимит исчерпан.\n{tail}"
        if limit and spent >= limit*.8:
            return f"🟡 Израсходовано {spent/limit:.0%} лимита.\n{tail}"
        return f"🎯 {tail}\nОсталось {money(round(limit-spent,2))}."
    return ''
