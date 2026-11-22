from __future__ import annotations

import re
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from uuid import uuid4

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
from openpyxl.styles import Font, PatternFill

MONEY = Decimal("0.01")


class ReconciliationError(ValueError):
    pass


@dataclass
class Mapping:
    date: str
    document: str
    operation: str
    amount: str


def _clean(value: Any) -> str:
    if pd.isna(value):
        return ""
    return re.sub(r"\s+", " ", str(value).strip())


def _norm(value: Any) -> str:
    return re.sub(r"[^a-zа-яё0-9]", "", _clean(value).lower())


def _money(value: Any) -> Decimal:
    raw = _clean(value).replace("\u00a0", "").replace(" ", "").replace(",", ".")
    raw = re.sub(r"[^0-9.\-()]", "", raw)
    if raw.startswith("(") and raw.endswith(")"):
        raw = "-" + raw[1:-1]
    try:
        return Decimal(raw).quantize(MONEY, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError) as exc:
        raise ReconciliationError(f"Не удалось распознать сумму «{value}».") from exc


def _date(value: Any) -> str:
    if pd.isna(value) or _clean(value) == "":
        return ""
    text = _clean(value)
    parsed = pd.to_datetime(value, yearfirst=True, errors="coerce") if re.match(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$", text) else pd.to_datetime(value, dayfirst=True, errors="coerce")
    if pd.isna(parsed):
        return _norm(value)
    return parsed.date().isoformat()


class ReconciliationService:
    allowed_extensions = {".csv", ".xlsx"}

    def read(self, path: Path) -> pd.DataFrame:
        if path.suffix.lower() not in self.allowed_extensions:
            raise ReconciliationError("Для сверки поддерживаются XLSX и CSV.")
        try:
            if path.suffix.lower() == ".xlsx":
                frame = pd.read_excel(path, dtype=object)
            else:
                try:
                    frame = pd.read_csv(path, dtype=object, sep=None, engine="python")
                except UnicodeDecodeError:
                    frame = pd.read_csv(path, dtype=object, sep=None, engine="python", encoding="cp1251")
        except Exception as exc:
            raise ReconciliationError("Не удалось прочитать выгрузку. Проверьте формат и заголовки.") from exc
        if frame.empty or not len(frame.columns):
            raise ReconciliationError("Выгрузка не содержит операций.")
        frame.columns = [str(item).strip() for item in frame.columns]
        return frame

    def suggest_mapping(self, columns: list[str]) -> dict[str, str]:
        aliases = {
            "date": ("дата", "date"),
            "document": ("документ", "номер", "doc", "number"),
            "operation": ("операц", "вид", "operation", "type"),
            "amount": ("сумма", "amount", "оборот"),
        }
        result: dict[str, str] = {}
        for field, needles in aliases.items():
            result[field] = next((column for column in columns if any(n in column.lower() for n in needles)), columns[0])
        return result

    def reconcile(self, ours: pd.DataFrame, counterparty: pd.DataFrame, ours_map: Mapping, counter_map: Mapping) -> dict[str, Any]:
        left, left_balances = self._records(ours, ours_map, "Наша выгрузка")
        right, right_balances = self._records(counterparty, counter_map, "Выгрузка контрагента")
        left_groups = self._group(left)
        right_groups = self._group(right)
        used_left: set[int] = set()
        used_right: set[int] = set()
        rows: list[dict[str, Any]] = []

        duplicate_ids = self._duplicates(left) | self._duplicates(right)
        for record in left + right:
            if record["id"] in duplicate_ids:
                rows.append(self._issue("Возможный дубль", record, None, "Повторяются нормализованные реквизиты и сумма"))

        for key in sorted(set(left_groups) | set(right_groups)):
            ls, rs = left_groups.get(key, []), right_groups.get(key, [])
            if len(ls) == 1 and len(rs) == 1:
                lrow, rrow = ls[0], rs[0]
                used_left.add(lrow["id"]); used_right.add(rrow["id"])
                if lrow["amount"] + rrow["amount"] == Decimal("0.00"):
                    rows.append(self._issue("Подтверждено", lrow, rrow, "Реквизиты совпали, суммы зеркальны"))
                else:
                    rows.append(self._issue("Расхождение суммы", lrow, rrow, "Суммы не являются зеркальными"))
            elif ls and rs:
                total_l = sum((item["amount"] for item in ls), Decimal("0"))
                total_r = sum((item["amount"] for item in rs), Decimal("0"))
                category = "Частичная оплата" if total_l + total_r == 0 and (len(ls) > 1 or len(rs) > 1) else "Неоднозначное совпадение"
                reason = "Несколько строк вместе закрывают зеркальную сумму" if category == "Частичная оплата" else "Найдено несколько кандидатов; требуется ручное подтверждение"
                for item in ls: used_left.add(item["id"])
                for item in rs: used_right.add(item["id"])
                rows.append(self._issue(category, ls[0], rs[0], reason, ls, rs))

        for item in left:
            if item["id"] not in used_left:
                rows.append(self._issue("Нет у контрагента", item, None, "Не найден кандидат по дате и номеру документа"))
        for item in right:
            if item["id"] not in used_right:
                rows.append(self._issue("Нет у нас", None, item, "Не найден кандидат по дате и номеру документа"))

        confirmed = [row for row in rows if row["category"] == "Подтверждено"]
        disputes = [row for row in rows if row["category"] != "Подтверждено"]
        opening = self._balance_difference(left_balances.get("opening"), right_balances.get("opening"))
        closing = self._balance_difference(left_balances.get("closing"), right_balances.get("closing"))
        operations = sum((item["amount"] for item in left), Decimal("0")) + sum((item["amount"] for item in right), Decimal("0"))
        unexplained = closing - opening - operations if opening is not None and closing is not None else None
        categories = Counter(row["category"] for row in disputes)
        return {
            "summary": {
                "confirmed_count": len(confirmed), "dispute_count": len(disputes),
                "opening_difference": self._display(opening), "operations_difference": self._display(operations),
                "closing_difference": self._display(closing), "unexplained_remainder": self._display(unexplained),
                "opening_balance_available": opening is not None, "closing_balance_available": closing is not None,
                "categories": dict(categories),
            },
            "rows": [self._json_row(row) for row in rows],
            "manual": [self._json_row(row) for row in disputes if row["category"] in {"Возможный дубль", "Частичная оплата", "Неоднозначное совпадение"}],
        }

    def _records(self, frame: pd.DataFrame, mapping: Mapping, source: str) -> tuple[list[dict[str, Any]], dict[str, Decimal]]:
        missing = [name for name in vars(mapping).values() if name not in frame.columns]
        if missing:
            raise ReconciliationError("Выбранные колонки не найдены: " + ", ".join(missing))
        records, balances = [], {}
        for offset, (_, row) in enumerate(frame.iterrows(), start=2):
            operation = _clean(row[mapping.operation])
            operation_norm = _norm(operation)
            amount = _money(row[mapping.amount])
            if "сальдо" in operation_norm or "balance" in operation_norm:
                target = "opening" if any(word in operation_norm for word in ("нач", "opening")) else "closing" if any(word in operation_norm for word in ("кон", "closing")) else None
                if target:
                    balances[target] = amount
                    continue
            records.append({
                "id": len(records) + (0 if source == "Наша выгрузка" else 1_000_000),
                "source": source, "row": offset, "date": _date(row[mapping.date]),
                "document": _clean(row[mapping.document]), "operation": operation, "amount": amount,
                "key": (_date(row[mapping.date]), _norm(row[mapping.document])),
                "original": {column: _clean(row[column]) for column in frame.columns},
            })
        return records, balances

    @staticmethod
    def _group(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in records: groups[item["key"]].append(item)
        return groups

    @staticmethod
    def _duplicates(records: list[dict[str, Any]]) -> set[int]:
        groups: dict[tuple[Any, ...], list[int]] = defaultdict(list)
        for item in records: groups[(item["key"], item["amount"], _norm(item["operation"]))].append(item["id"])
        return {identifier for ids in groups.values() if len(ids) > 1 for identifier in ids}

    def _issue(self, category: str, ours: dict[str, Any] | None, counter: dict[str, Any] | None, reason: str, ours_group=None, counter_group=None) -> dict[str, Any]:
        return {"category": category, "reason": reason, "ours": ours, "counterparty": counter,
                "ours_group": ours_group or [], "counterparty_group": counter_group or []}

    def _json_row(self, row: dict[str, Any]) -> dict[str, Any]:
        def pack(item):
            if not item: return None
            return {**{k: v for k, v in item.items() if k not in {"key", "original"}}, "amount": self._display(item["amount"]), "original": item["original"]}
        return {**{k: v for k, v in row.items() if k not in {"ours", "counterparty", "ours_group", "counterparty_group"}},
                "ours": pack(row["ours"]), "counterparty": pack(row["counterparty"]),
                "ours_group": [pack(x) for x in row["ours_group"]], "counterparty_group": [pack(x) for x in row["counterparty_group"]]}

    @staticmethod
    def _balance_difference(left: Decimal | None, right: Decimal | None) -> Decimal | None:
        return None if left is None or right is None else (left + right).quantize(MONEY)

    @staticmethod
    def _display(value: Decimal | None) -> str | None:
        return None if value is None else f"{value.quantize(MONEY):.2f}"

    def export_excel(self, result: dict[str, Any], output: Path) -> None:
        summary = result["summary"]
        summary_rows = [
            ("Подтверждено", summary["confirmed_count"]), ("Требует внимания", summary["dispute_count"]),
            ("Разница начального сальдо", summary["opening_difference"] or "Нет данных"),
            ("Расхождения операций", summary["operations_difference"]),
            ("Разница конечного сальдо", summary["closing_difference"] or "Нет данных"),
            ("Необъяснённый остаток", summary["unexplained_remainder"] or "Нельзя рассчитать"),
        ]
        columns = ["Категория", "Причина", "Наш файл", "Наша строка", "Наша дата", "Наш документ", "Наша сумма", "Наша исходная строка", "Файл контрагента", "Строка контрагента", "Дата контрагента", "Документ контрагента", "Сумма контрагента", "Исходная строка контрагента"]
        def flatten(item):
            ours, counter = item.get("ours") or {}, item.get("counterparty") or {}
            return [item["category"], item["reason"], ours.get("source"), ours.get("row"), ours.get("date"), ours.get("document"), ours.get("amount"), json.dumps(ours.get("original", {}), ensure_ascii=False), counter.get("source"), counter.get("row"), counter.get("date"), counter.get("document"), counter.get("amount"), json.dumps(counter.get("original", {}), ensure_ascii=False)]
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame(summary_rows, columns=["Показатель", "Значение"]).to_excel(writer, sheet_name="Итоги", index=False)
            pd.DataFrame([flatten(x) for x in result["rows"] if x["category"] != "Подтверждено"], columns=columns).to_excel(writer, sheet_name="Расхождения", index=False)
            pd.DataFrame([flatten(x) for x in result["manual"]], columns=columns).to_excel(writer, sheet_name="Ручная проверка", index=False)
            for sheet in writer.book.worksheets:
                for cell in sheet[1]:
                    cell.font = Font(bold=True, color="FFFFFF")
                    cell.fill = PatternFill("solid", fgColor="114B5F")
                sheet.freeze_panes = "A2"
                sheet.auto_filter.ref = sheet.dimensions
                for column in sheet.columns:
                    sheet.column_dimensions[column[0].column_letter].width = min(42, max(12, max(len(str(cell.value or "")) for cell in column) + 2))

    def export_chart(self, result: dict[str, Any], output: Path) -> None:
        categories = result["summary"]["categories"]
        if not categories:
            categories = {"Расхождений нет": 1}
        figure, axis = plt.subplots(figsize=(8, 5), dpi=150)
        figure.patch.set_facecolor("#f8f3ea")
        axis.pie(categories.values(), labels=categories.keys(), autopct="%1.0f%%", startangle=90)
        axis.set_title("Расхождения по категориям")
        figure.tight_layout(); figure.savefig(output, bbox_inches="tight"); plt.close(figure)
