from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import Settings, get_settings


class ReconciliationRepository:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _connect(self) -> sqlite3.Connection:
        self.settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.settings.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def migrate(self) -> None:
        migration_dir = Path(__file__).resolve().parents[2] / "migrations"
        with self._connect() as connection:
            for migration in sorted(migration_dir.glob("*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))

    def save_upload(
        self, *, token: str, owner_id: str, side: str, original_name: str, stored_path: str, columns: list[str]
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO reconciliation_uploads VALUES (?, ?, ?, ?, ?, ?, ?)",
                (token, owner_id, side, original_name, stored_path, json.dumps(columns, ensure_ascii=False), self.now()),
            )

    def get_upload(self, token: str, owner_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM reconciliation_uploads WHERE token = ? AND owner_id = ?", (token, owner_id)
            ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["columns"] = json.loads(item.pop("columns_json"))
        return item

    def save_result(self, record: dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO reconciliations
                   (id, owner_id, is_demo, ours_name, counterparty_name, summary_json,
                    result_json, report_path, chart_path, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["id"], record.get("owner_id"), int(record.get("is_demo", False)),
                    record["ours_name"], record["counterparty_name"],
                    json.dumps(record["summary"], ensure_ascii=False),
                    json.dumps(record["result"], ensure_ascii=False),
                    record["report_path"], record["chart_path"], self.now(),
                ),
            )

    def get_result(self, reconciliation_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM reconciliations WHERE id = ?", (reconciliation_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["is_demo"] = bool(item["is_demo"])
        item["summary"] = json.loads(item.pop("summary_json"))
        item["result"] = json.loads(item.pop("result_json"))
        return item

    def list_results(self, owner_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, ours_name, counterparty_name, summary_json, created_at "
                "FROM reconciliations WHERE owner_id = ? ORDER BY created_at DESC LIMIT 30",
                (owner_id,),
            ).fetchall()
        return [{**dict(row), "summary": json.loads(row["summary_json"])} for row in rows]

    @staticmethod
    def now() -> str:
        return datetime.now(tz=UTC).isoformat()
