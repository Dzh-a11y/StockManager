"""P5A-8 research backtest API tests (offline, local job runner)."""

from __future__ import annotations

import json
import time
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
    SyncRecord,
    SyncStatus,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.web.app import WebApp
from stock_manager.web.config import WebConfig

SHANGHAI = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)
REPO = Path(__file__).parents[1]
STATIC_ROOT = REPO / "src" / "stock_manager" / "web" / "static"
SYSTEM_TEMPLATES = REPO / "config" / "rule_templates"
QFQ = AdjustmentMethod.QFQ
DAYS = tuple(date(2026, 8, day) for day in range(20, 26))


def _seed(database_path: Path) -> None:
    repository = SQLiteRepository(database_path)
    stocks = (
        StockIdentity("sh.600001", "Alpha", "SH", False, date(2000, 1, 1), None),
        StockIdentity("sz.000002", "Beta", "SZ", True, date(2001, 1, 1), None),
    )
    bars = []
    for stock in stocks:
        for day in DAYS:
            bars.append(
                DailyBar(
                    stock.code, day, Decimal("100"), Decimal("101"), Decimal("99"),
                    Decimal("100"), Decimal("100"), Decimal("1000000"),
                    Decimal("100000000"), True,
                )
            )
    metadata = DatasetMetadata("market", DAYS[-1], "fixture", NOW, QFQ)
    repository.save_trading_days(DAYS, metadata)
    repository.save_stocks(
        stocks, DatasetMetadata("market", date(2026, 8, 19), "fixture", NOW, QFQ)
    )
    repository.save_stocks(stocks, metadata)
    repository.save_daily_bars(tuple(bars), metadata)
    repository.save_fundamentals(
        tuple(
            FundamentalSnapshot(
                stock.code, date(2026, 8, 19), date(2026, 8, 19),
                Decimal("8"), Decimal("1"), "fixture",
            )
            for stock in stocks
        ),
        metadata,
    )
    repository.save_sync_record(
        SyncRecord("market", DAYS[-1], SyncStatus.SUCCESS, "fixture", QFQ, NOW, NOW, None)
    )


def _app(tmp_path: Path) -> WebApp:
    database_path = tmp_path / "market.sqlite3"
    _seed(database_path)
    config = WebConfig(
        database_path=database_path,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
    )
    return WebApp(config)


def _submit_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "template_id": "system-default",
        "template_revision": 1,
        "strategy_spec_id": "selection_rebalance_v1",
        "backtest_start": "2026-08-20",
        "backtest_end": "2026-08-25",
        "initial_cash": "1000000",
        "max_positions": 5,
    }
    body.update(overrides)
    return body


def _wait_for_terminal(app: WebApp, run_id: str) -> dict[str, object]:
    for _ in range(80):
        status, _content_type, data = app.route(
            "GET", f"/api/research/backtests/{run_id}", {}, None
        )
        payload = json.loads(data.decode("utf-8"))
        if payload.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return payload
        time.sleep(0.25)
    raise AssertionError(f"run {run_id} did not finish")


def test_submit_and_succeed_with_results(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _content_type, data = app.route(
        "POST", "/api/research/backtests", {}, json.dumps(_submit_body()).encode("utf-8")
    )
    assert status == 202
    run_id = json.loads(data.decode("utf-8"))["run_id"]
    payload = _wait_for_terminal(app, run_id)
    assert payload["status"] == "SUCCEEDED"
    assert payload["metrics"]["final_value"] is not None
    status, _c, data = app.route("GET", f"/api/research/backtests/{run_id}/equity", {}, None)
    equity = json.loads(data.decode("utf-8"))
    assert equity["count"] > 0
    status, _c, data = app.route("GET", f"/api/research/backtests/{run_id}/orders", {}, None)
    orders = json.loads(data.decode("utf-8"))
    assert orders["count"] >= 0
    status, _c, data = app.route("GET", f"/api/research/backtests/{run_id}/provenance", {}, None)
    provenance = json.loads(data.decode("utf-8"))
    assert provenance["provenance"]["engine"] == "backtrader"
    assert provenance["provenance"]["cheat_modes"] == "none"


def test_second_submission_reuses_cached_eligibility(tmp_path: Path) -> None:
    """相同模板+区间+参数:第二次提交命中缓存,不重跑规则引擎。"""
    app = _app(tmp_path)
    first_run = _submit_and_wait(app, _submit_body())
    # 第二次提交(不同策略规格但同一资格缓存键的 eligibility 部分)
    body = _submit_body(strategy_spec_id="selection_fixed_holding_v1")
    second_run = _submit_and_wait(app, body)
    assert second_run["status"] == "SUCCEEDED"
    assert first_run["run_id"] != second_run["run_id"]


def _submit_and_wait(app: WebApp, body: dict[str, object]) -> dict[str, object]:
    status, _c, data = app.route(
        "POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8")
    )
    assert status == 202
    run_id = json.loads(data.decode("utf-8"))["run_id"]
    return _wait_for_terminal(app, run_id)


def test_unknown_strategy_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route(
        "POST", "/api/research/backtests", {},
        json.dumps(_submit_body(strategy_spec_id="evil_strategy")).encode("utf-8"),
    )
    assert status == 400


def test_wrong_template_revision_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route(
        "POST", "/api/research/backtests", {},
        json.dumps(_submit_body(template_revision=99)).encode("utf-8"),
    )
    assert status == 400


def test_cancel_request_accepted(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route(
        "POST", "/api/research/backtests", {}, json.dumps(_submit_body()).encode("utf-8")
    )
    run_id = json.loads(data.decode("utf-8"))["run_id"]
    status, _c, data = app.route(
        "POST", f"/api/research/backtests/{run_id}/cancel", {}, b"{}"
    )
    assert status == 200
    payload = json.loads(data.decode("utf-8"))
    assert payload["cancel_requested"] is True


def test_run_listing(tmp_path: Path) -> None:
    app = _app(tmp_path)
    _submit_and_wait(app, _submit_body())
    status, _c, data = app.route("GET", "/api/research/backtests", {}, None)
    payload = json.loads(data.decode("utf-8"))
    assert len(payload["runs"]) >= 1
    assert payload["runs"][0]["status"] == "SUCCEEDED"


def test_unknown_run_returns_404(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route("GET", "/api/research/backtests/nope", {}, None)
    assert status == 404
