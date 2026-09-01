"""Offline tests for P5-RD-3: SerialFetchWorker and provider safety boundary."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
    SyncTask,
    SyncTaskStatus,
)
from stock_manager.sync.worker import (
    ConcurrentProviderAccessError,
    FetchTaskProvider,
    ProviderFetchError,
    SerialFetchWorker,
    TaskExecutionResult,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)


class FakeProvider:
    """Deterministic in-memory provider recording every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.source_name = "fake"
        self._bar = DailyBar(
            "sh.600000", DAY, Decimal("10"), Decimal("11"), Decimal("9"),
            Decimal("10.5"), Decimal("10"), Decimal("1000"), Decimal("10500"), True,
        )
        self._fund = FundamentalSnapshot(
            "sh.600000", DAY, DAY, Decimal("8.5"), Decimal("0.9"), "fake"
        )
        self._div = DividendRecord("sh.600000", DAY, Decimal("0.3"), "fake")
        self._stock = StockIdentity("sh.600000", "浦发银行", "SSE", False, None, None)

    def fetch_stocks(self, as_of: date) -> list[StockIdentity]:
        self.calls.append(("stocks", as_of))
        return [self._stock]

    def fetch_daily_bars(
        self, codes: list[str], start: date, end: date, adjustment: AdjustmentMethod
    ) -> list[DailyBar]:
        self.calls.append(("daily_bars", (codes, start, end, adjustment)))
        return [self._bar]

    def fetch_fundamentals(self, codes: list[str], as_of: date) -> list[FundamentalSnapshot]:
        self.calls.append(("fundamentals", (codes, as_of)))
        return [self._fund]

    def fetch_dividends(self, codes: list[str], start: date, end: date) -> list[DividendRecord]:
        self.calls.append(("dividends", (codes, start, end)))
        return [self._div]


def _task(data_type: str = "daily_bars", **overrides: object) -> SyncTask:
    values: dict[str, object] = {
        "task_id": "t1",
        "plan_id": "plan-1",
        "sequence_no": 0,
        "data_type": data_type,
        "partition_key": DAY.isoformat(),
        "codes": ("sh.600000",),
        "range_start": DAY,
        "range_end": DAY,
        "dependencies": (),
        "status": SyncTaskStatus.PENDING,
        "attempt_count": 0,
        "not_before": None,
        "row_count": None,
        "error_code": None,
        "error_message": None,
        "started_at": None,
        "finished_at": None,
    }
    values.update(overrides)
    return SyncTask(**values)  # type: ignore[arg-type]


def _worker(provider: FakeProvider | None = None) -> tuple[SerialFetchWorker, FakeProvider]:
    p = provider or FakeProvider()
    return SerialFetchWorker(p, adjustment=AdjustmentMethod.QFQ, now=lambda: NOW), p


class TestSerialFetchWorker:
    def test_daily_bars_task(self) -> None:
        worker, provider = _worker()
        result = worker.execute(_task("daily_bars"))
        assert isinstance(result, TaskExecutionResult)
        assert result.row_count == 1
        assert result.finished_at == NOW
        assert provider.calls[0][0] == "daily_bars"
        _, (codes, start, end, adjustment) = provider.calls[0]
        assert codes == ("sh.600000",)
        assert adjustment is AdjustmentMethod.QFQ

    def test_fundamentals_task(self) -> None:
        worker, provider = _worker()
        result = worker.execute(_task("fundamentals"))
        assert result.row_count == 1
        assert provider.calls[0][0] == "fundamentals"

    def test_stocks_task(self) -> None:
        worker, provider = _worker()
        result = worker.execute(_task("stocks"))
        assert result.row_count == 1
        assert provider.calls[0][0] == "stocks"

    def test_dividends_task(self) -> None:
        worker, provider = _worker()
        result = worker.execute(_task("dividends"))
        assert result.row_count == 1
        assert provider.calls[0][0] == "dividends"

    def test_unsupported_data_type_raises(self) -> None:
        worker, _ = _worker()
        with pytest.raises(ProviderFetchError):
            worker.execute(_task("not_a_type"))

    def test_non_pending_task_rejected(self) -> None:
        worker, _ = _worker()
        with pytest.raises(ValueError):
            worker.execute(_task("daily_bars", status=SyncTaskStatus.RUNNING))

    def test_provider_error_propagates(self) -> None:
        class BrokenProvider(FakeProvider):
            def fetch_daily_bars(self, *args: object, **kwargs: object) -> list[DailyBar]:
                raise RuntimeError("boom")

        worker, _ = _worker(BrokenProvider())
        with pytest.raises(ProviderFetchError) as exc:
            worker.execute(_task("daily_bars"))
        assert "boom" in str(exc.value)

    def test_concurrent_access_blocked(self) -> None:
        worker, _ = _worker()
        assert worker._channel_lock.acquire(blocking=False)
        try:
            with pytest.raises(ConcurrentProviderAccessError):
                worker.execute(_task("daily_bars"))
        finally:
            worker._channel_lock.release()

    def test_sequential_calls_serialized(self) -> None:
        worker, provider = _worker()
        for i in range(3):
            worker.execute(_task("daily_bars", task_id=f"t{i}", sequence_no=i))
        assert len(provider.calls) == 3
        assert worker.call_log == (("daily_bars", 1), ("daily_bars", 1), ("daily_bars", 1))

    def test_adjustment_required(self) -> None:
        with pytest.raises(ValueError):
            SerialFetchWorker(FakeProvider(), adjustment="qfq", now=lambda: NOW)

    def test_call_log_requires_guard(self) -> None:
        worker, _ = _worker()
        worker.execute(_task("stocks"))
        assert worker.call_log == (("stocks", 1),)
