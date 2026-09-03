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
CURRENT_SCHEMA_VERSION = 5

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
    updated_at TEXT NOT NULL,
    runner_pid INTEGER
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
    finished_at TEXT,
    progress_json TEXT
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


def _execute_statements(connection: sqlite3.Connection, script: str) -> None:
    """Execute a static SQL script without ``executescript`` auto-commits."""
    statement = ""
    for line in script.splitlines():
        statement = f"{statement}\n{line}"
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete migration SQL statement")


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
    _execute_statements(connection, _BOOKKEEPING_TABLES)
    _execute_statements(connection, _STAGING_TABLES)
    # 幂等补列:sync_tasks 增加逐任务批次进度(老库已迁移过时补上)。
    task_columns = _column_names(connection, "sync_tasks")
    if "progress_json" not in task_columns:
        connection.execute(
            "ALTER TABLE sync_tasks ADD COLUMN progress_json TEXT"
        )
    # 幂等补列:sync_plans 记录 runner 进程 pid(用于检测残留 RUNNING)。
    plan_columns = _column_names(connection, "sync_plans")
    if "runner_pid" not in plan_columns:
        connection.execute(
            "ALTER TABLE sync_plans ADD COLUMN runner_pid INTEGER"
        )


def _migrate_to_v2(connection: sqlite3.Connection) -> None:
    """Preserve every code batch in generation manifests."""
    primary_key = [
        str(row[1])
        for row in connection.execute(
            "PRAGMA table_info(generation_partitions)"
        ).fetchall()
        if int(row[5]) > 0
    ]
    desired_key = ["generation", "data_type", "partition_key", "batch_id"]
    if primary_key != desired_key:
        connection.execute(
            "ALTER TABLE generation_partitions RENAME TO generation_partitions_v1"
        )
        connection.execute(
            """CREATE TABLE generation_partitions (
                   generation TEXT NOT NULL,
                   data_type TEXT NOT NULL,
                   partition_key TEXT NOT NULL,
                   batch_id TEXT NOT NULL,
                   PRIMARY KEY (generation, data_type, partition_key, batch_id)
               )"""
        )
        connection.execute(
            """INSERT INTO generation_partitions
                   (generation, data_type, partition_key, batch_id)
               SELECT generation, data_type, partition_key, batch_id
               FROM generation_partitions_v1"""
        )
        connection.execute("DROP TABLE generation_partitions_v1")


def _migrate_to_v3(connection: sqlite3.Connection) -> None:
    """Remove the rejected provider request quota and persistent circuit."""
    connection.execute("DROP TABLE IF EXISTS provider_request_ledger")
    connection.execute("DROP TABLE IF EXISTS provider_circuit_breakers")


def _migrate_to_v4(connection: sqlite3.Connection) -> None:
    """Add independently versioned P5B index, rate and analysis records."""
    _execute_statements(
        connection,
        """
        CREATE TABLE IF NOT EXISTS index_catalog (
            index_id TEXT PRIMARY KEY,
            provider_code TEXT NOT NULL,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            return_version TEXT NOT NULL,
            source TEXT NOT NULL,
            UNIQUE (provider_code, return_version, source)
        );
        CREATE TABLE IF NOT EXISTS index_daily_bars (
            index_id TEXT NOT NULL,
            trading_day TEXT NOT NULL,
            close TEXT NOT NULL,
            return_version TEXT NOT NULL,
            PRIMARY KEY (index_id, trading_day),
            FOREIGN KEY (index_id) REFERENCES index_catalog(index_id)
        );
        CREATE INDEX IF NOT EXISTS idx_index_daily_bars_day
            ON index_daily_bars (index_id, trading_day);
        CREATE TABLE IF NOT EXISTS deposit_rates (
            term TEXT NOT NULL,
            effective_on TEXT NOT NULL,
            annual_rate TEXT NOT NULL,
            source TEXT NOT NULL,
            PRIMARY KEY (term, effective_on, source)
        );
        CREATE TABLE IF NOT EXISTS capm_results (
            analysis_id TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            as_of TEXT NOT NULL,
            window_days INTEGER NOT NULL,
            benchmark_id TEXT NOT NULL,
            benchmark_return_version TEXT NOT NULL,
            rate_term TEXT NOT NULL,
            alpha_daily TEXT,
            alpha_annualized TEXT,
            beta TEXT,
            r_squared TEXT,
            observation_count INTEGER NOT NULL,
            periods_per_year INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (analysis_id, window_days),
            FOREIGN KEY (benchmark_id) REFERENCES index_catalog(index_id)
        );
        """,
    )


def _migrate_to_v5(connection: sqlite3.Connection) -> None:
    """Bind reference inputs to the same staged/verified publication lifecycle."""
    connection.execute("""CREATE TABLE IF NOT EXISTS sync_runners (
        dataset_id TEXT PRIMARY KEY, status TEXT NOT NULL, runner_pid INTEGER,
        updated_at TEXT NOT NULL, message TEXT NOT NULL)""")
    definitions = {
        "index_catalog": "index_id TEXT, provider_code TEXT, name TEXT, category TEXT, return_version TEXT, source TEXT",
        "index_daily_bars": "index_id TEXT, trading_day TEXT, close TEXT, return_version TEXT",
        "deposit_rates": "term TEXT, effective_on TEXT, annual_rate TEXT, source TEXT",
    }
    keys = {"index_catalog": "index_id", "index_daily_bars": "index_id, trading_day",
            "deposit_rates": "term, effective_on, source"}
    for table, columns in definitions.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if "batch_id" not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN batch_id TEXT")
        connection.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_batch ON {table}(batch_id)")
        connection.execute(
            f"CREATE TABLE IF NOT EXISTS {table}_staging (batch_id TEXT NOT NULL, "
            f"{columns}, PRIMARY KEY (batch_id, {keys[table]}))"
        )


_MIGRATIONS: dict[int, Any] = {
    1: _migrate_to_v1,
    2: _migrate_to_v2,
    3: _migrate_to_v3,
    4: _migrate_to_v4,
    5: _migrate_to_v5,
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
            connection.execute("BEGIN IMMEDIATE")
            step(connection)
            connection.execute(f"PRAGMA user_version = {current + 1}")
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise
        current += 1
    return current
