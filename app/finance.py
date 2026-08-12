from __future__ import annotations


def money(value: float) -> str:
    return f"{value:,.0f} ₽".replace(",", " ")


def allocation(income: float, expense: float, debt: float, goals_remaining: float) -> str:
    available = max(income - expense, 0)
    if income <= 0:
        return "Сначала внесите доходы и расходы за месяц — тогда я предложу распределение."
    essentials = income * 0.50
    safety = income * 0.10
    debt_share = min(income * 0.20, debt)
    goals = min(income * 0.15, goals_remaining)
    flexible = max(income - essentials - safety - debt_share - goals, 0)
    warning = "\n⚠️ Расходы уже выше доходов." if expense > income else ""
    return (
        "Рекомендованный ориентир на месяц:\n"
        f"• обязательные расходы — до {money(essentials)}\n"
        f"• резерв — {money(safety)}\n"
        f"• погашение долгов — {money(debt_share)}\n"
        f"• цели — {money(goals)}\n"
        f"• свободные траты — до {money(flexible)}\n"
        f"Сейчас после учтённых расходов осталось {money(available)}.{warning}"
    )


def affordability(price: float, balance: float, debt: float, goals_remaining: float) -> str:
    reserve = max(balance * 0.20, 0)
    spendable = max(balance - reserve, 0)
    if price <= spendable:
        return f"✅ Да. После покупки останется {money(balance-price)}; резерв {money(reserve)} сохранён."
    shortage = price - spendable
    if debt > 0 or goals_remaining > 0:
        return f"⚠️ Сейчас нежелательно: не хватает {money(shortage)} сверх безопасного лимита, а также есть долги или незакрытые цели."
    return f"⚠️ Лучше отложить: до безопасной суммы покупки не хватает {money(shortage)}."


def credit_card_advice(price: float, balance: float, debt: float) -> str:
    if price <= 0:
        return "Сумма должна быть больше нуля."
    if debt > 0:
        return "❌ Не советую: уже есть непогашенный долг. Новая покупка увеличит кредитную нагрузку."
    if balance >= price:
        return "✅ Можно только ради льготного периода/кэшбэка, если вы сразу отложите всю сумму и точно погасите выписку полностью."
    return "❌ Не советую: собственных денег на полное погашение сейчас недостаточно. Кредитка не должна заменять доход."

