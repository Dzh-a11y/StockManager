"""P5C web API tests: strategy template CRUD, group submission, single-code /
ignore-eligibility mode, run-level fees and bars range query (offline)."""

from __future__ import annotations

import json
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

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
    stock = StockIdentity("sh.600001", "Alpha", "SH", False, date(2000, 1, 1), None)
    bars = [
        DailyBar(
            "sh.600001", day, Decimal("100"), Decimal("101"), Decimal("99"),
            Decimal("100"), Decimal("100"), Decimal("1000000"),
            Decimal("100000000"), True,
        )
        for day in DAYS
    ]
    metadata = DatasetMetadata("market", DAYS[-1], "fixture", NOW, QFQ)
    repository.save_trading_days(DAYS, metadata)
    repository.save_stocks(
        (stock,), DatasetMetadata("market", date(2026, 8, 19), "fixture", NOW, QFQ)
    )
    repository.save_stocks((stock,), metadata)
    repository.save_daily_bars(tuple(bars), metadata)
    repository.save_fundamentals(
        (
            FundamentalSnapshot(
                "sh.600001", date(2026, 8, 19), date(2026, 8, 19),
                Decimal("8"), Decimal("1"), "fixture",
            ),
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


def _wait(app: WebApp, run_id: str) -> dict[str, object]:
    for _ in range(80):
        _s, _c, data = app.route("GET", f"/api/research/backtests/{run_id}", {}, None)
        payload = json.loads(data.decode("utf-8"))
        if payload.get("status") in ("SUCCEEDED", "FAILED", "CANCELLED"):
            return payload
        time.sleep(0.25)
    raise AssertionError("run did not finish")


def _group_policies() -> dict[str, object]:
    return {
        "entry": {
            "operator": "any",
            "items": [
                {"policy_id": "eligibility_enter_v1", "version": 1, "parameters": {}}
            ],
        },
        "exit": {
            "operator": "any",
            "items": [
                {"policy_id": "eligibility_exit_v1", "version": 1, "parameters": {}}
            ],
        },
        "rebalance": {"policy_id": "daily_v1", "version": 1, "parameters": {}},
        "allocation": {
            "policy_id": "equal_weight_v1",
            "version": 1,
            "parameters": {"max_positions": 20, "cash_reserve_ratio": "0"},
        },
        "ranking": {"policy_id": "turnover_20d_desc_v1", "version": 1, "parameters": {}},
        "execution": {"policy_id": "ashare_execution_v1", "version": 1, "parameters": {}},
        "take_profit_tiers": [],
    }


def test_strategy_template_crud(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route("GET", "/api/research/strategies", {}, None)
    assert status == 200
    listing = json.loads(data.decode("utf-8"))["strategies"]
    assert any(item["strategy_template_id"] == "default-backtest-v1" for item in listing)

    body = {
        "strategy_template_id": "trend-follow-20",
        "name": "20日均线趋势",
        "description": "测试策略模板",
        "policies": _group_policies(),
    }
    status, _c, data = app.route(
        "POST", "/api/research/strategies", {}, json.dumps(body).encode("utf-8")
    )
    assert status == 201
    status, _c, data = app.route(
        "GET", "/api/research/strategies/trend-follow-20", {}, None
    )
    assert status == 200
    full = json.loads(data.decode("utf-8"))
    assert full["revision"] == 1
    assert full["policies"]["entry"]["operator"] == "any"
    assert full["policies"]["take_profit_tiers"] == []

    status, _c, data = app.route(
        "PUT", "/api/research/strategies/trend-follow-20", {},
        json.dumps({"expected_revision": 1, **body}).encode("utf-8"),
    )
    assert status == 200
    assert json.loads(data.decode("utf-8"))["revision"] == 2
    status, _c, data = app.route(
        "PUT", "/api/research/strategies/trend-follow-20", {},
        json.dumps({"expected_revision": 1, **body}).encode("utf-8"),
    )
    assert status == 409
    status, _c, data = app.route(
        "DELETE", "/api/research/strategies/trend-follow-20", {},
        json.dumps({"expected_revision": 2}).encode("utf-8"),
    )
    assert status == 204


def test_strategy_default_read_only(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _c, data = app.route(
        "PUT", "/api/research/strategies/default-backtest-v1", {},
        json.dumps({"expected_revision": 1, "name": "x", "description": "y",
                    "policies": _group_policies()}).encode("utf-8"),
    )
    assert status == 400


def test_ignore_eligibility_single_code_run(tmp_path: Path) -> None:
    """忽略资格 + 单代码 + run 级费用:提交成功且回看带 settings 快照。"""
    app = _app(tmp_path)
    body = {
        "template_id": "system-default",
        "template_revision": 1,
        "policies": _group_policies(),
        "backtest_start": "2026-08-20",
        "backtest_end": "2026-08-25",
        "initial_cash": "1000000",
        "max_positions": 5,
        "codes": "sh.600001",
        "ignore_eligibility": True,
        "commission_rate": "0.0002",
        "stamp_duty_rate": "0.0005",
        "transfer_fee_rate": "0.00001",
        "min_commission": "1",
        "lot_size": 100,
        "strategy_template_id": "default-backtest-v1",
        "strategy_template_revision": 1,
    }
    status, _c, data = app.route(
        "POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8")
    )
    assert status == 202
    run_id = json.loads(data.decode("utf-8"))["run_id"]
    payload = _wait(app, run_id)
    assert payload["status"] == "SUCCEEDED"
    settings = payload.get("settings") or {}
    assert settings["mode"] == "ignore_eligibility"
    assert settings["codes"] == ["sh.600001"]
    assert settings["fees"]["commission_rate"] == "0.0002"
    assert settings["strategy_template_id"] == "default-backtest-v1"
    assert settings["fees"]["lot_size"] == 100
    status, _c, data = app.route("GET", "/api/research/backtests", {}, None)
    runs = json.loads(data.decode("utf-8"))["runs"]
    assert runs and runs[0]["result"] is not None


def test_ignore_eligibility_without_codes_rejected(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = {
        "template_id": "system-default",
        "template_revision": 1,
        "policies": _group_policies(),
        "window_years": 1,
        "initial_cash": "1000000",
        "ignore_eligibility": True,
    }
    status, _c, data = app.route(
        "POST", "/api/research/backtests", {}, json.dumps(body).encode("utf-8")
    )
    assert status == 400


def test_bars_range_query(tmp_path: Path) -> None:
    app = _app(tmp_path)
    query = {
        "code": ["sh.600001"],
        "adjustment": ["qfq"],
        "start": ["2026-08-20"],
        "end": ["2026-08-25"],
    }
    status, _c, data = app.route("GET", "/api/bars", query, None)
    assert status == 200
    payload = json.loads(data.decode("utf-8"))
    assert payload["start"] == "2026-08-20"
    assert len(payload["bars"]) == len(DAYS)
    assert payload["bars"][0]["trading_day"] == "2026-08-20"
