"""Idempotent schema migrations for the P5 DataSync reconstruction (P5-RD-1).

The database carries ``PRAGMA user_version`` as the schema version. Version 0
is the pre-P5 schema (no batch binding, no bookkeeping tables). Each migration
step runs inside one transaction, is idempotent (guarded by ``PRAGMA
table_info`` / ``IF NOT EXISTS``), and bumps ``user_version`` on success.
A failed step rolls back and leaves the version unchanged so a restart can
retry the same step safely.
"""

from __future__ import annotations

import sqlite3
from typing import Any

#: Current schema version after all migrations.
CURRENT_SCHEMA_VERSION = 1

#: Data tables that gain an immutable-batch binding column.
_BATCH_TABLES: tuple[tuple[str, str], ...] = (
    # (table, index column after batch_id)
    ("daily_bars", "trading_day"),
    ("stocks", "as_of"),
    ("fundamentals", "published_on"),
    ("dividends", "ex_date"),
)

_BOOKKEEPING_TABLES = """
CREATE TABLE IF NOT EXISTS sync_plans (
    plan_id TEXT PRIMARY KEY,
    plan_version INTEGER NOT NULL,
    mode TEXT NOT NULL,
    source TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    universe_policy TEXT NOT NULL,
    target_start TEXT NOT NULL,
    target_end TEXT NOT NULL,
    latest_completed_trading_day TEXT,
    parent_generation TEXT,
    candidate_generation_id TEXT,
    required_data_types TEXT NOT NULL,
    task_count INTEGER NOT NULL,
    plan_fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_tasks (
    task_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    data_type TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    codes TEXT NOT NULL,
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    dependencies TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    not_before TEXT,
    row_count INTEGER,
    error_code TEXT,
    error_message TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sync_tasks_plan ON sync_tasks (plan_id, sequence_no);
CREATE TABLE IF NOT EXISTS candidate_generations (
    candidate_generation_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    parent_generation TEXT,
    write_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_batches (
    batch_id TEXT PRIMARY KEY,
    candidate_generation_id TEXT NOT NULL,
    data_type TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    codes TEXT NOT NULL,
    range_start TEXT NOT NULL,
    range_end TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    source TEXT NOT NULL,
    batch_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ingest_batches_candidate
    ON ingest_batches (candidate_generation_id, data_type);
CREATE TABLE IF NOT EXISTS generation_partitions (
    generation TEXT NOT NULL,
    data_type TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    PRIMARY KEY (generation, data_type, partition_key)
);
CREATE TABLE IF NOT EXISTS coverage_verifications (
    candidate_generation_id TEXT NOT NULL,
    data_type TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    expected_count INTEGER NOT NULL,
    actual_count INTEGER NOT NULL,
    distinct_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    invalid_count INTEGER NOT NULL,
    coverage_ratio TEXT NOT NULL,
    missing_items TEXT NOT NULL,
    status TEXT NOT NULL,
    verified_revision INTEGER NOT NULL,
    manifest_sha256 TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (candidate_generation_id, data_type, partition_key)
);
CREATE TABLE IF NOT EXISTS active_generations (
    dataset_id TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    generation TEXT NOT NULL,
    activated_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, adjustment)
);
CREATE TABLE IF NOT EXISTS seed_imports (
    import_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    schema_version INTEGER,
    status TEXT NOT NULL,
    imported_at TEXT NOT NULL
);
"""


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


_STAGING_TABLES = """
CREATE TABLE IF NOT EXISTS stocks_staging (
    batch_id TEXT NOT NULL,
    code TEXT NOT NULL,
    as_of TEXT NOT NULL,
    name TEXT NOT NULL,
    exchange TEXT NOT NULL,
    is_st INTEGER NOT NULL CHECK (is_st IN (0, 1)),
    listed_on TEXT,
    delisted_on TEXT,
    PRIMARY KEY (batch_id, code, as_of)
);
CREATE TABLE IF NOT EXISTS daily_bars_staging (
    batch_id TEXT NOT NULL,
    code TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    adjustment TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    preclose TEXT NOT NULL,
    volume TEXT NOT NULL,
    amount TEXT NOT NULL,
    is_trading INTEGER NOT NULL CHECK (is_trading IN (0, 1)),
    PRIMARY KEY (batch_id, code, trading_day, adjustment)
);
CREATE TABLE IF NOT EXISTS fundamentals_staging (
    batch_id TEXT NOT NULL,
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    published_on TEXT NOT NULL,
    pe_ttm TEXT,
    pb TEXT,
    source TEXT NOT NULL,
    PRIMARY KEY (batch_id, code, report_date, published_on)
);
CREATE TABLE IF NOT EXISTS dividends_staging (
    batch_id TEXT NOT NULL,
    code TEXT NOT NULL,
    ex_date TEXT NOT NULL,
    cash_dividend_per_share TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (batch_id, code, ex_date, source)
);
"""


def _migrate_to_v1(connection: sqlite3.Connection) -> None:
    """Add immutable-batch binding columns, indexes and bookkeeping tables."""
    for table, time_column in _BATCH_TABLES:
        columns = _column_names(connection, table)
        if "batch_id" not in columns:
            connection.execute(
                f"ALTER TABLE {table} ADD COLUMN batch_id TEXT"
            )
        index_name = f"idx_{table}_batch"
        connection.execute(
            f"CREATE INDEX IF NOT EXISTS {index_name} "
            f"ON {table} (batch_id, {time_column})"
        )
    # dataset_versions gains published-generation identity fields so the
    # published generation record (manifest digest + parent) lives here.
    version_columns = _column_names(connection, "dataset_versions")
    if "manifest_sha256" not in version_columns:
        connection.execute(
            "ALTER TABLE dataset_versions ADD COLUMN manifest_sha256 TEXT"
        )
    if "parent_generation" not in version_columns:
        connection.execute(
            "ALTER TABLE dataset_versions ADD COLUMN parent_generation TEXT"
        )
    connection.executescript(_BOOKKEEPING_TABLES)
    connection.executescript(_STAGING_TABLES)


_MIGRATIONS: dict[int, Any] = {
    1: _migrate_to_v1,
}


def current_schema_version() -> int:
    """Return the schema version the code expects."""
    return CURRENT_SCHEMA_VERSION


def schema_version(connection: sqlite3.Connection) -> int:
    """Read the database's ``PRAGMA user_version``."""
    return int(connection.execute("PRAGMA user_version").fetchone()[0])


def migrate_database(connection: sqlite3.Connection) -> int:
    """Apply all pending migrations in order; return the new version.

    Each migration runs inside its own transaction. ``user_version`` is only
    bumped after the step commits, so an interrupted migration leaves a
    consistent (older) schema that can be retried.
    """
    current = schema_version(connection)
    if current > CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"database schema version {current} is newer than supported "
            f"{CURRENT_SCHEMA_VERSION}; refusing to open"
        )
    while current < CURRENT_SCHEMA_VERSION:
        step = _MIGRATIONS[current + 1]
        try:
            step(connection)
        except sqlite3.Error:
            connection.rollback()
            raise
        connection.execute(f"PRAGMA user_version = {current + 1}")
        connection.commit()
        current += 1
    return current
