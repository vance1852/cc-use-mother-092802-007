"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_boundaries (
    boundary_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    name TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_meters (
    meter_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES retrofit_boundaries(boundary_id),
    meter_type TEXT NOT NULL CHECK(meter_type IN ('steam','output','energy')),
    unit TEXT NOT NULL,
    min_range REAL,
    max_range REAL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_calibrations (
    calibration_id TEXT PRIMARY KEY,
    meter_id TEXT NOT NULL REFERENCES retrofit_meters(meter_id),
    certificate_no TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_batches (
    batch_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES retrofit_boundaries(boundary_id),
    product_code TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK(period_kind IN ('baseline','verification')),
    shift_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    output REAL NOT NULL CHECK(output >= 0),
    steam_unit_cost REAL NOT NULL CHECK(steam_unit_cost >= 0),
    downtime_minutes INTEGER NOT NULL DEFAULT 0 CHECK(downtime_minutes >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_factors (
    factor_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES retrofit_boundaries(boundary_id),
    product_code TEXT,
    period_kind TEXT NOT NULL CHECK(period_kind IN ('baseline','verification')),
    factor REAL NOT NULL CHECK(factor > 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','approved','rejected')),
    proposed_by TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_readings (
    reading_id TEXT PRIMARY KEY,
    meter_id TEXT NOT NULL REFERENCES retrofit_meters(meter_id),
    batch_id TEXT NOT NULL REFERENCES retrofit_batches(batch_id),
    value REAL,
    status TEXT NOT NULL CHECK(status IN ('recorded','missing','anomalous')),
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    UNIQUE(meter_id, batch_id)
);
CREATE TABLE IF NOT EXISTS retrofit_verifications (
    verification_id TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL REFERENCES retrofit_boundaries(boundary_id),
    parent_verification_id TEXT REFERENCES retrofit_verifications(verification_id),
    name TEXT NOT NULL,
    baseline_start TEXT NOT NULL,
    baseline_end TEXT NOT NULL,
    verification_start TEXT NOT NULL,
    verification_end TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    frozen_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('frozen','confirmed','closed','calibration_hold','superseded')),
    sufficient_data INTEGER NOT NULL CHECK(sufficient_data IN (0, 1)),
    results_json TEXT,
    snapshot_hash TEXT NOT NULL,
    alternate_id TEXT,
    confirmed_by TEXT,
    confirmed_at TEXT,
    review_note TEXT,
    closed_by TEXT,
    closed_at TEXT,
    hold_since TEXT,
    hold_reason TEXT
);
CREATE TABLE IF NOT EXISTS retrofit_verification_meters (
    verification_id TEXT NOT NULL REFERENCES retrofit_verifications(verification_id),
    meter_id TEXT NOT NULL,
    meter_type TEXT NOT NULL,
    unit TEXT NOT NULL,
    min_range REAL,
    max_range REAL,
    PRIMARY KEY(verification_id, meter_id)
);
CREATE TABLE IF NOT EXISTS retrofit_verification_batches (
    verification_id TEXT NOT NULL REFERENCES retrofit_verifications(verification_id),
    batch_id TEXT NOT NULL,
    period_kind TEXT NOT NULL,
    product_code TEXT NOT NULL,
    steam_total REAL NOT NULL,
    output REAL NOT NULL,
    steam_unit_cost REAL NOT NULL,
    factor_id TEXT,
    factor_value REAL NOT NULL,
    adjusted_rate REAL NOT NULL,
    included INTEGER NOT NULL CHECK(included IN (0, 1)),
    exclusion_reason TEXT,
    PRIMARY KEY(verification_id, batch_id)
);
CREATE TABLE IF NOT EXISTS retrofit_verification_sources (
    verification_id TEXT NOT NULL REFERENCES retrofit_verifications(verification_id),
    batch_id TEXT NOT NULL,
    meter_id TEXT NOT NULL,
    reading_id TEXT NOT NULL,
    value REAL,
    reading_status TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    calibration_id TEXT,
    calibration_valid INTEGER NOT NULL CHECK(calibration_valid IN (0, 1)),
    PRIMARY KEY(verification_id, reading_id)
);
CREATE TABLE IF NOT EXISTS retrofit_alternates (
    alternate_id TEXT PRIMARY KEY,
    verification_id TEXT NOT NULL REFERENCES retrofit_verifications(verification_id),
    proposed_by TEXT NOT NULL,
    excluded_batches_json TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('proposed','accepted','rejected')),
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    child_verification_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retrofit_corrections (
    correction_id TEXT PRIMARY KEY,
    meter_id TEXT NOT NULL REFERENCES retrofit_meters(meter_id),
    calibration_id TEXT,
    effective_from TEXT NOT NULL,
    reason TEXT NOT NULL,
    reported_by TEXT NOT NULL,
    reported_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','cleared'))
);
CREATE TABLE IF NOT EXISTS retrofit_impacts (
    correction_id TEXT NOT NULL REFERENCES retrofit_corrections(correction_id),
    verification_id TEXT NOT NULL REFERENCES retrofit_verifications(verification_id),
    action TEXT NOT NULL CHECK(action IN ('hold','disclosure')),
    prior_status TEXT NOT NULL,
    affected_measurements_json TEXT NOT NULL,
    savings_amount_at_risk REAL NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(correction_id, verification_id)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
