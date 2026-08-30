#!/usr/bin/env python3
"""P4-6 performance comparison: serial vs concurrent read and screen.

Runs on the SAME dataset as the P4-0 baseline and verifies result equality
before accepting any timing. Offline only; never touches a provider.
"""

from __future__ import annotations

import json
import resource
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from stock_manager.domain import AdjustmentMethod
from stock_manager.read import (
    MarketDataReadRequest,
    MarketDataReadService,
    SQLiteMarketDataReaderFactory,
)
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
DATASET = "market"
ADJUSTMENT = AdjustmentMethod.QFQ


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value * 1024 if sys.platform.startswith("linux") else value


def timed(label, fn):
    start = time.perf_counter()
    result = fn()
    return {"label": label, "elapsed_seconds": round(time.perf_counter() - start, 4),
            "rows": len(result) if hasattr(result, "__len__") else 0}, result


def main() -> int:
    repo = SQLiteRepository(DB_PATH)
    latest = repo.get_latest_dataset_metadata(DATASET, ADJUSTMENT)
    if latest is None:
        print("no dataset", file=sys.stderr)
        return 1
    target_day = latest.trading_day
    stocks = repo.get_stocks(target_day)
    all_codes = tuple(sorted(s.code for s in stocks))
    start_window = target_day - timedelta(days=365)

    factory = SQLiteMarketDataReaderFactory(DB_PATH)
    request = MarketDataReadRequest(
        DATASET, all_codes, start_window, target_day, ADJUSTMENT, 500,
        include_fundamentals=True,
        dividends_start=date(target_day.year - 3, 1, 1),
    )

    print(f"universe={len(all_codes)} target={target_day.isoformat()}", file=sys.stderr)

    # 1) 读取：串行 vs 并发（结果必须逐项相等）
    serial_service = MarketDataReadService(factory, max_workers=1, batch_size=500)
    parallel_service = MarketDataReadService(
        factory, max_workers=4, batch_size=500, small_data_serial_threshold=0
    )
    m_read_serial, serial_result = timed("read serial max_workers=1", lambda: serial_service.read(request))
    m_read_parallel, parallel_result = timed("read parallel max_workers=4", lambda: parallel_service.read(request))

    equal = (
        serial_result.bars == parallel_result.bars
        and serial_result.fundamentals == parallel_result.fundamentals
        and serial_result.dividends == parallel_result.dividends
        and serial_result.snapshot == parallel_result.snapshot
    )
    print(f"read equality: {equal}", file=sys.stderr)
    if not equal:
        print("READ RESULTS MISMATCH - refusing to report timing", file=sys.stderr)
        return 2

    # 2) 参数化筛选：串行 vs 分片并发（结果必须逐项相等）
    registry = build_default_registry()
    raw = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    serial_screen = ParameterizedScreeningService(
        repo, registry, reader_factory=factory, max_workers=1
    )
    parallel_screen = ParameterizedScreeningService(
        repo, registry, reader_factory=factory, max_workers=4,
        small_data_serial_threshold=0,
    )
    m_screen_serial, screen_serial = timed(
        "screen serial max_workers=1", lambda: serial_screen.screen(plan, DATASET, target_day, ADJUSTMENT)
    )
    m_screen_parallel, screen_parallel = timed(
        "screen parallel max_workers=4", lambda: parallel_screen.screen(plan, DATASET, target_day, ADJUSTMENT)
    )
    screen_equal = screen_serial == screen_parallel
    print(f"screen equality: {screen_equal}", file=sys.stderr)
    if not screen_equal:
        print("SCREEN RESULTS MISMATCH - refusing to report timing", file=sys.stderr)
        return 3

    report = {
        "benchmark": "P4-6 concurrent vs serial comparison",
        "machine": "Apple M5 Pro, 48 GB unified memory",
        "target_day": target_day.isoformat(),
        "universe_size": len(all_codes),
        "batch_size": 500,
        "max_workers_parallel": 4,
        "small_data_serial_threshold": 0,
        "read_equality": equal,
        "screen_equality": screen_equal,
        "measurements": [
            m_read_serial,
            m_read_parallel,
            m_screen_serial,
            m_screen_parallel,
        ],
        "peak_rss_bytes": peak_rss_bytes(),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
