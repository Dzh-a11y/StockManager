"""P5A-1 v2 backfill progress reporting tests."""

from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    BackfillRunStatus,
    BackfillRunV2,
    BackfillChunkV2,
    DailyBar,
    DatasetMetadata,
    HistoricalRunStatus,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.web.app import WebApp
from stock_manager.web.config import WebConfig

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=SHANGHAI)
QFQ = AdjustmentMethod.QFQ
REPO = Path(__file__).parents[1]


def _app(tmp_path: Path) -> WebApp:
    db = tmp_path / "market.sqlite3"
    repo = SQLiteRepository(db)
    from stock_manager.domain import StockIdentity

    stocks = tuple(
        StockIdentity(f"{i:06d}.SZ", f"股票{i}", "SZSE", False, date(2018, 7, 12), None)
        for i in range(1, 101)
    )
    from stock_manager.domain import DatasetMetadata

    repo.save_stocks(stocks, DatasetMetadata("market", date(2026, 8, 31), "fixture", NOW, QFQ))
    config = WebConfig(
        database_path=db,
        system_template_root=REPO / "config" / "rule_templates",
        user_template_root=tmp_path / "user-templates",
        static_root=REPO / "src" / "stock_manager" / "web" / "static",
        sync_config_path=None,
        lock_directory=None,
    )
    return WebApp(config)


def test_progress_none_when_no_run(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route("GET", "/api/sync/backfill/progress", {}, None)
    assert status == 200
    import json

    payload = json.loads(data.decode("utf-8"))
    assert payload["status"] == "none"


def _save_window(repo: SQLiteRepository) -> None:
    """Save three trading days and bars for all stocks on the first day only."""
    from decimal import Decimal

    from stock_manager.domain import DailyBar

    meta = DatasetMetadata("market", date(2026, 8, 31), "fixture", NOW, QFQ)
    days = (date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27))
    repo.save_trading_days(days, meta)
    bars = tuple(
        DailyBar(
            f"{i:06d}.SZ", days[0], Decimal("10"), Decimal("11"), Decimal("9"),
            Decimal("10.5"), Decimal("10"), Decimal("1000"), Decimal("10500"), True,
        )
        for i in range(1, 101)
    )
    repo.save_daily_bars(bars, meta)


def test_progress_reports_running_run(tmp_path: Path) -> None:
    app = _app(tmp_path)
    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    repo.save_backfill_run_v2(
        BackfillRunV2(
            "run-1",
            "market",
            QFQ,
            date(2018, 7, 12),
            date(2026, 8, 31),
            BackfillRunStatus.RUNNING,
            NOW,
            None,
            None,
        )
    )
    _save_window(repo)
    status, _c, data = app.route("GET", "/api/sync/backfill/progress", {}, None)
    assert status == 200
    import json

    payload = json.loads(data.decode("utf-8"))
    assert payload["status"] == "RUNNING"
    assert payload["target_start"] == "2018-07-12"
    assert payload["target_end"] == "2026-08-31"
    # 进度按完整入库股票数:窗口 3 个交易日,池 100 只;
    # 全部股票只有 1 天 bar(1/3 < 95%)→ 完整入库 0 只,进度 0。
    assert payload["covered_days"] == 0
    assert payload["total_days"] == 100
    assert payload["progress"] == 0


def test_progress_complete_when_success_and_done(tmp_path: Path) -> None:
    app = _app(tmp_path)
    repo = SQLiteRepository(tmp_path / "market.sqlite3")
    repo.save_backfill_run_v2(
        BackfillRunV2(
            "run-1",
            "market",
            QFQ,
            date(2018, 7, 12),
            date(2026, 8, 31),
            BackfillRunStatus.SUCCESS,
            NOW,
            NOW,
            None,
        )
    )
    # 完整覆盖:三个交易日全部补齐 -> 判定 complete,progress=1.0。
    _save_window(repo)
    from decimal import Decimal

    from stock_manager.domain import DailyBar

    meta = DatasetMetadata("market", date(2026, 8, 31), "fixture", NOW, QFQ)
    days = (date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27))
    bars = tuple(
        DailyBar(
            f"{i:06d}.SZ", d, Decimal("10"), Decimal("11"), Decimal("9"),
            Decimal("10.5"), Decimal("10"), Decimal("1000"), Decimal("10500"), True,
        )
        for i in range(1, 101)
        for d in days
    )
    repo.save_daily_bars(bars, meta)
    status, _c, data = app.route("GET", "/api/sync/backfill/progress", {}, None)
    import json

    payload = json.loads(data.decode("utf-8"))
    assert payload["status"] == "complete"
    assert payload["progress"] == 1.0
