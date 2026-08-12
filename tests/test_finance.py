from app.finance import affordability, allocation, credit_card_advice, money


def test_money():
    assert money(120000) == "120 000 ₽"


def test_affordability_safe_purchase():
    assert "Да" in affordability(10000, 100000, 0, 0)


def test_affordability_rejects_large_purchase():
    assert "отложить" in affordability(90000, 100000, 0, 0)


def test_credit_card_requires_cash_and_no_debt():
    assert "Не советую" in credit_card_advice(1000, 10000, 500)
    assert "Можно" in credit_card_advice(1000, 10000, 0)


def test_allocation_without_income():
    assert "внесите доходы" in allocation(0, 0, 0, 0)

