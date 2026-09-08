import asyncio
import io
import sqlite3
import zipfile
from datetime import date
from xml.etree import ElementTree as ET

import pytest

from app.db import Database
from app.export import export_xlsx
from app.inputs import parse_amount, parse_date, quick_entry, shift_month


@pytest.mark.parametrize("raw, expected", [("1 250,50", 1250.50), ("850", 850), ("2\u00a0000 ₽", 2000), ("0,01", .01)])
def test_amounts(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["1e3", "12abc", "NaN", "-1", "0", "1.234", "1,2,3", "12 34", "1000000000"])
def test_invalid_amounts_are_not_silently_rewritten(raw):
    with pytest.raises(ValueError):
        parse_amount(raw)


def test_quick_entries_and_dates():
    assert quick_entry("продукты 1 250,50; ужин") == {"kind": "expense", "category": "продукты", "amount": 1250.50, "note": "ужин"}
    assert quick_entry("+ 120000 зарплата")["kind"] == "income"
    assert quick_entry("850 такси")["category"] == "транспорт"
    assert quick_entry("зарплата 500")["kind"] == "income"
    assert shift_month("2025-12", 1) == "2026-01"
    assert parse_date("31.08.2025", date(2025, 9, 1)) == "2025-08-31"
    with pytest.raises(ValueError):
        parse_date("02.09.2025", date(2025, 9, 1))
    with pytest.raises(ValueError):
        quick_entry("- 500 продукты", "income")


def test_carry_balance_precision_limits_and_user_isolation(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await db.set_opening(1, 1000)
        first = await db.add_transaction(1, "income", "зарплата", 10000.10, occurred_on="2025-08-31", source_message_id=10)
        assert await db.add_transaction(1, "income", "зарплата", 10000.10, occurred_on="2025-08-31", source_message_id=10) == first
        expense = await db.add_transaction(1, "expense", "продукты", 2500.20, occurred_on="2025-09-01")
        await db.add_transaction(2, "expense", "продукты", 999999, occurred_on="2025-09-01")
        assert (await db.summary(1, "2025-08"))["balance"] == 11000.10
        summary = await db.summary(1, "2025-09")
        assert summary["income"] == 0
        assert summary["expense"] == 2500.20
        assert summary["balance"] == 8499.90
        assert summary["net"] == -2500.20
        assert (await db.transactions(1, "2025-08"))[0]["id"] == first
        assert await db.transaction(2, expense) is None
        assert not await db.delete_transaction(2, expense)
        assert not await db.edit_transaction(2, expense, kind="expense", category="продукты", amount=1)
        await db.set_budget(1, "2025-09", "Продукты", 3000)
        await db.set_budget(1, "2025-08", "продукты", 50)
        report = await db.budget_report(1, "2025-09")
        assert report == [{"category": "продукты", "limit": 3000, "spent": 2500.20}]
        await db.set_budget(1, "2025-09", "продукты", 0)
        assert (await db.budget_report(1, "2025-09"))[0]["limit"] == 0
        await db.set_budget(1, "2025-09", "продукты", None)
        assert (await db.budget_report(1, "2025-09"))[0]["limit"] is None
        assert await db.edit_transaction(1, expense, kind="expense", category="продукты", amount=500, occurred_on="2025-08-30")
        assert not await db.transactions(1, "2025-09")
        assert (await db.summary(1, "2025-09"))["balance"] == 10500.10
        assert await db.delete_transaction(1, expense)
        assert (await db.summary(1, "2025-09"))["balance"] == 11000.10
        await db.select_month(1, "2025-08")
        await db.init()
        assert await db.selected_month(1) == "2025-08"
        assert (await db.summary(1, "2025-08"))["balance"] == 11000.10
    asyncio.run(scenario())


def test_legacy_database_migration_is_repeatable(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE transactions(id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, category TEXT, amount REAL, note TEXT, created_at TEXT)")
        conn.execute("INSERT INTO transactions VALUES(1,7,'expense','Такси',100.25,'поездка','2025-07-31T23:59:59')")
    async def scenario():
        db = Database(path)
        await db.init()
        await db.init()
        row = await db.transaction(7, 1)
        assert row["occurred_on"] == "2025-07-31"
        assert row["amount_minor"] == 10025
        assert row["category"] == "транспорт"
        assert (await db.summary(7, "2025-08"))["balance"] == -100.25
    asyncio.run(scenario())


def test_export_dates_money_text_and_all_sheets(tmp_path):
    async def scenario():
        db = Database(tmp_path / "budget.db")
        await db.init()
        await db.add_transaction(1, "expense", "продукты", 123.45, '=HYPERLINK("bad")\x01', occurred_on="2025-09-01")
        return export_xlsx(await db.summary(1, "2025-09"), await db.transactions(1, "2025-09"), await db.budget_report(1, "2025-09"))
    content = asyncio.run(scenario())
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.testzip() is None
        for filename in archive.namelist():
            ET.fromstring(archive.read(filename))
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        assert len(workbook.find("s:sheets", ns)) == 3
        rows = ET.fromstring(archive.read("xl/worksheets/sheet2.xml"))
        assert not rows.findall(".//s:f", ns)
        assert rows.find(".//s:c[@r='E2']/s:v", ns).text == "123.45"
        assert rows.find(".//s:c[@r='B2']", ns).attrib["t"] == "n"
        assert rows.find(".//s:c[@r='F2']", ns).attrib["t"] == "inlineStr"
        assert rows.find(".//s:c[@r='F2']/s:is/s:t", ns).text == '=HYPERLINK("bad")'
