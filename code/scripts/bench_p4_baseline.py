"""P4-0 baseline benchmark: measure the existing serial read/screen paths.

Reads ONLY the local SQLite database (offline). Produces a reproducible
performance record: hardware/software versions, database scale, elapsed time,
returned row counts, peak RSS memory and SQLite query plans.

Usage:
    .venv/bin/python scripts/bench_p4_baseline.py
"""

from __future__ import annotations

import json
import resource
import sqlite3
import sys
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from stock_manager.domain import AdjustmentMethod
from stock_manager.rules.builtin import build_default_registry
from stock_manager.services.parameterized_screening_service import (
    ParameterizedScreeningService,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "market.sqlite3"
TEMPLATE_PATH = ROOT / "config" / "rule_templates" / "system-default.json"
DATASET_ID = "market"
ADJUSTMENT = AdjustmentMethod.QFQ


def _peak_rss_bytes() -> int:
    # macOS/BSD 的 ru_maxrss 单位为字节; Linux 为 KB。
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform.startswith("linux"):
        return value * 1024
    return value


def _measure(repo: SQLiteRepository, label: str, fn) -> dict[str, object]:
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    rows = len(result) if hasattr(result, "__len__") else 0
    return {"label": label, "elapsed_seconds": round(elapsed, 4), "rows": rows}


def main() -> int:
    if not DB_PATH.is_file():
        print(f"database not found: {DB_PATH}", file=sys.stderr)
        return 1
    import platform

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    bar_count = conn.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
    stock_count = conn.execute("SELECT COUNT(DISTINCT code) FROM daily_bars").fetchone()[0]
    day_range = conn.execute(
        "SELECT MIN(trading_day), MAX(trading_day) FROM daily_bars"
    ).fetchone()
    plan_rows = conn.execute(
        """SELECT * FROM daily_bars WHERE code IN (?, ?)
           AND trading_day BETWEEN ? AND ? AND adjustment = ?
           ORDER BY code, trading_day""",
        ("sh.600000", "sz.000001", "2025-09-01", "2026-08-28", "qfq"),
    ).fetchall()
    conn.close()

    env = {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "db_bytes": DB_PATH.stat().st_size,
        "db_daily_bar_count": int(bar_count),
        "db_stock_count": int(stock_count),
        "db_day_range": [day_range[0], day_range[1]],
        "plan_rows": len(plan_rows),
    }

    repo = SQLiteRepository(DB_PATH)
    latest = repo.get_latest_dataset_metadata(DATASET_ID, ADJUSTMENT)
    if latest is None:
        print("no dataset metadata", file=sys.stderr)
        return 1
    target_day = latest.trading_day
    stocks = repo.get_stocks(target_day)
    all_codes = tuple(sorted(stock.code for stock in stocks))
    start_window = target_day - timedelta(days=365)
    sample_codes = all_codes[:200]

    # 1) serial full-universe daily-bar read (retention window ~365 natural days)
    m1 = _measure(
        repo,
        "serial get_daily_bars all codes 365d",
        lambda: repo.get_daily_bars(all_codes, start_window, target_day, ADJUSTMENT),
    )
    # 2) serial sample read (200 codes)
    m2 = _measure(
        repo,
        "serial get_daily_bars 200 codes 365d",
        lambda: repo.get_daily_bars(sample_codes, start_window, target_day, ADJUSTMENT),
    )
    # 3) serial parameterized screening over the full universe
    registry = build_default_registry()
    raw = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    service = ParameterizedScreeningService(repo, registry)

    def run_screen() -> object:
        results = service.screen(plan, DATASET_ID, target_day, ADJUSTMENT)
        return results

    m3 = _measure(repo, "serial parameterized screen all codes", run_screen)

    # query plans for the hot queries
    conn2 = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    qp = {}
    for label, sql, params in [
        ("bars_in", "EXPLAIN QUERY PLAN SELECT * FROM daily_bars WHERE code IN (?,?) AND trading_day BETWEEN ? AND ? AND adjustment = ?", ("sh.600000", "sz.000001", "2025-09-01", "2026-08-28", "qfq")),
        ("stocks_asof", "EXPLAIN QUERY PLAN SELECT * FROM stocks WHERE as_of = ? ORDER BY code", (target_day.isoformat(),)),
        ("fundamentals_in", "EXPLAIN QUERY PLAN SELECT * FROM fundamentals WHERE code IN (?,?) AND published_on <= ?", ("sh.600000", "sz.000001", target_day.isoformat())),
    ]:
        qp[label] = [list(row) for row in conn2.execute(sql, params).fetchall()]
    conn2.close()

    report = {
        "benchmark": "P4-0 baseline (serial existing paths)",
        "date": date.today().isoformat(),
        "env": env,
        "target_day": target_day.isoformat(),
        "universe_size": len(all_codes),
        "measurements": [m1, m2, m3],
        "peak_rss_bytes": _peak_rss_bytes(),
        "query_plans": qp,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())