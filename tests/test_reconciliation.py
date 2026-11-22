from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import pandas as pd
from openpyxl import load_workbook

from app.services.reconciliation_service import Mapping, ReconciliationError, ReconciliationService


class ReconciliationServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ReconciliationService()
        examples = Path(__file__).resolve().parents[1] / "examples"
        self.result = self.service.reconcile(
            self.service.read(examples / "reconciliation_ours.csv"),
            self.service.read(examples / "reconciliation_counterparty.csv"),
            Mapping("Дата", "Документ", "Операция", "Сумма"),
            Mapping("Transaction date", "Document No", "Type", "Amount"),
        )

    def test_known_demo_result(self):
        summary = self.result["summary"]
        self.assertEqual(summary["confirmed_count"], 2)
        self.assertEqual(summary["operations_difference"], "450.00")
        self.assertEqual(summary["closing_difference"], "450.00")
        self.assertEqual(summary["unexplained_remainder"], "0.00")
        categories = {row["category"] for row in self.result["rows"]}
        self.assertIn("Расхождение суммы", categories)
        self.assertIn("Частичная оплата", categories)
        self.assertIn("Возможный дубль", categories)

    def test_excel_has_required_sheets(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "result.xlsx"
            self.service.export_excel(self.result, output)
            self.assertEqual(load_workbook(output, read_only=True).sheetnames, ["Итоги", "Расхождения", "Ручная проверка"])

    def test_empty_file_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "empty.csv"
            path.write_text("Дата,Документ,Операция,Сумма\n", encoding="utf-8")
            with self.assertRaises(ReconciliationError):
                self.service.read(path)

    def test_ambiguous_match_stays_manual(self):
        ours = pd.DataFrame([["2026-01-01", "A", "x", "50"], ["2026-01-01", "A", "x", "50"]], columns=["d", "n", "o", "a"])
        counter = pd.DataFrame([["2026-01-01", "A", "y", "-100"]], columns=["d", "n", "o", "a"])
        result = self.service.reconcile(ours, counter, Mapping("d", "n", "o", "a"), Mapping("d", "n", "o", "a"))
        self.assertTrue(any(row["category"] == "Частичная оплата" for row in result["manual"]))


if __name__ == "__main__":
    unittest.main()
