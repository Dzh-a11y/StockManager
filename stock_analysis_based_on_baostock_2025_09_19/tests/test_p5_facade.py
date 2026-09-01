"""Offline tests for the DataSyncService -> SyncPipeline facade integration."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    FundamentalSnapshot,
    ReadinessStatus,
    StockIdentity,
    SyncPlanMode,
    SyncPlanStatus,
)
from stock_manager.storage import SQLiteRepository
from stock_manager.sync.data_sync_service import (
    DataSyncService,
    SyncConfig,
    SyncHistoryConfig,
)
from stock_manager.sync.pipeline import PipelineRun

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAYS = (date(2026, 9, 1), date(2026, 9, 2))
CODES = ("sh.600000", "sz.000001", "sh.600519")


class FacadeProvider:
    """Minimal provider the facade pipeline can drive offline."""

    source_name = "facade"

    def fetch_trading_days(self, start: date, end: date) -> list[date]:
        return [d for d in DAYS if start <= d <= end]

    def fetch_stocks(self, as_of: date) -> list[StockIdentity]:
        return [StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES]

    def fetch_daily_bars(
        self, codes: list[str], start: date, end: date, adjustment: AdjustmentMethod
    ) -> list[DailyBar]:
        bars: list[DailyBar] = []
        from datetime import timedelta

        day = start
        while day <= end:
            for code in codes:
                bars.append(
                    DailyBar(
                        code, day, Decimal("10"), Decimal("11"), Decimal("9"),
                        Decimal("10.5"), Decimal("10"), Decimal("1000"),
                        Decimal("10500"), True,
                    )
                )
            day = day + timedelta(days=1)
        return bars

    def fetch_fundamentals(
        self, codes: list[str], as_of: date
    ) -> list[FundamentalSnapshot]:
        return [
            FundamentalSnapshot(c, as_of, as_of, Decimal("8"), Decimal("1"), "facade")
            for c in codes
        ]


@pytest.fixture()
def service(tmp_path: Path) -> DataSyncService:
    from stock_manager.domain import DatasetMetadata, StockIdentity

    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    # 离线日历 + 股票池:让 planner 的 calendar/universe 有数据。
    calendar_metadata = DatasetMetadata(
        "trading_calendar", DAYS[0], "fixture", NOW, AdjustmentMethod.UNADJUSTED
    )
    repo.save_trading_days(DAYS, calendar_metadata)
    pool_metadata = DatasetMetadata(
        "market", DAYS[0], "fixture", NOW, AdjustmentMethod.QFQ
    )
    repo.save_stocks(
        [StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES],
        pool_metadata,
    )
    config = SyncConfig(
        cutoff_time=__import__("datetime").time(15, 0),
        retry_cooldown=timedelta(seconds=5),
        minimum_request_interval_seconds=0.0,
        calendar_horizon_days=5,
        dividend_lookback_years=1,
        retention_days=360,
        history=SyncHistoryConfig(target_years=8),
    )
    return DataSyncService(
        FacadeProvider(),
        repo,
        tmp_path / "locks",
        config,
        clock=lambda: NOW,
    )


class TestFacadeIntegration:
    def test_plan_and_execute_via_service(self, service: DataSyncService) -> None:
        output = service.run_pipeline_plan(
            mode="BOOTSTRAP",
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            data_types=("daily_bars",),
        )
        assert output.plan.mode is SyncPlanMode.BOOTSTRAP
        run = service.run_pipeline_execute(output.plan.plan_id)
        assert isinstance(run, PipelineRun)
        assert run.published is True
        assert run.plan_status is SyncPlanStatus.SUCCEEDED

    def test_facade_publishes_readable_generation(
        self, service: DataSyncService
    ) -> None:
        output = service.run_pipeline_plan(
            mode="BOOTSTRAP",
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=DAYS[0],
            target_end=DAYS[-1],
            data_types=("daily_bars",),
        )
        service.run_pipeline_execute(output.plan.plan_id)
        # 通过 pipeline 的 gate 检查 READY
        pipeline = service.build_pipeline()
        from stock_manager.sync.committer import ReadinessGate

        repo = service._repository
        import sqlite3

        def factory() -> sqlite3.Connection:
            connection = sqlite3.connect(repo.database_path, timeout=30.0)
            connection.row_factory = sqlite3.Row
            return connection

        gate = ReadinessGate(factory)
        result = gate.evaluate(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            required_data_types=("daily_bars",),
            requested_start=DAYS[0],
            requested_end=DAYS[-1],
        )
        assert result.status is ReadinessStatus.READY

    def test_unknown_mode_rejected(self, service: DataSyncService) -> None:
        with pytest.raises(ValueError):
            service.run_pipeline_plan(
                mode="BOGUS",
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                target_start=DAYS[0],
                target_end=DAYS[-1],
            )


class TestPipelineDefaultSwitch:
    def test_config_parses_pipeline_default(self, tmp_path: Path) -> None:
        from stock_manager.sync.config import load_sync_config

        path = tmp_path / "sync.json"
        path.write_text(
            (
                '{"metadata":{"version":2,"timezone":"Asia/Shanghai"},'
                '"policy":{"cutoff_time":"15:00:00","retry_cooldown_seconds":5,'
                '"minimum_request_interval_seconds":0.2,"calendar_horizon_days":5,'
                '"dividend_lookback_years":1,"pipeline_default":true},'
                '"history":{"target_years":8}}'
            ),
            encoding="utf-8",
        )
        config = load_sync_config(path)
        assert config.pipeline_default is True

    def test_config_pipeline_default_absent_is_false(self, tmp_path: Path) -> None:
        from stock_manager.sync.config import load_sync_config

        path = tmp_path / "sync.json"
        path.write_text(
            (
                '{"metadata":{"version":2,"timezone":"Asia/Shanghai"},'
                '"policy":{"cutoff_time":"15:00:00","retry_cooldown_seconds":5,'
                '"minimum_request_interval_seconds":0.2,"calendar_horizon_days":5,'
                '"dividend_lookback_years":1},'
                '"history":{"target_years":8}}'
            ),
            encoding="utf-8",
        )
        config = load_sync_config(path)
        assert config.pipeline_default is False

    def test_startup_sync_routes_to_pipeline_when_enabled(
        self, tmp_path: Path
    ) -> None:
        from datetime import time as wall_time

        repo = SQLiteRepository(tmp_path / "market.sqlite3")
        from stock_manager.domain import DatasetMetadata, StockIdentity

        repo.save_trading_days(
            DAYS,
            DatasetMetadata("trading_calendar", DAYS[0], "fixture", NOW, AdjustmentMethod.UNADJUSTED),
        )
        repo.save_stocks(
            [StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES],
            DatasetMetadata("market", DAYS[0], "fixture", NOW, AdjustmentMethod.QFQ),
        )
        config = SyncConfig(
            cutoff_time=wall_time(9, 0),
            retry_cooldown=timedelta(seconds=5),
            minimum_request_interval_seconds=0.0,
            calendar_horizon_days=5,
            dividend_lookback_years=1,
            retention_days=360,
            history=SyncHistoryConfig(target_years=8),
            pipeline_default=True,
        )
        svc = DataSyncService(
            FacadeProvider(), repo, tmp_path / "locks", config, clock=lambda: NOW
        )
        run = svc.startup_sync("market", AdjustmentMethod.QFQ)
        from stock_manager.sync.pipeline import PipelineRun

        assert isinstance(run, PipelineRun)
        assert run.published is True
        assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is not None

    def test_startup_sync_falls_back_when_disabled(
        self, tmp_path: Path
    ) -> None:
        from datetime import time as wall_time

        repo = SQLiteRepository(tmp_path / "market.sqlite3")
        config = SyncConfig(
            cutoff_time=wall_time(9, 0),
            retry_cooldown=timedelta(seconds=5),
            minimum_request_interval_seconds=0.0,
            calendar_horizon_days=5,
            dividend_lookback_years=1,
            retention_days=360,
            history=SyncHistoryConfig(target_years=8),
            pipeline_default=False,
        )
        svc = DataSyncService(
            FacadeProvider(), repo, tmp_path / "locks", config, clock=lambda: NOW
        )
        # 未启用时走旧 v2 路径:返回 SyncOutcome 而非 PipelineRun。
        from stock_manager.domain import SyncOutcome
        from stock_manager.sync.pipeline import PipelineRun

        outcome = svc.startup_sync("market", AdjustmentMethod.QFQ)
        assert not isinstance(outcome, PipelineRun)
        assert outcome is None or isinstance(outcome, SyncOutcome)
