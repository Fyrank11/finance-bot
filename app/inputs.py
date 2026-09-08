"""Strict input parsing: never silently turn arbitrary text into a different sum."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

INCOME_CATEGORIES = ("зарплата", "аванс", "премия", "подработка", "проценты и дивиденды", "прочие доходы")
EXPENSE_CATEGORIES = (
    "продукты", "кафе и рестораны", "ипотека / аренда", "кредиты", "ЖКХ", "связь и интернет",
    "транспорт", "здоровье", "одежда", "дом и быт", "спорт", "развлечения", "путешествия",
    "питомцы", "подарки", "образование", "комиссии и проценты", "прочие расходы",
)
ALIASES = {"кафе": "кафе и рестораны", "кофе": "кафе и рестораны", "ресторан": "кафе и рестораны",
           "такси": "транспорт", "бензин": "транспорт", "аптека": "здоровье",
           "ипотека": "ипотека / аренда", "аренда": "ипотека / аренда", "квартальная премия": "премия"}
MONTHS = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
AMOUNT = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d{1,2})?"
QUICK_SCALE = r"(?:тыс\.?|млн\.?|[кk])"
QUICK_CURRENCY = r"(?:₽|руб(?:ль|ля|лей)?\.?)"
QUICK_AMOUNT = rf"{AMOUNT}(?:\s*{QUICK_SCALE})?(?:\s*{QUICK_CURRENCY})?"


def parse_amount(value: str, *, allow_zero: bool = False) -> float:
    cleaned = value.strip()
    if not re.fullmatch(AMOUNT + r"(?:\s*(?:₽|руб\.?))?", cleaned, re.IGNORECASE):
        raise ValueError("Введите сумму, например 1 250,50")
    cleaned = re.sub(r"\s|₽|руб\.?", "", cleaned, flags=re.IGNORECASE).replace(",", ".")
    amount = Decimal(cleaned)
    if amount > Decimal("999999999.99") or amount < 0 or (not allow_zero and amount == 0):
        raise ValueError("Сумма должна быть больше нуля и не превышать 999 999 999,99 ₽")
    return float(amount)


def to_minor(value: float, *, allow_zero: bool = False) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number > Decimal("999999999.99") or number < 0 or (number == 0 and not allow_zero):
            raise ValueError("Некорректная сумма")
        if number != number.quantize(Decimal("0.01")):
            raise ValueError("Не более двух знаков после запятой")
        return int(number * 100)
    except InvalidOperation as exc:
        raise ValueError("Некорректная сумма") from exc


def category_name(value: str) -> str:
    value = " ".join(value.strip().casefold().split())
    if not value or len(value) > 60 or not any(c.isalpha() for c in value):
        raise ValueError("Категория: от 1 до 60 символов, с буквами")
    return ALIASES.get(value, value)


def today(timezone: str = "Europe/Moscow") -> date:
    return datetime.now(ZoneInfo(timezone)).date()


def parse_date(value: str, current: date) -> str:
    try:
        parsed = datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError as exc:
        raise ValueError("Введите дату в формате ДД.ММ.ГГГГ") from exc
    if parsed.year < 2000 or parsed > current:
        raise ValueError("Нужна дата с 01.01.2000 по сегодняшний день")
    return parsed.isoformat()


def valid_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise ValueError("Месяц в формате ГГГГ-ММ")
    first = date.fromisoformat(value + "-01")
    if not 2000 <= first.year <= 9998:
        raise ValueError("Год вне диапазона")
    return value


def shift_month(value: str, delta: int) -> str:
    year, month = map(int, valid_month(value).split("-"))
    index = year * 12 + month - 1 + delta
    return date(index // 12, index % 12 + 1, 1).strftime("%Y-%m")


def month_label(value: str) -> str:
    year, month = map(int, valid_month(value).split("-"))
    return f"{MONTHS[month - 1]} {year}"


def _quick_amount(value: str) -> float:
    """Expand an explicitly written scale with Decimal before validating the sum."""
    match = re.fullmatch(
        rf"(?P<amount>{AMOUNT})(?:\s*(?P<scale>{QUICK_SCALE}))?(?:\s*{QUICK_CURRENCY})?",
        value, re.IGNORECASE,
    )
    if not match:
        raise ValueError("Введите одну сумму, например 1 250,50 или 1,5к")
    amount = Decimal(re.sub(r"\s", "", match["amount"]).replace(",", "."))
    scale = (match["scale"] or "").casefold().rstrip(".")
    if scale:
        amount *= Decimal("1000000" if scale == "млн" else "1000")
    return parse_amount(format(amount, "f"))


def quick_entry(text: str, kind: str | None = None, *, current: date | None = None) -> dict:
    """Parse one sum and category, optional today/yesterday, and a note after ';'.

    The date is returned only when explicitly requested. Callers can supply their
    local current date so a record made around midnight uses the right day.
    """
    body, _, note = text.partition(";")
    body = body.strip()
    relative_dates = re.findall(r"(?<!\S)(сегодня|вчера)(?!\S)", body, re.IGNORECASE)
    if len(relative_dates) > 1:
        raise ValueError("Укажите только одну дату: сегодня или вчера")
    occurred_on = None
    if relative_dates:
        relative = relative_dates[0].casefold()
        current = current or today()
        occurred_on = current - timedelta(days=relative == "вчера")
        if occurred_on.year < 2000:
            raise ValueError("Нужна дата с 01.01.2000 по сегодняшний день")
        body = re.sub(r"(?<!\S)(?:сегодня|вчера)(?!\S)", "", body, flags=re.IGNORECASE).strip()
    explicit_kind = None
    if body.startswith(("+", "-")):
        explicit_kind = "income" if body[0] == "+" else "expense"
        body = body[1:].strip()
    first = re.fullmatch(rf"({QUICK_AMOUNT})\s+(.+)", body, re.IGNORECASE)
    last = re.fullmatch(rf"(.+?)\s+({QUICK_AMOUNT})", body, re.IGNORECASE)
    if first:
        amount, category = first.groups()
    elif last:
        category, amount = last.groups()
    else:
        raise ValueError("Пример: кофе 350 рублей или + зарплата 150000; комментарий")
    # Digits left outside the amount mean multiple sums or malformed input.
    # Numeric details belong in the note, where they cannot alter the sum.
    if re.search(r"\d|(?<!\w)[+-]|[+-](?!\w)|#", category):
        raise ValueError("Укажите одну сумму и категорию; комментарий и #теги — после ;")
    category = category_name(category)
    guessed = "income" if category in INCOME_CATEGORIES else "expense"
    if explicit_kind and kind and explicit_kind != kind:
        raise ValueError("Знак не соответствует выбранному типу операции")
    result = {"kind": kind or explicit_kind or guessed, "category": category,
              "amount": _quick_amount(amount), "note": note.strip()[:500]}
    if occurred_on is not None:
        result["occurred_on"] = occurred_on.isoformat()
    return result
