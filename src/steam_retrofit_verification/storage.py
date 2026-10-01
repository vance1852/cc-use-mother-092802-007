"""在基础库 SQLite 数据库上扩展技改核验所需的表。

不另开连接：核验领域与基础主体、场所、审计链共用同一个 Database 事务。
"""

from __future__ import annotations

from beverage_ops_foundation.storage import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS equipment_boundaries (
    boundary_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL CHECK(version >= 1),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meter_points (
    meter_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES equipment_boundaries(boundary_id),
    name TEXT NOT NULL,
    metric TEXT NOT NULL CHECK(metric IN ('steam','product')),
    unit TEXT NOT NULL,
    calibration_due TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS calibrations (
    calibration_id TEXT PRIMARY KEY,
    meter_id TEXT NOT NULL REFERENCES meter_points(meter_id),
    certified_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    certificate_ref TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_batches (
    batch_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES equipment_boundaries(boundary_id),
    product_code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    output REAL NOT NULL CHECK(output > 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steam_readings (
    reading_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES production_batches(batch_id),
    meter_id TEXT NOT NULL REFERENCES meter_points(meter_id),
    steam_kg REAL NOT NULL,
    measured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_readings_batch ON steam_readings(batch_id, received_at);
CREATE TABLE IF NOT EXISTS adjustment_factors (
    factor_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES equipment_boundaries(boundary_id),
    code TEXT NOT NULL,
    value REAL NOT NULL CHECK(value > 0),
    reason TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    approved_at TEXT NOT NULL,
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    UNIQUE(boundary_id, code)
);
CREATE TABLE IF NOT EXISTS meter_issues (
    issue_id TEXT PRIMARY KEY,
    meter_id TEXT NOT NULL REFERENCES meter_points(meter_id),
    issue_from TEXT NOT NULL,
    issue_to TEXT NOT NULL,
    note TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS verifications (
    verification_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES equipment_boundaries(boundary_id),
    title TEXT NOT NULL,
    baseline_start TEXT NOT NULL,
    baseline_end TEXT NOT NULL,
    verification_start TEXT NOT NULL,
    verification_end TEXT NOT NULL,
    confidence_level REAL NOT NULL CHECK(confidence_level > 0 AND confidence_level < 1),
    status TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('standard','engineering_alternative')),
    engineering_rationale TEXT NOT NULL DEFAULT '',
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    submitted_at TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    snapshot_id TEXT,
    standard_result_json TEXT,
    result_json TEXT,
    settle_state TEXT NOT NULL DEFAULT 'unsettled' CHECK(settle_state IN ('unsettled','settled')),
    settled_at TEXT,
    closed_at TEXT,
    version INTEGER NOT NULL CHECK(version >= 1)
);
CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
    frozen_at TEXT NOT NULL,
    cutoff_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
    snapshot_id TEXT NOT NULL REFERENCES snapshots(snapshot_id),
    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
    reviewer_id TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engineering_overrides (
    override_id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
    batch_id TEXT NOT NULL REFERENCES production_batches(batch_id),
    steam_kg REAL NOT NULL CHECK(steam_kg > 0),
    rationale TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(verification_id, batch_id)
);
CREATE TABLE IF NOT EXISTS corrections (
    correction_id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES verifications(verification_id),
    issue_id TEXT NOT NULL REFERENCES meter_issues(issue_id),
    reason TEXT NOT NULL,
    impacted_meter_ids_json TEXT NOT NULL,
    disclosed_impact_json TEXT NOT NULL,
    recorded_by TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
"""


class VerificationStorage:
    """在基础库连接上建表并暴露核验 schema 初始化。"""

    def __init__(self, database: Database) -> None:
        self.database = database
        database.connection.executescript(SCHEMA)
