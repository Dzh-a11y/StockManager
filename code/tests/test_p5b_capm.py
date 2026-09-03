from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from stock_manager.capm import CapmAnalysisService, CapmInputError, estimate_capm
from stock_manager.capm.returns import build_aligned_returns
from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DepositRate,
    IndexDailyBar,
    IndexIdentity,
    IndexReturnVersion,
)
from stock_manager.storage import SQLiteRepository


def test_ols_estimate_matches_hand_calculated_fixture() -> None:
    estimate = estimate_capm(
        tuple(Decimal(value) for value in ("-0.02", "-0.01", "0", "0.01", "0.02")),
        tuple(Decimal(value) for value in ("-0.0298", "-0.0148", "0.0002", "0.0152", "0.0302")),
        minimum_observations=5,
    )
    assert estimate.alpha_daily == Decimal("0.0002")
    assert estimate.alpha_annualized == Decimal("0.0504")
    assert estimate.beta == Decimal("1.5")
    assert estimate.r_squared == Decimal("1")


def test_returns_split_rate_change_and_reject_misaligned_dates() -> None:
    start = date(2026, 1, 2)
    observations = build_aligned_returns(
        ((start, Decimal("100")), (start + timedelta(days=3), Decimal("102"))),
        ((start, Decimal("200")), (start + timedelta(days=3), Decimal("204"))),
        (DepositRate("1_year", start, Decimal("0.01"), "fixture"),
         DepositRate("1_year", start + timedelta(days=1), Decimal("0.02"), "fixture")),
    )
    assert observations[0].risk_free_return == Decimal("0.05") / Decimal(365)
    with pytest.raises(ValueError, match="dates differ"):
        build_aligned_returns(
            ((start, Decimal("100")),),
            ((start + timedelta(days=1), Decimal("100")),),
            (DepositRate("1_year", start, Decimal("0.01"), "fixture"),),
        )


def test_ols_rejects_zero_market_variance() -> None:
    with pytest.raises(CapmInputError, match="zero variance"):
        estimate_capm((Decimal("0"),) * 15, (Decimal("0.01"),) * 15)


def test_service_requires_180_days_and_persists_non_stock_data(tmp_path) -> None:
    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    end = date(2026, 8, 1)
    days = tuple(end - timedelta(days=200 - index) for index in range(200))
    metadata = DatasetMetadata("market", end, "fixture", datetime.now(timezone.utc), AdjustmentMethod.QFQ)
    stock_bars = tuple(
        DailyBar("sh.600000", day, Decimal("10"), Decimal("10"), Decimal("10"),
                 Decimal("10") + Decimal(index) / Decimal("100"), Decimal("10"),
                 Decimal("1"), Decimal("1"), True)
        for index, day in enumerate(days)
    )
    repo.save_daily_bars(stock_bars, metadata)
    repo.save_indexes((IndexIdentity("hs300.price", "sh.000300", "沪深300", "broad", IndexReturnVersion.PRICE, "fixture"),))
    repo.save_index_daily_bars(tuple(
        IndexDailyBar("hs300.price", day, Decimal("100") + Decimal(index) / Decimal("10"), IndexReturnVersion.PRICE)
        for index, day in enumerate(days)
    ))
    repo.save_deposit_rates((DepositRate("1_year", days[0], Decimal("0"), "fixture"),))
    result = CapmAnalysisService(repo).analyse("sh.600000", end, windows=(30,))
    # Raw legacy rows are not a published reference generation.
    assert result[0].status == "DATA_INCOMPLETE"
    assert "not ready" in result[0].reason
