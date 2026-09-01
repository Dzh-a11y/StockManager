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
    repo.save_backfill_chunk_v2(
        BackfillChunkV2("run-1", 0, ("000001.SZ",), date(2018, 7, 12), date(2025, 8, 31), 1900, BackfillRunStatus.SUCCESS)
    )
    status, _c, data = app.route("GET", "/api/sync/backfill/progress", {}, None)
    assert status == 200
    import json

    payload = json.loads(data.decode("utf-8"))
    assert payload["status"] == "RUNNING"
    assert payload["target_start"] == "2018-07-12"
    assert payload["target_end"] == "2026-08-31"
    assert payload["done_chunks"] == 1
    assert payload["total_chunks"] >= 1
    assert 0 <= payload["progress"] <= 1


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
    # 100 只股票 / 100 批 = 1 个 chunk 组
    repo.save_backfill_chunk_v2(
        BackfillChunkV2("run-1", 0, ("000001.SZ",), date(2018, 7, 12), date(2025, 8, 31), 1900, BackfillRunStatus.SUCCESS)
    )
    status, _c, data = app.route("GET", "/api/sync/backfill/progress", {}, None)
    import json

    payload = json.loads(data.decode("utf-8"))
    assert payload["status"] == "complete"
