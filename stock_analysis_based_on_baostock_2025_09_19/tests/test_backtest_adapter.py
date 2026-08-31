"""P5A-6 Backtrader adapter tests: T+1 execution, determinism, isolation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.backtest.backtrader_engine import BacktraderBacktestEngine
from stock_manager.backtest.contracts import (
    BacktestInputError,
    BacktestMarketData,
)
from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    StockIdentity,
)
from stock_manager.research import (
    PolicySpec,
    ResearchStrategySpec,
    EvaluationSchedule,
    builtin_strategy_specs,
)
from stock_manager.services.historical_screening_executor import (
    EligibilitySnapshot,
)
from stock_manager.storage import SQLiteRepository

QFQ = AdjustmentMethod.QFQ
SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)

DAYS = tuple(
    d
    for d in (
        date(2020, 1, 2) + __import__("datetime").timedelta(days=i)
        for i in range(30)
    )
    if d.weekday() < 5
)[:20]


def _bar(code: str, day: date, close: str) -> DailyBar:
    value = Decimal(close)
    return DailyBar(
        code, day, value, value + Decimal("0.1"), value - Decimal("0.1"), value,
        value, Decimal("1000000"), Decimal("10500000"), True,
    )


def _seed(tmp_path: Path) -> Path:
    db = tmp_path / "market.sqlite3"
    repo = SQLiteRepository(db)
    metadata = DatasetMetadata("market", DAYS[-1], "fixture", NOW, QFQ)
    repo.save_trading_days(DAYS, metadata)
    stocks = tuple(
        StockIdentity(code, f"股票{code[:3]}", "SZSE", False, DAYS[0], None)
        for code in ("000001.SZ", "000002.SZ")
    )
    repo.save_stocks(stocks, metadata)
    bars = []
    for code in ("000001.SZ", "000002.SZ"):
        for i, day in enumerate(DAYS):
            bars.append(_bar(code, day, str(10 + i * 0.2)))
    repo.save_daily_bars(tuple(bars), metadata)
    return db


def _market_data(tmp_path: Path) -> BacktestMarketData:
    db = _seed(tmp_path)
    from stock_manager.read.historical import (
        PointInTimeRequest,
        SQLitePointInTimeReader,
    )

    with SQLitePointInTimeReader(
        db, PointInTimeRequest("market", (), DAYS[0], DAYS[-1], QFQ)
    ) as reader:
        bars = reader.bars_through(DAYS[-1])
        trading_days = reader.trading_days(DAYS[0], DAYS[-1])
    return BacktestMarketData("market", QFQ, trading_days, bars)


def _spec() -> ResearchStrategySpec:
    specs = builtin_strategy_specs(
        template_id="t",
        template_revision=1,
        plan_fingerprint="fp",
        backtest_start=DAYS[5],
        backtest_end=DAYS[-1],
        initial_cash=Decimal("1000000"),
        max_positions=5,
    )
    return specs["selection_rebalance_v1"]


class _Timeline:
    """Minimal eligibility timeline shaped like HistoricalScreeningResult."""

    def __init__(self, snapshots: tuple[EligibilitySnapshot, ...]) -> None:
        self.snapshots = snapshots


def _eligibility_from(day: date) -> _Timeline:
    snapshots = tuple(
        EligibilitySnapshot(d, ("000001.SZ",) if d >= day else (), 1)
        for d in DAYS
        if DAYS[0] <= d <= DAYS[-1]
    )
    return _Timeline(snapshots)


def test_end_to_end_backtest_produces_result(tmp_path: Path) -> None:
    market = _market_data(tmp_path)
    engine = BacktraderBacktestEngine()
    result = engine.run(_spec(), _eligibility_from(DAYS[5]), market)
    assert result.spec_id == "selection_rebalance_v1"
    assert result.metrics.initial_cash == Decimal("1000000")
    assert result.metrics.final_value > 0
    assert result.metrics.trade_count >= 0
    assert result.provenance["cheat_modes"] == "none"
    assert result.provenance["engine"] == "backtrader"


def test_signal_on_t_fills_at_earliest_t_plus_1(tmp_path: Path) -> None:
    """资格从 DAYS[5] 起出现,买入成交日必须晚于 DAYS[5](T+1 开盘)。"""
    market = _market_data(tmp_path)
    engine = BacktraderBacktestEngine()
    result = engine.run(_spec(), _eligibility_from(DAYS[5]), market)
    buys = [trade for trade in result.trades if trade.side == "buy"]
    assert buys, "expected at least one buy"
    for trade in buys:
        assert trade.trading_day > DAYS[5], (
            f"buy on {trade.trading_day} must be after signal day {DAYS[5]}"
        )


def test_eligibility_exit_sells_position(tmp_path: Path) -> None:
    market = _market_data(tmp_path)
    engine = BacktraderBacktestEngine()
    # 资格只出现在 DAYS[5]..DAYS[8],之后退出
    snapshots = tuple(
        EligibilitySnapshot(
            d, ("000001.SZ",) if DAYS[5] <= d <= DAYS[8] else (), 1
        )
        for d in DAYS
    )
    result = engine.run(_spec(), _Timeline(snapshots), market)
    sells = [trade for trade in result.trades if trade.side == "sell"]
    assert sells, "expected a sell after eligibility ends"


def test_deterministic_repeat_runs(tmp_path: Path) -> None:
    market = _market_data(tmp_path)
    engine = BacktraderBacktestEngine()
    first = engine.run(_spec(), _eligibility_from(DAYS[5]), market)
    second = engine.run(_spec(), _eligibility_from(DAYS[5]), market)
    assert first.metrics == second.metrics
    assert first.trades == second.trades


def test_sma_timing_exit_strategy_runs(tmp_path: Path) -> None:
    market = _market_data(tmp_path)
    specs = builtin_strategy_specs(
        template_id="t",
        template_revision=1,
        plan_fingerprint="fp",
        backtest_start=DAYS[5],
        backtest_end=DAYS[-1],
    )
    engine = BacktraderBacktestEngine()
    result = engine.run(specs["selection_sma_timing_v1"], _eligibility_from(DAYS[5]), market)
    assert result.metrics.final_value > 0


def test_fixed_holding_strategy_runs(tmp_path: Path) -> None:
    market = _market_data(tmp_path)
    specs = builtin_strategy_specs(
        template_id="t",
        template_revision=1,
        plan_fingerprint="fp",
        backtest_start=DAYS[5],
        backtest_end=DAYS[-1],
    )
    engine = BacktraderBacktestEngine()
    result = engine.run(specs["selection_fixed_holding_v1"], _eligibility_from(DAYS[5]), market)
    assert result.metrics.final_value > 0


def test_adjustment_mismatch_rejected(tmp_path: Path) -> None:
    market = _market_data(tmp_path)
    market = BacktestMarketData(
        market.dataset_id, AdjustmentMethod.UNADJUSTED, market.trading_days, market.bars
    )
    engine = BacktraderBacktestEngine()
    with pytest.raises(BacktestInputError, match="adjustment"):
        engine.run(_spec(), _eligibility_from(DAYS[5]), market)


def test_domain_contracts_do_not_import_backtrader() -> None:
    import subprocess
    import sys

    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from stock_manager.backtest.contracts import BacktestResult; "
            "from stock_manager.research import ResearchStrategySpec; "
            "assert 'backtrader' not in sys.modules",
        ],
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def test_ranking_policy_order() -> None:
    from stock_manager.backtest.policies import rank_candidates, RankingEntry

    ranked = rank_candidates(
        (
            RankingEntry("A", Decimal("100")),
            RankingEntry("B", Decimal("300")),
            RankingEntry("C", Decimal("100")),
        ),
        2,
    )
    assert ranked == ("B", "A")  # 金额降序,同额按代码升序



def _bar_with(code: str, day: date, *, close: str, preclose: str, volume: str = "1000000") -> DailyBar:
    value = Decimal(close)
    return DailyBar(
        code, day, value, value + Decimal("0.1"), value - Decimal("0.1"), value,
        Decimal(preclose), Decimal(volume), Decimal("10500000"), True,
    )


def test_limit_up_day_blocks_buy_with_warning(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite3"
    repo = SQLiteRepository(db)
    metadata = DatasetMetadata("market", DAYS[-1], "fixture", NOW, QFQ)
    repo.save_trading_days(DAYS, metadata)
    repo.save_stocks(
        (StockIdentity("000001.SZ", "股票", "SZSE", False, DAYS[0], None),),
        metadata,
    )
    bars = []
    for i, day in enumerate(DAYS):
        if day == DAYS[5]:
            bars.append(_bar_with("000001.SZ", day, close="11", preclose="10"))  # 涨停
        elif day < DAYS[5]:
            bars.append(_bar_with("000001.SZ", day, close="10", preclose="10"))
        else:
            bars.append(_bar_with("000001.SZ", day, close=str(10 + i * 0.1), preclose="10"))
    repo.save_daily_bars(tuple(bars), metadata)
    from stock_manager.read.historical import (
        PointInTimeRequest,
        SQLitePointInTimeReader,
    )

    with SQLitePointInTimeReader(
        db, PointInTimeRequest("market", (), DAYS[0], DAYS[-1], QFQ)
    ) as reader:
        market = BacktestMarketData(
            "market", QFQ, reader.trading_days(DAYS[0], DAYS[-1]),
            reader.bars_through(DAYS[-1]),
            (StockIdentity("000001.SZ", "股票", "SZSE", False, DAYS[0], None),),
        )
    engine = BacktraderBacktestEngine()
    result = engine.run(_spec(), _eligibility_from(DAYS[5]), market)
    assert any("limit-up" in warning for warning in result.warnings), result.warnings


def test_suspension_day_blocks_buy_with_warning(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite3"
    repo = SQLiteRepository(db)
    metadata = DatasetMetadata("market", DAYS[-1], "fixture", NOW, QFQ)
    repo.save_trading_days(DAYS, metadata)
    repo.save_stocks(
        (StockIdentity("000001.SZ", "股票", "SZSE", False, DAYS[0], None),),
        metadata,
    )
    bars = []
    for i, day in enumerate(DAYS):
        if day == DAYS[5]:
            bars.append(_bar_with("000001.SZ", day, close="10", preclose="10", volume="0"))  # 停牌
        else:
            bars.append(_bar_with("000001.SZ", day, close=str(10 + i * 0.1), preclose=str(10 + (i - 1) * 0.1)))
    repo.save_daily_bars(tuple(bars), metadata)
    from stock_manager.read.historical import (
        PointInTimeRequest,
        SQLitePointInTimeReader,
    )

    with SQLitePointInTimeReader(
        db, PointInTimeRequest("market", (), DAYS[0], DAYS[-1], QFQ)
    ) as reader:
        market = BacktestMarketData(
            "market", QFQ, reader.trading_days(DAYS[0], DAYS[-1]),
            reader.bars_through(DAYS[-1]),
            (StockIdentity("000001.SZ", "股票", "SZSE", False, DAYS[0], None),),
        )
    engine = BacktraderBacktestEngine()
    result = engine.run(_spec(), _eligibility_from(DAYS[5]), market)
    assert any("suspended" in warning for warning in result.warnings), result.warnings

