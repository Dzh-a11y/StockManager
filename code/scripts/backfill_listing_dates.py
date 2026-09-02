"""Backfill listing dates (ipoDate/outDate) into the local stocks snapshot.

Usage:
    python scripts/backfill_listing_dates.py [--db data/market.sqlite3]
                                            [--request-interval 0.2]

Fetches every A-share stock's listing/delisting date via
``BaostockProvider.fetch_stock_basics`` (``query_stock_basic``, paginated)
and updates ``stocks.listed_on`` / ``stocks.delisted_on`` by code. It only
touches the stocks table columns; it never rewrites daily bars, so already
ingested market data is untouched. After this script runs, any later
incremental / bootstrap sync keeps carrying listing dates automatically
because ``fetch_stocks`` merges them.

This is a network maintenance script; it is NOT part of the product code.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from stock_manager.providers.baostock_provider import BaostockProvider


def _update_listing_dates(
    database_path: Path,
    basics: dict[str, tuple[date | None, date | None]],
) -> int:
    """Update listed_on/delisted_on for every matching stocks row by code."""
    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        updated = 0
        for code, (listed_on, delisted_on) in basics.items():
            cursor = connection.execute(
                """UPDATE stocks
                   SET listed_on = ?, delisted_on = ?
                   WHERE code = ?""",
                (
                    None if listed_on is None else listed_on.isoformat(),
                    None if delisted_on is None else delisted_on.isoformat(),
                    code,
                ),
            )
            updated += cursor.rowcount
        connection.commit()
        return updated
    finally:
        connection.close()


def _count_null_listing_dates(database_path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        total = connection.execute(
            "SELECT COUNT(*) FROM stocks"
        ).fetchone()[0]
        nulls = connection.execute(
            "SELECT COUNT(*) FROM stocks WHERE listed_on IS NULL"
        ).fetchone()[0]
        return int(total), int(nulls)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="backfill listing dates")
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("data/market.sqlite3"),
        help="path to the local SQLite database",
    )
    parser.add_argument(
        "--request-interval",
        type=float,
        default=0.2,
        help="seconds between Baostock requests",
    )
    args: argparse.Namespace = parser.parse_args(argv)

    if not args.db.is_file():
        raise SystemExit(f"database not found: {args.db}")

    total, nulls = _count_null_listing_dates(args.db)
    print(f"before: {total} stocks rows, {nulls} with NULL listed_on")

    provider: Any = BaostockProvider(
        request_interval_seconds=args.request_interval
    )
    basics = provider.fetch_stock_basics()
    print(f"fetched listing dates for {len(basics)} A-share codes")

    updated = _update_listing_dates(args.db, basics)
    total, nulls = _count_null_listing_dates(args.db)
    print(f"updated {updated} rows; after: {total} rows, {nulls} NULL listed_on")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
