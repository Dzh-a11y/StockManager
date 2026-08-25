from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    RuleResult,
    SyncOutcome,
    SyncRecord,
    SyncStatus,
)


NOW = datetime(2026, 8, 25, 8, 0, tzinfo=timezone.utc)
DAY = date(2026, 8, 24)


def _bar(**overrides: object) -> DailyBar:
    values: dict[str, object] = {
        "code": "sh.600000",
        "trading_day": DAY,
        "open": Decimal("10"),
        "high": Decimal("10"),
        "low": Decimal("10"),
        "close": Decimal("10"),
        "preclose": Decimal("10"),
        "volume": Decimal("0"),
        "amount": Decimal("0"),
        "is_trading": True,
    }
    values.update(overrides)
    return DailyBar(**values)  # type: ignore[arg-type]


def _sync(status: SyncStatus, **overrides: object) -> SyncRecord:
    values: dict[str, object] = {
        "dataset_id": "daily_bars",
        "trading_day": DAY,
        "status": status,
        "source": "fixture",
        "adjustment": AdjustmentMethod.UNADJUSTED,
        "started_at": NOW,
        "finished_at": NOW,
        "error_message": None,
    }
    values.update(overrides)
    return SyncRecord(**values)  # type: ignore[arg-type]


def test_adjustment_is_explicit_and_has_supported_values() -> None:
    assert [item.value for item in AdjustmentMethod] == ["unadjusted", "qfq", "hfq"]
    with pytest.raises(TypeError):
        DatasetMetadata("daily_bars", DAY, "fixture", NOW)  # type: ignore[call-arg]


def test_dataset_metadata_requires_aware_sync_time() -> None:
    metadata = DatasetMetadata(
        "daily_bars", DAY, "fixture", NOW, AdjustmentMethod.QFQ
    )
    assert metadata.adjustment is AdjustmentMethod.QFQ
    with pytest.raises(ValueError, match="timezone-aware"):
        DatasetMetadata(
            "daily_bars",
            DAY,
            "fixture",
            datetime(2026, 8, 25, 8, 0),
            AdjustmentMethod.QFQ,
        )


def test_daily_bar_accepts_zero_and_equal_high_low() -> None:
    bar = _bar()
    assert bar.high == bar.low
    assert bar.volume == Decimal("0")


@pytest.mark.parametrize(
    "overrides",
    [
        {"high": Decimal("9"), "low": Decimal("10")},
        {"volume": Decimal("-1")},
        {"amount": Decimal("-1")},
    ],
)
def test_daily_bar_rejects_invalid_boundaries(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _bar(**overrides)


def test_rule_result_has_required_structured_fields() -> None:
    result = RuleResult("pe_positive", True, Decimal("12.5"), "> 0", "PE is positive")
    assert result.actual_value == Decimal("12.5")
    assert result.threshold == "> 0"
    assert result.reason == "PE is positive"


def test_success_sync_record_invariants() -> None:
    assert _sync(SyncStatus.SUCCESS).status is SyncStatus.SUCCESS
    with pytest.raises(ValueError, match="finished_at"):
        _sync(SyncStatus.SUCCESS, finished_at=None)
    with pytest.raises(ValueError, match="error_message"):
        _sync(SyncStatus.SUCCESS, error_message="unexpected")


def test_failed_sync_record_invariants() -> None:
    record = _sync(SyncStatus.FAILED, error_message="provider unavailable")
    assert record.error_message == "provider unavailable"
    with pytest.raises(ValueError, match="finished_at"):
        _sync(SyncStatus.FAILED, finished_at=None, error_message="failed")
    with pytest.raises(ValueError, match="error_message"):
        _sync(SyncStatus.FAILED, error_message="")


def test_sync_record_requires_aware_times() -> None:
    with pytest.raises(ValueError, match="started_at"):
        _sync(SyncStatus.RUNNING, started_at=datetime(2026, 8, 25, 8, 0))


def test_skipped_sync_outcome_requires_visible_warning() -> None:
    metadata = DatasetMetadata(
        "daily_bars", DAY, "fixture", NOW, AdjustmentMethod.QFQ
    )
    outcome = SyncOutcome(
        "daily_bars", DAY, SyncStatus.SUCCESS, True, "数据已存在，跳过拉取", metadata
    )
    assert outcome.skipped is True
    with pytest.raises(ValueError, match="warning"):
        SyncOutcome("daily_bars", DAY, SyncStatus.SUCCESS, True, None, metadata)
