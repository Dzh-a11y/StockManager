#!/usr/bin/env python3
"""P5A-1 database integrity verification CLI (offline, read-only).

Prints a JSON report used before/after migrations and coverage backfills:
row counts, per-code bar boundaries, duplicate keys, calendar gaps, database
size, PRAGMA integrity_check and an EXPLAIN QUERY PLAN sample.

Usage:
    python3 scripts/verify_db_integrity.py data/market.sqlite3 [--out report.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stock_manager.storage.integrity import verify_database_integrity


def main() -> int:
    parser = argparse.ArgumentParser(description="verify local database integrity")
    parser.add_argument("db", type=Path, help="path to the SQLite database")
    parser.add_argument("--out", type=Path, default=None, help="write JSON report to a file")
    args = parser.parse_args()
    if not args.db.is_file():
        print(f"database does not exist: {args.db}", file=sys.stderr)
        return 1
    report = verify_database_integrity(args.db)
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())

