from datetime import date

import pytest

from app.inputs import parse_amount, quick_entry, to_minor


@pytest.mark.parametrize(
    "text,kind,category,minor",
    [
        ("продукты 850", "expense", "продукты", 85000),
        ("850 продукты", "expense", "продукты", 85000),
        ("+ 120000 зарплата", "income", "зарплата", 12000000),
        ("+ зарплата 150000", "income", "зарплата", 15000000),
        ("кофе 350", "expense", "кафе и рестораны", 35000),
        ("кофе 350 рублей", "expense", "кафе и рестораны", 35000),
        ("350 рублей кофе", "expense", "кафе и рестораны", 35000),
        ("АПТЕКА 350,25 руб.", "expense", "здоровье", 35025),
        ("продукты 1,5к", "expense", "продукты", 150000),
        ("1.50K продукты", "expense", "продукты", 150000),
        ("продукты 2 тыс", "expense", "продукты", 200000),
        ("продукты 2 тыс. рублей", "expense", "продукты", 200000),
        ("+ премия 1 млн", "income", "премия", 100000000),
        ("+ 1,25 млн. ₽ премия", "income", "премия", 125000000),
        ("аптека 0,01", "expense", "здоровье", 1),
        ("кофе 1\u202f250,01 ₽", "expense", "кафе и рестораны", 125001),
        ("покупка 999999999,99", "expense", "покупка", 99999999999),
        ("- 350 кофе", "expense", "кафе и рестораны", 35000),
    ],
)
def test_quick_syntax_keeps_exact_minor_units(text, kind, category, minor):
    entry = quick_entry(text)
    assert entry["kind"] == kind
    assert entry["category"] == category
    assert to_minor(entry["amount"]) == minor
    assert entry["note"] == ""
    assert "occurred_on" not in entry


@pytest.mark.parametrize(
    "text",
    [
        "кофе 2.345", "2.345 кофе", "кофе 2.345к", "кофе NaN", "кофе Infinity",
        "кофе -350", "кофе - 350", "--350 кофе", "кофе 0", "кофе 0к",
        "кофе 1000000000", "кофе 1000 млн", "кофе 999999999,999",
        "кофе 12 34", "кофе 1,2,3", "кофе 1e3", "кофе 12abc",
        "кофе 350 и 450", "350 кофе 450", "кофе 350 такси 500", "кофе 350/450",
        "кофе 1 млн тыс", "кофе 100 долларов", "кофе #еда 350",
        "сегодня кофе вчера 350", "кофе сегодня 350 сегодня",
    ],
)
def test_quick_input_rejects_ambiguous_and_invalid_amounts(text):
    with pytest.raises(ValueError):
        quick_entry(text, current=date(2026, 9, 8))


@pytest.mark.parametrize(
    "text,expected",
    [
        ("вчера кофе 350", "2026-09-07"),
        ("кофе 350 рублей вчера", "2026-09-07"),
        ("350 вчера кофе", "2026-09-07"),
        ("СЕГОДНЯ + зарплата 150000", "2026-09-08"),
        ("+ сегодня зарплата 150000", "2026-09-08"),
    ],
)
def test_relative_date_uses_callers_local_day(text, expected):
    assert quick_entry(text, current=date(2026, 9, 8))["occurred_on"] == expected


def test_yesterday_crosses_month_and_year_boundaries():
    assert quick_entry("вчера продукты 850", current=date(2026, 1, 1))["occurred_on"] == "2025-12-31"
    with pytest.raises(ValueError):
        quick_entry("вчера продукты 850", current=date(2000, 1, 1))


def test_note_and_tags_never_change_date_or_amount():
    entry = quick_entry("кофе 350 рублей; вчера с Олей, 2 кофе #свидание; ещё заметка", current=date(2026, 9, 8))
    assert entry == {
        "kind": "expense", "category": "кафе и рестораны", "amount": 350,
        "note": "вчера с Олей, 2 кофе #свидание; ещё заметка",
    }


def test_selected_kind_and_explicit_sign_conflict():
    with pytest.raises(ValueError):
        quick_entry("+ кофе 350", "expense")
    with pytest.raises(ValueError):
        quick_entry("- зарплата 350", "income")
    assert quick_entry("возврат 350", "income")["kind"] == "income"


def test_amount_form_parser_remains_strict():
    assert parse_amount("1 250,50 руб.") == 1250.50
    for text in ("-350", "2.345", "NaN", "1,5к", "1000 млн"):
        with pytest.raises(ValueError):
            parse_amount(text)
