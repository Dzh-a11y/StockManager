"""P5A-8b strategy editor API tests: policy catalog, window years, custom policies."""

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
QFQ = AdjustmentMethod.QFQ
REPO = Path(__file__).parents[1]
STATIC_ROOT = REPO / "src" / "stock_manager" / "web" / "static"
SYSTEM_TEMPLATES = REPO / "config" / "rule_templates"
DAYS = tuple(date(2026, 8, day) for day in range(20, 26))


def _seed(database_path: Path) -> None:
    repo = SQLiteRepository(database_path)
    stocks = (
        StockIdentity("sh.600001", "Alpha", "SH", False, date(2000, 1, 1), None),
        StockIdentity("sz.000002", "Beta", "SZ", True, date(2001, 1, 1), None),
    )
    bars = []
    for stock in stocks:
        for day in DAYS:
            bars.append(
                DailyBar(stock.code, day, Decimal("100"), Decimal("101"), Decimal("99"),
                         Decimal("100"), Decimal("100"), Decimal("1000000"),
                         Decimal("100000000"), True)
            )
    metadata = DatasetMetadata("market", DAYS[-1], "fixture", NOW, QFQ)
    repo.save_trading_days(DAYS, metadata)
    repo.save_stocks(stocks, DatasetMetadata("market", date(2026, 8, 19), "fixture", NOW, QFQ))
    repo.save_stocks(stocks, metadata)
    repo.save_daily_bars(tuple(bars), metadata)
    repo.save_fundamentals(
        tuple(FundamentalSnapshot(s.code, date(2026, 8, 19), date(2026, 8, 19),
                                  Decimal("8"), Decimal("1"), "fixture") for s in stocks),
        metadata,
    )
    repo.save_sync_record(SyncRecord("market", DAYS[-1], SyncStatus.SUCCESS, "fixture", QFQ, NOW, NOW, None))


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


def _wait(app: WebApp, run_id: str) -> dict[str, object]:
    for _ in range(80):
        _s, _c, data = app.route("GET", f"/api/research/backtests/{run_id}", {}, None)
        payload = json.loads(data.decode("utf-8"))
        if payload.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return payload
        time.sleep(0.25)
    raise AssertionError("run did not finish")


def test_policy_catalog_returns_six_kinds(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route("GET", "/api/research/policies", {}, None)
    assert status == 200
    payload = json.loads(data.decode("utf-8"))
    kinds = set(payload["policies"])
    assert kinds == {"entry", "exit", "rebalance", "allocation", "ranking", "execution"}
    # 每个 kind 至少一个政策,且含参数 schema 字段
    for kind in kinds:
        assert payload["policies"][kind], f"empty {kind}"
        first = payload["policies"][kind][0]
        assert "policy_id" in first and "version" in first and "parameters" in first
    # 检查已知政策
    all_ids = [p["policy_id"] for items in payload["policies"].values() for p in items]
    assert "eligibility_enter_v1" in all_ids
    assert "ashare_execution_v1" in all_ids


def test_submit_with_window_years(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = {
        "template_id": "system-default",
        "template_revision": 1,
        "strategy_spec_id": "selection_rebalance_v1",
        "window_years": 1,
        "initial_cash": "1000000",
        "max_positions": 5,
    }
    status, _c, data = app.route("POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8"))
    assert status == 202
    run_id = json.loads(data.decode("utf-8"))["run_id"]
    payload = _wait(app, run_id)
    assert payload["status"] == "SUCCEEDED"


def test_submit_with_custom_policies(tmp_path: Path) -> None:
    app = _app(tmp_path)
    policies = {
        "entry": {"policy_id": "eligibility_enter_v1", "version": 1, "parameters": {}},
        "exit": {"policy_id": "fixed_holding_v1", "version": 1, "parameters": {"holding_trading_days": 3}},
        "rebalance": {"policy_id": "daily_v1", "version": 1, "parameters": {}},
        "allocation": {"policy_id": "equal_weight_v1", "version": 1, "parameters": {"max_positions": 5}},
        "ranking": {"policy_id": "turnover_20d_desc_v1", "version": 1, "parameters": {}},
        "execution": {"policy_id": "ashare_execution_v1", "version": 1, "parameters": {}},
    }
    body = {
        "template_id": "system-default",
        "template_revision": 1,
        "policies": policies,
        "window_years": 1,
        "initial_cash": "1000000",
    }
    status, _c, data = app.route("POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8"))
    assert status == 202
    run_id = json.loads(data.decode("utf-8"))["run_id"]
    payload = _wait(app, run_id)
    assert payload["status"] == "SUCCEEDED"


def test_submit_custom_policies_invalid_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    policies = {
        "entry": {"policy_id": "no_such_policy", "version": 1, "parameters": {}},
        "exit": {"policy_id": "eligibility_exit_v1", "version": 1, "parameters": {}},
        "rebalance": {"policy_id": "daily_v1", "version": 1, "parameters": {}},
        "allocation": {"policy_id": "equal_weight_v1", "version": 1, "parameters": {"max_positions": 5}},
        "ranking": {"policy_id": "turnover_20d_desc_v1", "version": 1, "parameters": {}},
        "execution": {"policy_id": "ashare_execution_v1", "version": 1, "parameters": {}},
    }
    body = {
        "template_id": "system-default",
        "template_revision": 1,
        "policies": policies,
        "window_years": 1,
        "initial_cash": "1000000",
    }
    status, _c, data = app.route("POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8"))
    assert status == 400


def test_window_years_out_of_range_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = {
        "template_id": "system-default",
        "template_revision": 1,
        "strategy_spec_id": "selection_rebalance_v1",
        "window_years": 9,
        "initial_cash": "1000000",
    }
    status, _c, data = app.route("POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8"))
    assert status == 400
