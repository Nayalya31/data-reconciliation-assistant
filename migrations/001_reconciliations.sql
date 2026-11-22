CREATE TABLE IF NOT EXISTS reconciliation_uploads (
    token TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('ours', 'counterparty')),
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    columns_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reconciliations (
    id TEXT PRIMARY KEY,
    owner_id TEXT,
    is_demo INTEGER NOT NULL DEFAULT 0,
    ours_name TEXT NOT NULL,
    counterparty_name TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    report_path TEXT NOT NULL,
    chart_path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reconciliations_owner_created
ON reconciliations(owner_id, created_at DESC);
