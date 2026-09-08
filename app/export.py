"""Native XLSX snapshots for the deployed bot, with no server-side Office dependency.

All user text is an inline string, never a formula. Dates and money are numeric.
"""
from __future__ import annotations

import io
import re
import zipfile
from datetime import date
from decimal import Decimal
from xml.etree.ElementTree import Element, SubElement, tostring

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT = "http://schemas.openxmlformats.org/package/2006/content-types"


def xml(element: Element) -> bytes:
    return tostring(element, encoding="utf-8", xml_declaration=True)


def sheet(rows: list[list], widths: list[int]) -> bytes:
    root = Element("worksheet", xmlns=NS)
    views = SubElement(root, "sheetViews")
    view = SubElement(views, "sheetView", workbookViewId="0", showGridLines="0")
    SubElement(view, "pane", ySplit="1", topLeftCell="A2", activePane="bottomLeft", state="frozen")
    columns = SubElement(root, "cols")
    for i, width in enumerate(widths, 1):
        SubElement(columns, "col", min=str(i), max=str(i), width=str(width), customWidth="1")
    data = SubElement(root, "sheetData")
    for row_number, values in enumerate(rows, 1):
        row = SubElement(data, "row", r=str(row_number), ht="24", customHeight="1")
        for col_number, value in enumerate(values):
            attrs = {"r": f"{chr(65 + col_number)}{row_number}"}
            style = "1" if row_number == 1 else "0"
            if isinstance(value, date):
                value, style = (value - date(1899, 12, 30)).days, "3"
            elif isinstance(value, (float, Decimal)):
                style = "2"
            attrs["s"] = style
            if isinstance(value, (int, float, Decimal)):
                cell = SubElement(row, "c", **attrs, t="n")
                SubElement(cell, "v").text = str(value)
            else:
                cell = SubElement(row, "c", **attrs, t="inlineStr")
                text = SubElement(SubElement(cell, "is"), "t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"})
                text.text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))[:32767]
    if len(rows) > 1:
        SubElement(root, "autoFilter", ref=f"A1:{chr(64 + len(widths))}{len(rows)}")
    return xml(root)


def export_xlsx(summary: dict, transactions: list[dict], budgets: list[dict]) -> bytes:
    summary_rows = [["Показатель", "Значение"], ["Месяц", summary["month"]],
                    ["Доходы, ₽", summary["income"]], ["Расходы, ₽", summary["expense"]],
                    ["Разница за месяц, ₽", summary["net"]], ["Расчётный остаток на конец месяца, ₽", summary["balance"]],
                    ["Начальные деньги, ₽", summary["opening"]]]
    tx_rows = [["№", "Дата", "Тип", "Категория", "Сумма, ₽", "Комментарий"]]
    tx_rows += [[r["id"], date.fromisoformat(r["occurred_on"]), "Доход" if r["kind"] == "income" else "Расход",
                 r["category"], Decimal(r["amount_minor"]) / 100, r["note"]] for r in transactions]
    budget_rows = [["Категория", "Лимит, ₽", "Потрачено, ₽", "Остаток лимита, ₽"]]
    budget_rows += [[r["category"], r["limit"] if r["limit"] is not None else "Не задан", r["spent"],
                     round(r["limit"] - r["spent"], 2) if r["limit"] is not None else ""] for r in budgets]
    worksheets = [("Обзор", summary_rows, [49, 25]), ("Операции", tx_rows, [10, 16, 14, 30, 20, 65]),
                  ("Лимиты", budget_rows, [30, 22, 22, 24])]
    workbook = Element("workbook", xmlns=NS)
    sheets = SubElement(workbook, "sheets")
    rels = Element("Relationships", xmlns=PKG)
    content = Element("Types", xmlns=CONTENT)
    SubElement(content, "Default", Extension="rels", ContentType="application/vnd.openxmlformats-package.relationships+xml")
    SubElement(content, "Default", Extension="xml", ContentType="application/xml")
    for part, subtype in [("workbook.xml", "sheet.main"), ("styles.xml", "styles")]:
        SubElement(content, "Override", PartName=f"/xl/{part}", ContentType=f"application/vnd.openxmlformats-officedocument.spreadsheetml.{subtype}+xml")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for i, (name, rows, widths) in enumerate(worksheets, 1):
            SubElement(sheets, "sheet", {"name": name, "sheetId": str(i), f"{{{REL}}}id": f"rId{i}"})
            SubElement(rels, "Relationship", Id=f"rId{i}", Type=REL + "/worksheet", Target=f"worksheets/sheet{i}.xml")
            SubElement(content, "Override", PartName=f"/xl/worksheets/sheet{i}.xml", ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml")
            archive.writestr(f"xl/worksheets/sheet{i}.xml", sheet(rows, widths))
        SubElement(rels, "Relationship", Id="rIdStyles", Type=REL + "/styles", Target="styles.xml")
        root_rels = Element("Relationships", xmlns=PKG)
        SubElement(root_rels, "Relationship", Id="rId1", Type=REL + "/officeDocument", Target="xl/workbook.xml")
        archive.writestr("_rels/.rels", xml(root_rels))
        archive.writestr("[Content_Types].xml", xml(content))
        archive.writestr("xl/workbook.xml", xml(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", xml(rels))
        archive.writestr("xl/styles.xml", STYLES)
    return buffer.getvalue()


STYLES = f'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{NS}">
<numFmts count="2"><numFmt numFmtId="164" formatCode="#,##0.00"/><numFmt numFmtId="165" formatCode="dd/mm/yyyy"/></numFmts>
<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF172E3B"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''.encode()
