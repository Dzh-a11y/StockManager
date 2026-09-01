"""Regression: v2 targets must force-refresh the calendar when coverage is short."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_manager.domain import AdjustmentMethod, SyncStatus
from stock_manager.providers import FixtureProvider
from stock_manager.storage import SQLiteRepository
from stock_manager.sync import DataSyncService, SyncConfig, SyncHistoryConfig

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)
QFQ = AdjustmentMethod.QFQ


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def test_short_calendar_is_force_refreshed_for_v2_targets(tmp_path: Path) -> None:
    """本地日历只有近一年时,_resolve_targets_v2 必须重新拉取整段窗口日历。"""
    # 构造:fixture 提供 2018~2026 的交易日(跨八年),本地库先只存近一年日历
    all_days = tuple(
        d
        for d in (date(2018, 1, 2) + __import__("datetime").timedelta(days=i) for i in range(3200))
        if d.weekday() < 5
    )
    provider = FixtureProvider(
        trading_days=all_days,
        stocks=(),
        bars=(),
        fundamentals=(),
    )
    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    # 预置"旧一年日历"与对应 SUCCESS 记录(模拟 v1 时代缓存)
    old_meta = __import__("stock_manager.domain", fromlist=["DatasetMetadata"]).DatasetMetadata(
        "trading_calendar", date(2026, 8, 25), "fixture", NOW, AdjustmentMethod.UNADJUSTED
    )
    repo.save_trading_days(tuple(d for d in all_days if d >= date(2025, 9, 1)), old_meta)
    repo.save_sync_record(
        __import__("stock_manager.domain", fromlist=["SyncRecord"]).SyncRecord(
            "trading_calendar", date(2026, 8, 25), SyncStatus.SUCCESS, "fixture",
            AdjustmentMethod.UNADJUSTED, NOW, NOW, None,
        )
    )
    config = SyncConfig(
        time(17, 30), timedelta(minutes=5), 0, 45, 3, 360,
        SyncHistoryConfig(target_years=2),
    )
    service = DataSyncService(
        provider, repo, tmp_path / "locks", config, clock=MutableClock(NOW)
    )
    target_start, target_end = service._resolve_targets_v2()
    # 2 年目标 → 约 520 个交易日前的日期,必须远早于本地旧日历起点(2025-09-01)
    assert target_start < date(2025, 1, 1), f"target_start 未回溯:{target_start}"
    # provider 的日历被拉取过(强制刷新)
    assert provider.calls.get("fetch_trading_days", 0) >= 1
    # 本地日历已扩展到窗口起点
    days = repo.get_trading_days(target_start, target_end)
    assert days and days[0] <= target_start
