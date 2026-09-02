"""SQLite database integrity verification for P5A-1 migrations and coverage checks.

Operational tooling only: it opens the database read-only and never participates
in the repository contract or the screening path.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


_COUNTS_TABLES = (
    "stocks",
    "daily_bars",
    "fundamentals",
    "dividends",
    "trading_days",
    "sync_runs",
    "dataset_metadata",
    "backfill_chunks",
    "dataset_versions",
    "dataset_coverage",
    "backfill_runs_v2",
    "backfill_chunks_v2",
)


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
        ("table", table),
    ).fetchone()
    return row is not None


def _row_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in _COUNTS_TABLES:
        if _table_exists(connection, table):
            counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
    return counts


def _duplicate_key_rows(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """SELECT COUNT(*) FROM (
                SELECT code, trading_day, adjustment, COUNT(*) AS c
                FROM daily_bars GROUP BY code, trading_day, adjustment HAVING c > 1
            )"""
        ).fetchone()[0]
    )


def _bar_coverage(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        """SELECT adjustment, MIN(trading_day) AS earliest,
                  MAX(trading_day) AS latest, COUNT(DISTINCT trading_day) AS days,
                  COUNT(DISTINCT code) AS codes
           FROM daily_bars GROUP BY adjustment"""
    ).fetchall()
    return {
        row["adjustment"]: {
            "earliest": row["earliest"],
            "latest": row["latest"],
            "trading_days": int(row["days"]),
            "codes": int(row["codes"]),
        }
        for row in rows
    }


def _calendar_gaps(connection: sqlite3.Connection) -> list[dict[str, str]]:
    """List calendar days absent from qfq daily_bars (sampled head)."""
    gaps: list[dict[str, str]] = []
    if not _table_exists(connection, "trading_days"):
        return gaps
    rows = connection.execute(
        """SELECT td.trading_day AS day FROM trading_days td
           LEFT JOIN daily_bars b ON b.trading_day = td.trading_day
               AND b.adjustment = ?
           GROUP BY td.trading_day HAVING COUNT(b.code) = 0
           ORDER BY td.trading_day LIMIT 50""",
        ("qfq",),
    ).fetchall()
    return [{"trading_day": row["day"]} for row in rows]


def _per_code_boundaries(connection: sqlite3.Connection) -> dict[str, Any]:
    """First/last bar day and bar count per code (head sample for large dbs)."""
    rows = connection.execute(
        """SELECT code, MIN(trading_day) AS earliest,
                  MAX(trading_day) AS latest, COUNT(*) AS bars
           FROM daily_bars WHERE adjustment = ?
           GROUP BY code ORDER BY code LIMIT 20""",
        ("qfq",),
    ).fetchall()
    return {
        row["code"]: {
            "earliest": row["earliest"],
            "latest": row["latest"],
            "bars": int(row["bars"]),
        }
        for row in rows
    }


def _explain_sample(connection: sqlite3.Connection) -> list[str]:
    """EXPLAIN QUERY PLAN evidence for the historical read shape."""
    plans: list[str] = []
    try:
        rows = connection.execute(
            """EXPLAIN QUERY PLAN SELECT close FROM daily_bars
               WHERE adjustment = ? AND code = ? AND trading_day BETWEEN ? AND ?""",
            ("qfq", "000001.SZ", "2018-01-01", "2026-08-31"),
        ).fetchall()
    except sqlite3.OperationalError:
        return plans
    return [row[3] for row in rows]


def verify_database_integrity(database_path: Path) -> dict[str, Any]:
    """Produce a JSON-serializable integrity report for one SQLite database.

    Never writes: opens with mode=ro + query_only so migrations can be
    verified before and after without altering state.
    """
    from datetime import datetime, timezone

    uri = f"file:{database_path}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        integrity_check = connection.execute("PRAGMA integrity_check").fetchone()[0]
        report: dict[str, Any] = {
            "database_path": str(database_path),
            "size_bytes": database_path.stat().st_size if database_path.is_file() else 0,
            "integrity_check": integrity_check,
            "row_counts": _row_counts(connection),
            "duplicate_daily_bar_keys": _duplicate_key_rows(connection),
            "bar_coverage_by_adjustment": _bar_coverage(connection),
            "calendar_gaps_sample": _calendar_gaps(connection),
            "per_code_boundaries_sample": _per_code_boundaries(connection),
            "explain_query_plan_sample": _explain_sample(connection),
            "verified_at": datetime.now(timezone.utc).isoformat(),
        }
    return report

