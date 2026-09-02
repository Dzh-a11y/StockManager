#!/usr/bin/env python3
"""P5A-4 historical screening benchmark: worker and shard size scaling.

Offline by default (synthetic fixture); --real-db runs against a local
database (e.g. eight-year data) for production sizing. Results are saved
as JSON for the P5A performance gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from stock_manager.domain import AdjustmentMethod, DailyBar, DatasetMetadata, StockIdentity
from stock_manager.read.plan_view import PicklableScreeningPlan
from stock_manager.rules.builtin import build_default_registry
from stock_manager.services.historical_screening_executor import HistoricalScreeningExecutor
from stock_manager.storage import SQLiteRepository
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template


QFQ = AdjustmentMethod.QFQ


def build_fixture_db(path: Path, stock_count: int, day_count: int) -> None:
    """Deterministic synthetic universe and bars (offline)."""
    repo = SQLiteRepository(path)
    start = date(2018, 9, 3)
    days = [start + __import__('datetime').timedelta(days=i) for i in range(day_count)]
    days = [d for d in days if d.weekday() < 5][:day_count]
    from zoneinfo import ZoneInfo

    now = datetime(2026, 8, 25, 18, tzinfo=ZoneInfo('Asia/Shanghai'))
    metadata = DatasetMetadata('market', days[-1], 'fixture', now, QFQ)
    repo.save_trading_days(tuple(days), metadata)
    stocks = tuple(
        StockIdentity(f'{i:06d}.SZ', f'股票{i}', 'SZSE', False, days[0], None)
        for i in range(1, stock_count + 1)
    )
    repo.save_stocks(stocks, metadata)
    bars = []
    for i, stock in enumerate(stocks):
        base = Decimal('10') + Decimal(i % 7)
        for j, day in enumerate(days):
            close = base + Decimal(j % 11) * Decimal('0.1')
            bars.append(
                DailyBar(stock.code, day, close, close + Decimal('0.5'),
                         close - Decimal('0.5'), close, close, Decimal('1000'),
                         Decimal('10500'), True)
            )
    repo.save_daily_bars(tuple(bars), metadata)


def template_plan() -> PicklableScreeningPlan:
    raw = {
        'metadata': {
            'schema_version': 2, 'template_id': 't', 'revision': 1, 'name': 'T',
            'description': 'd', 'timezone': 'Asia/Shanghai',
            'technical_adjustment': 'qfq',
        },
        'rules': {
            'consecutive_up_days': {
                'enabled': True,
                'parameters': {'lookback_trading_sessions': 60, 'required_consecutive_days': 3},
            },
            'n_day_close_above': {
                'enabled': True,
                'parameters': {'lookback_trading_sessions': 20, 'minimum_close': '10'},
            },
        },
        'composition': {
            'operator': 'all',
            'groups': [{'group_id': 'g', 'operator': 'all', 'rules': ['consecutive_up_days', 'n_day_close_above']}],
        },
    }
    registry = build_default_registry()
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    return PicklableScreeningPlan.from_plan(plan)


def main() -> int:
    parser = argparse.ArgumentParser(description='P5A-4 historical screening benchmark')
    parser.add_argument('--real-db', type=Path, default=None, help='run against a real local database')
    parser.add_argument('--stocks', type=int, default=600, help='fixture stock count (offline mode)')
    parser.add_argument('--days', type=int, default=250, help='fixture trading day count (offline mode)')
    parser.add_argument('--out', type=Path, default=None, help='JSON output file')
    args = parser.parse_args()

    if args.real_db is not None:
        db = args.real_db
        stock_count = 0
        day_count = 0
    else:
        tmp = TemporaryDirectory(prefix='p5a4-bench-')
        db = Path(tmp.name) / 'fixture.sqlite3'
        build_fixture_db(db, args.stocks, args.days)
        stock_count, day_count = args.stocks, args.days

    plan = template_plan()
    from stock_manager.read.historical import PointInTimeRequest, SQLitePointInTimeReader

    with SQLitePointInTimeReader(db, PointInTimeRequest('market', (), date(1900, 1, 1), date(2100, 1, 1), QFQ)) as reader:
        snapshots = reader.all_universe_snapshots()
        days = reader.trading_days(date(1900, 1, 1), date(2100, 1, 1))
    codes = sorted({s.code for _a, stocks in snapshots for s in stocks})
    score_days = tuple(days[len(days) // 2 :])
    request_args = dict(
        dataset_id='market', adjustment=QFQ, generation=None,
        warmup_start=days[0], score_start=score_days[0], score_end=score_days[-1],
        evaluation_days=score_days,
    )

    results = {}
    for workers in (1, 2, 4):
        for batch in (100, 250, 500):
            executor = HistoricalScreeningExecutor(
                build_default_registry(), database_path=str(db),
                max_workers=workers, batch_size=batch,
            )
            started = time.perf_counter()
            result = executor.execute(plan, __import__('stock_manager.services.historical_screening_executor', fromlist=['HistoricalScreeningRequest']).HistoricalScreeningRequest(**request_args), codes)
            elapsed = time.perf_counter() - started
            key = f'w{workers}_b{batch}'
            results[key] = {
                'elapsed_seconds': round(elapsed, 3),
                'snapshots': len(result.snapshots),
                'fingerprint': result.result_fingerprint,
            }
            print(f'{key}: {elapsed:.3f}s fingerprint={result.result_fingerprint}', file=sys.stderr)

    report = {
        'benchmark': 'P5A-4 historical screening scaling',
        'fixture': {'stocks': stock_count, 'days': day_count, 'real_db': args.real_db is not None},
        'results': results,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.out is not None:
        args.out.write_text(payload + chr(10), encoding='utf-8')
    return 0


if __name__ == '__main__':
    sys.exit(main())
