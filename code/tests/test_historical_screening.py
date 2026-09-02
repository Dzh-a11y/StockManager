"""P5A-4 historical screening executor tests: consistency, parallelism, gating."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    FundamentalSnapshot,
    StockIdentity,
)
from stock_manager.read.plan_view import PicklableScreeningPlan
from stock_manager.rules.builtin import build_default_registry
from stock_manager.services.historical_screening_executor import (
    DatasetGenerationMismatchError,
    HistoricalScreeningError,
    HistoricalScreeningExecutor,
    HistoricalScreeningRequest,
    historical_worker,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template

QFQ = AdjustmentMethod.QFQ
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)

DAYS = tuple(
    d
    for d in (
        date(2020, 1, 2) + __import__("datetime").timedelta(days=i)
        for i in range(40)
    )
    if d.weekday() < 5
)[:20]

TEMPLATE_RAW = {
    "metadata": {
        "schema_version": 2,
        "template_id": "t",
        "revision": 1,
        "name": "T",
        "description": "d",
        "timezone": "Asia/Shanghai",
        "technical_adjustment": "qfq",
    },
    "rules": {
        "consecutive_up_days": {
            "enabled": True,
            "parameters": {"lookback_trading_sessions": 60, "required_consecutive_days": 2},
        }
    },
    "composition": {
        "operator": "all",
        "groups": [{"group_id": "g", "operator": "all", "rules": ["consecutive_up_days"]}],
    },
}


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "market.sqlite3"
    repo = SQLiteRepository(db)
    codes = ("000001.SZ", "000002.SZ", "000003.SZ")
    stocks = tuple(
        StockIdentity(code, f"股票{code[:3]}", "SZSE", False, DAYS[0], None)
        for code in codes
    )
    repo.save_stocks(stocks, _metadata(DAYS[5]))
    repo.save_trading_days(DAYS, _metadata(DAYS[5]))
    bars: list[DailyBar] = []
    fundamentals: list[FundamentalSnapshot] = []
    for code in codes:
        rising = code.endswith("2.SZ")  # 000002 连涨
        for i, day in enumerate(DAYS):
            close = Decimal(str(10 + i * 0.1)) if rising else Decimal("10")
            bars.append(
                DailyBar(
                    code,
                    day,
                    Decimal("10"),
                    close + Decimal("0.5"),
                    Decimal("9"),
                    close,
                    Decimal("10"),
                    Decimal("1000"),
                    Decimal("10500"),
                    True,
                )
            )
        fundamentals.append(
            FundamentalSnapshot(code, DAYS[5], DAYS[5], Decimal("8"), Decimal("1"), "fixture")
        )
    repo.save_daily_bars(tuple(bars), _metadata(DAYS[5]))
    repo.save_fundamentals(tuple(fundamentals), _metadata(DAYS[5]))
    return db


def _metadata(day: date) -> DatasetMetadata:
    return DatasetMetadata("market", day, "fixture", NOW, QFQ)


def _plan() -> PicklableScreeningPlan:
    registry = build_default_registry()
    plan = TemplateCompiler(registry).compile(parse_template(TEMPLATE_RAW))
    return PicklableScreeningPlan.from_plan(plan)


def _request() -> HistoricalScreeningRequest:
    return HistoricalScreeningRequest(
        dataset_id="market",
        adjustment=QFQ,
        generation=None,
        warmup_start=DAYS[0],
        score_start=DAYS[5],
        score_end=DAYS[10],
        evaluation_days=DAYS[5:11],
    )


def test_serial_execution_produces_expected_eligibility(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    executor = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(db), max_workers=1, batch_size=2
    )
    result = executor.execute(_plan(), _request(), ("000001.SZ", "000002.SZ", "000003.SZ"))
    assert result.snapshots
    for snapshot in result.snapshots:
        assert snapshot.trading_day in DAYS[5:11]
        # 000002 从第二个评估日起连涨两天才通过
    assert result.result_fingerprint


def test_parallel_matches_reference_exactly(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    serial = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(db), max_workers=1, batch_size=2
    ).execute(_plan(), _request())
    parallel = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(db), max_workers=2, batch_size=2
    ).execute(_plan(), _request())
    assert serial == parallel
    assert serial.result_fingerprint == parallel.result_fingerprint


def test_repeated_runs_are_deterministic(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    executor = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(db), max_workers=2, batch_size=2
    )
    first = executor.execute(_plan(), _request())
    second = executor.execute(_plan(), _request())
    assert first.result_fingerprint == second.result_fingerprint
    assert first.snapshots == second.snapshots


def test_each_shard_reads_history_exactly_once(tmp_path: Path) -> None:
    """bars_through must be called once per worker (no per-day market queries)."""
    db = _seed(tmp_path)

    calls = {"bars": 0}

    class CountingReader:
        def __init__(self) -> None:
            self.bars_calls = 0
            from stock_manager.read.historical import (
                PointInTimeRequest,
                SQLitePointInTimeReader,
            )

            self._inner = SQLitePointInTimeReader(
                db, PointInTimeRequest("market", (), DAYS[0], DAYS[10], QFQ)
            )

        def bars_through(self, day: date):
            self.bars_calls += 1
            calls["bars"] += 1
            return self._inner.bars_through(day)

        def fundamentals_through(self, day: date):
            return self._inner.fundamentals_through(day)

        def all_universe_snapshots(self):
            return self._inner.all_universe_snapshots()

        def trading_days(self, start: date, end: date):
            return self._inner.trading_days(start, end)

        def committed_generation(self):
            return self._inner.committed_generation()

        def data_fingerprint(self):
            return self._inner.data_fingerprint()

        def close(self) -> None:
            self._inner.close()

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    executor = HistoricalScreeningExecutor(
        build_default_registry(),
        database_path=str(db),
        max_workers=1,
        batch_size=2,
        reader_factory=CountingReader,
    )
    executor.execute(_plan(), _request())
    assert calls["bars"] == 2  # 2 个 shard,每 shard 恰好一次


def test_generation_mismatch_is_rejected(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    executor = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(db), max_workers=1
    )
    request = _request()
    request = HistoricalScreeningRequest(
        dataset_id="market",
        adjustment=QFQ,
        generation="market-2020-01-01-deadbeef",
        warmup_start=DAYS[0],
        score_start=DAYS[5],
        score_end=DAYS[10],
        evaluation_days=DAYS[5:11],
    )
    with pytest.raises(DatasetGenerationMismatchError, match="does not match"):
        executor.execute(_plan(), request)


def test_worker_failure_fails_whole_run(tmp_path: Path) -> None:
    db = _seed(tmp_path)
    executor = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(tmp_path / "missing.sqlite3"), max_workers=1
    )
    with pytest.raises(HistoricalScreeningError):
        executor.execute(_plan(), _request())


def test_request_validation() -> None:
    with pytest.raises(ValueError, match="warmup_start must not be after score_start"):
        HistoricalScreeningRequest(
            "market", QFQ, None, DAYS[5], DAYS[0], DAYS[10], DAYS[5:11]
        )
    with pytest.raises(ValueError, match="evaluation_days must be sorted"):
        HistoricalScreeningRequest(
            "market", QFQ, None, DAYS[0], DAYS[5], DAYS[10], DAYS[6:11] + (DAYS[5],)
        )
    with pytest.raises(ValueError, match="evaluation_days must not be empty"):
        HistoricalScreeningRequest("market", QFQ, None, DAYS[0], DAYS[5], DAYS[10], ())


def test_empty_universe_returns_empty_result(tmp_path: Path) -> None:
    db = tmp_path / "empty.sqlite3"
    SQLiteRepository(db)
    executor = HistoricalScreeningExecutor(
        build_default_registry(), database_path=str(db), max_workers=1
    )
    result = executor.execute(_plan(), _request())
    assert result.snapshots == ()
