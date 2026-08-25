"""Offline HTTP contract tests for the StockManager P3 local Web layer."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    RuleResult,
    StockIdentity,
    SyncRecord,
    SyncStatus,
)
from stock_manager.rules.base import (
    ParameterDefinition,
    ParameterType,
    RuleContext,
    RuleDataRequirement,
    RuleDefinition,
)
from stock_manager.rules.builtin import build_default_registry
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.web.app import WebApp
from stock_manager.web.config import WebConfig


REPO = Path(__file__).parents[1]
TARGET_DAY = date(2026, 8, 25)
SHANGHAI = ZoneInfo("Asia/Shanghai")
STATIC_ROOT = REPO / "src" / "stock_manager" / "web" / "static"
SYSTEM_TEMPLATES = REPO / "config" / "rule_templates"


def _seed_repository(database_path: Path) -> SQLiteRepository:
    repository = SQLiteRepository(database_path)
    days = tuple(date(2026, 8, day) for day in range(20, 26))
    stocks = (
        StockIdentity("sh.600001", "Alpha", "SH", False, date(2000, 1, 1), None),
        StockIdentity("sz.000002", "Beta", "SZ", True, date(2001, 1, 1), None),
    )
    closes = (Decimal("100"),) * 5 + (Decimal("109"),)
    bars: list[DailyBar] = []
    for stock in stocks:
        for index, day in enumerate(days):
            close = closes[index]
            previous_close = Decimal("100") if index == 0 else closes[index - 1]
            bars.append(
                DailyBar(
                    stock.code,
                    day,
                    Decimal("100"),
                    close,
                    Decimal("99"),
                    close,
                    previous_close,
                    Decimal("400") if index == 5 else Decimal("100"),
                    Decimal("1000"),
                    True,
                )
            )
    fundamentals = tuple(
        FundamentalSnapshot(
            stock.code,
            date(2025, 12, 31),
            date(2026, 4, 1),
            Decimal("15"),
            Decimal("1"),
            "fixture",
        )
        for stock in stocks
    )
    dividends = tuple(
        DividendRecord(stock.code, date(2025, 6, 1), Decimal("0.1"), "fixture")
        for stock in stocks
    )
    now = datetime(2026, 8, 25, 18, 0, tzinfo=SHANGHAI)
    metadata = DatasetMetadata("market", TARGET_DAY, "fixture", now, AdjustmentMethod.QFQ)
    success = SyncRecord(
        "market",
        TARGET_DAY,
        SyncStatus.SUCCESS,
        "fixture",
        AdjustmentMethod.QFQ,
        now,
        now,
        None,
    )
    repository.save_market_snapshot(
        stocks, tuple(bars), fundamentals, dividends, days, metadata, success
    )
    return repository


def _app(tmp_path: Path) -> WebApp:
    database_path = tmp_path / "market.sqlite3"
    _seed_repository(database_path)
    config = WebConfig(
        database_path=database_path,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
    )
    return WebApp(config)


def _default_template() -> dict:
    root = SYSTEM_TEMPLATES / "system-default.json"
    return json.loads(root.read_text(encoding="utf-8"))


def _post(app: WebApp, path: str, body: object) -> tuple[int, object]:
    status, content_type, data = app.route(
        "POST", path, {}, json.dumps(body).encode("utf-8")
    )
    return status, json.loads(data.decode("utf-8")) if data else None


def _get(app: WebApp, path: str) -> tuple[int, object]:
    status, _content_type, data = app.route("GET", path, {}, None)
    return status, json.loads(data.decode("utf-8")) if data else None


def _delete(app: WebApp, path: str, body: object) -> tuple[int, object]:
    status, _content_type, data = app.route(
        "DELETE", path, {}, json.dumps(body).encode("utf-8")
    )
    return status, json.loads(data.decode("utf-8")) if data else None


def _screen_body(template: dict) -> dict:
    return {
        "template": template,
        "dataset_id": "market",
        "trading_day": TARGET_DAY.isoformat(),
        "adjustment": "qfq",
        "codes": ["sh.600001"],
    }


# ---------- P3-1: rule catalog ----------
def test_rules_catalog_is_metadata_driven_and_stable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get(app, "/api/rules")
    assert status == 200
    ids = [entry["rule_id"] for entry in payload["rules"]]
    assert ids == sorted(ids)
    assert "pe_positive" in ids and "annual_min_volume" in ids
    pe = next(entry for entry in payload["rules"] if entry["rule_id"] == "pe_positive")
    assert pe["name"] == "PE 下限"
    param = pe["parameters"][0]
    assert param["parameter_id"] == "minimum_exclusive"
    assert param["value_type"] == "decimal"
    assert param["default_value"] == "0"
    assert param["required"] is True
    assert set(param) == {
        "parameter_id",
        "value_type",
        "required",
        "default_value",
        "minimum",
        "maximum",
        "label",
        "description",
    }


# ---------- P3-2: app skeleton + static + health ----------
def test_health_and_static_whitelist(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get(app, "/health")
    assert status == 200
    assert payload == {"status": "ok"}

    for path in ("/", "/styles.css", "/app.js"):
        status, content_type, _ = app.route("GET", path, {}, None)
        assert status == 200
        assert content_type

    status, _ct, _body = app.route("GET", "/nope", {}, None)
    assert status == 404

    status, _ct, body = app.route("GET", "/favicon.ico", {}, None)
    assert status == 404
    assert b"NOT_FOUND" in body


def test_app_rejects_missing_database(tmp_path: Path) -> None:
    config = WebConfig(
        database_path=tmp_path / "missing.sqlite3",
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user",
        static_root=STATIC_ROOT,
    )
    with pytest.raises(ValueError, match="does not exist"):
        WebApp(config)


# ---------- P3-3: template APIs ----------
def test_template_list_and_full_load(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get(app, "/api/templates")
    assert status == 200
    assert payload["templates"][0]["template_id"] == "system-default"
    assert payload["templates"][0]["is_system"] is True

    status, full = _get(app, "/api/templates/system-default")
    assert status == 200
    assert full["is_system"] is True
    assert full["template"]["metadata"]["revision"] == 1
    assert full["template"]["composition"]["operator"] == "all"


def test_template_validate_ok_and_invalid(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _post(app, "/api/templates/validate", {"template": _default_template()})
    assert status == 200
    assert payload["valid"] is True
    assert payload["plan"]["template_id"] == "system-default"

    broken = _default_template()
    broken["composition"]["groups"] = []
    status, payload = _post(app, "/api/templates/validate", {"template": broken})
    assert status == 400
    assert payload["error"]["code"] == "BAD_REQUEST"


def test_user_template_crud_and_revision_conflict(tmp_path: Path) -> None:
    app = _app(tmp_path)
    template = _default_template()
    template["metadata"] = dict(template["metadata"], template_id="my-strategy", name="我的策略", revision=1)

    status, created = _post(app, "/api/templates", {"template": template})
    assert status == 201
    assert created == {"template_id": "my-strategy", "revision": 1}

    status, _ = _post(app, "/api/templates", {"template": template})
    assert status == 409

    updated = _default_template()
    updated["metadata"] = dict(updated["metadata"], template_id="my-strategy", name="我的策略", revision=1)
    status, _ct, saved = app.route(
        "PUT",
        "/api/templates/my-strategy",
        {},
        json.dumps({"template": updated, "expected_revision": 1}).encode("utf-8"),
    )
    assert status == 200
    assert saved is not None
    assert json.loads(saved.decode("utf-8")) == {"template_id": "my-strategy", "revision": 2}

    status, _ct, payload = app.route(
        "PUT",
        "/api/templates/my-strategy",
        {},
        json.dumps({"template": updated, "expected_revision": 1}).encode("utf-8"),
    )
    assert status == 409
    assert json.loads(payload.decode("utf-8"))["error"]["code"] == "CONFLICT"

    status, _ = _delete(app, "/api/templates/my-strategy", {"expected_revision": 2})
    assert status == 204
    status, _ = _get(app, "/api/templates/my-strategy")
    assert status == 404


def test_system_template_is_read_only(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _delete(app, "/api/templates/system-default", {"expected_revision": 1})
    assert status == 403
    assert payload["error"]["code"] == "FORBIDDEN"


# ---------- P3-4: screen API ----------
def test_screen_returns_tri_state_results(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _post(app, "/api/screen", _screen_body(_default_template()))
    assert status == 200
    assert payload["template_id"] == "system-default"
    assert payload["template_revision"] == 1
    assert payload["metadata"]["adjustment"] == "qfq"
    assert payload["summary"]["total"] == 1
    assert payload["summary"]["passed"] == 1
    result = payload["results"][0]
    assert result["code"] == "sh.600001"
    assert result["name"] == "Alpha"
    assert result["passed"] is True
    statuses = {item["rule_id"]: item["status"] for item in result["rule_executions"]}
    assert statuses["non_st"] in ("PASSED", "FAILED")
    assert all(item["status"] in ("PASSED", "FAILED", "SKIPPED") for item in result["rule_executions"])


def test_screen_reports_local_data_missing_as_404(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = _screen_body(_default_template())
    body["trading_day"] = "2099-01-01"
    status, payload = _post(app, "/api/screen", body)
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"


def test_screen_rejects_bad_adjustment(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = _screen_body(_default_template())
    body["adjustment"] = "hfq"
    status, payload = _post(app, "/api/screen", body)
    assert status == 400


def test_request_body_size_limit(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, _ct, data = app.route("POST", "/api/screen", {}, b"x" * 1_000_001)
    assert status == 400
    assert b"too large" in data


# ---------- P3-8: new rule end-to-end extensibility ----------
class AlwaysPassRule:
    definition = RuleDefinition(
        "always_pass",
        "始终通过",
        "测试用：始终通过并可携带整数参数",
        (
            ParameterDefinition(
                "factor",
                ParameterType.INTEGER,
                True,
                1,
                None,
                None,
                "倍数",
                "正整数参数",
            ),
        ),
    )

    def parse_parameters(self, raw):
        if not isinstance(raw, dict) or set(raw) != {"factor"}:
            raise ValueError("always_pass parameters must contain factor")
        value = raw["factor"]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError("factor must be a positive integer")
        return value

    def data_requirement(self, parameters):
        if not isinstance(parameters, int):
            raise TypeError("parameters must be int")
        return RuleDataRequirement()

    def evaluate(self, context, parameters):
        if not isinstance(parameters, int):
            raise TypeError("parameters must be int")
        del context
        return RuleResult("always_pass", True, parameters, parameters, "always passes")


def _extensible_app(tmp_path: Path) -> WebApp:
    database_path = tmp_path / "market.sqlite3"
    _seed_repository(database_path)
    registry = build_default_registry()
    registry.register(AlwaysPassRule())
    config = WebConfig(
        database_path=database_path,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
    )
    return WebApp(config, registry=registry)


def _always_pass_template() -> dict:
    return {
        "metadata": {
            "schema_version": 2,
            "template_id": "extension-test",
            "revision": 1,
            "name": "扩展测试",
            "description": "仅启用新注册的规则",
            "timezone": "Asia/Shanghai",
            "technical_adjustment": "qfq",
        },
        "rules": {"always_pass": {"enabled": True, "parameters": {"factor": 3}}},
        "composition": {
            "operator": "all",
            "groups": [{"group_id": "g1", "operator": "all", "rules": ["always_pass"]}],
        },
    }


def test_new_rule_auto_appears_in_catalog_and_renders_and_screens(tmp_path: Path) -> None:
    app = _extensible_app(tmp_path)

    status, catalog = _get(app, "/api/rules")
    assert status == 200
    entry = next(item for item in catalog["rules"] if item["rule_id"] == "always_pass")
    assert entry["name"] == "始终通过"
    assert entry["parameters"][0]["value_type"] == "integer"
    assert entry["parameters"][0]["default_value"] == 1

    status, payload = _post(
        app, "/api/templates/validate", {"template": _always_pass_template()}
    )
    assert status == 200
    assert payload["valid"] is True

    status, payload = _post(app, "/api/screen", _screen_body(_always_pass_template()))
    assert status == 200
    assert payload["summary"]["passed"] == 1
    execution = payload["results"][0]["rule_executions"][0]
    assert execution["rule_id"] == "always_pass"
    assert execution["status"] == "PASSED"
    assert execution["result"]["reason"] == "always passes"


# ---------- P3.x: sync endpoint (server-side DataSyncService trigger) ----------
class _FakeProvider:
    source_name = "fixture"

    def __init__(self, trading_day, stocks, bars, fundamentals, dividends):
        self._day = trading_day
        self._stocks = stocks
        self._bars = bars
        self._fundamentals = fundamentals
        self._dividends = dividends

    def fetch_trading_days(self, start, end):
        del start, end
        return [self._day]

    def fetch_stocks(self, as_of):
        del as_of
        return self._stocks

    def fetch_daily_bars(self, codes, start, end, adjustment):
        del start, end, adjustment
        return [bar for bar in self._bars if bar.code in codes]

    def fetch_fundamentals(self, codes, as_of):
        del as_of
        return [item for item in self._fundamentals if item.code in codes]

    def fetch_dividends(self, codes, start, end):
        del start, end
        return [item for item in self._dividends if item.code in codes]


def _fake_provider() -> _FakeProvider:
    stocks = (
        StockIdentity("sh.600001", "Alpha", "SH", False, date(2000, 1, 1), None),
        StockIdentity("sz.000002", "Beta", "SZ", True, date(2001, 1, 1), None),
    )
    bars = tuple(
        DailyBar(
            stock.code, TARGET_DAY, Decimal("10"), Decimal("10"), Decimal("10"),
            Decimal("10"), Decimal("9"), Decimal("100"), Decimal("1000"), True,
        )
        for stock in stocks
    )
    fundamentals = tuple(
        FundamentalSnapshot(
            stock.code, date(2025, 12, 31), date(2026, 4, 1), Decimal("15"), Decimal("1"), "fixture"
        )
        for stock in stocks
    )
    return _FakeProvider(TARGET_DAY, stocks, tuple(bars), fundamentals, ())


def _non_st_template() -> dict:
    return {
        "metadata": {
            "schema_version": 2,
            "template_id": "simple",
            "revision": 1,
            "name": "简单策略",
            "description": "仅排除 ST",
            "timezone": "Asia/Shanghai",
            "technical_adjustment": "qfq",
        },
        "rules": {"non_st": {"enabled": True, "parameters": {}}},
        "composition": {
            "operator": "all",
            "groups": [{"group_id": "g1", "operator": "all", "rules": ["non_st"]}],
        },
    }


def test_sync_endpoint_requires_configuration(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _post(
        app,
        "/api/sync",
        {"dataset_id": "market", "trading_day": TARGET_DAY.isoformat(), "adjustment": "qfq"},
    )
    assert status == 400
    assert payload["error"]["code"] == "BAD_REQUEST"


def test_sync_populates_local_data_then_screen_reads_it(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite3"
    # Create the (empty) local schema so startup validation passes.
    SQLiteRepository(db)
    locks = tmp_path / "locks"
    locks.mkdir()
    config = WebConfig(
        database_path=db,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
        sync_config_path=REPO / "config" / "sync.json",
        lock_directory=locks,
    )
    app = WebApp(config, provider_factory=_fake_provider)

    status, payload = _post(
        app,
        "/api/sync",
        {"dataset_id": "market", "trading_day": TARGET_DAY.isoformat(), "adjustment": "qfq"},
    )
    assert status == 200
    assert payload["status"] == "SUCCESS"
    assert payload["skipped"] is False

    status, screen = _post(
        app,
        "/api/screen",
        {
            "template": _non_st_template(),
            "dataset_id": "market",
            "trading_day": TARGET_DAY.isoformat(),
            "adjustment": "qfq",
        },
    )
    assert status == 200
    assert screen["summary"]["total"] == 2
    passed = {item["code"] for item in screen["results"] if item["passed"]}
    assert passed == {"sh.600001"}



def test_sync_progress_endpoint_tracks_state(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite3"
    SQLiteRepository(db)
    locks = tmp_path / "locks"
    locks.mkdir()
    config = WebConfig(
        database_path=db,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
        sync_config_path=REPO / "config" / "sync.json",
        lock_directory=locks,
    )
    app = WebApp(config, provider_factory=_fake_provider)

    status, progress = _get(app, "/api/sync/progress")
    assert status == 200
    assert progress["status"] == "idle"

    status, _ = _post(
        app,
        "/api/sync",
        {"dataset_id": "market", "trading_day": TARGET_DAY.isoformat(), "adjustment": "qfq", "retry": True},
    )
    assert status == 200

    status, progress = _get(app, "/api/sync/progress")
    assert status == 200
    assert progress["status"] == "done"
    assert progress["dataset_id"] == "market"
    assert progress["trading_day"] == TARGET_DAY.isoformat()


def test_sync_endpoint_rejects_while_backfill_running(tmp_path: Path) -> None:
    db = tmp_path / "market.sqlite3"
    SQLiteRepository(db)
    locks = tmp_path / "locks"
    locks.mkdir()
    config = WebConfig(
        database_path=db,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
        sync_config_path=REPO / "config" / "sync.json",
        lock_directory=locks,
    )
    app = WebApp(config, provider_factory=_fake_provider)
    app._sync_progress = {
        "status": "running",
        "phase": "backfill",
        "dataset_id": "market",
        "adjustment": "qfq",
        "message": "启动回补历史数据",
    }

    status, payload = _post(
        app,
        "/api/sync",
        {"dataset_id": "market", "trading_day": TARGET_DAY.isoformat(), "adjustment": "qfq"},
    )
    assert status == 409
    assert payload["error"]["code"] == "CONFLICT"


def test_backfill_batch_progress_callback_updates_state(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app._sync_progress = {"status": "running", "phase": "backfill"}
    app._on_backfill_batch_progress(
        {
            "phase": "daily_bars",
            "index": 42,
            "total": 100,
            "current_code": "sh.600000",
        }
    )
    assert app._sync_progress["batch_phase"] == "daily_bars"
    assert app._sync_progress["batch_completed"] == 42
    assert app._sync_progress["batch_total"] == 100
    assert app._sync_progress["current_code"] == "sh.600000"
    assert app._sync_progress["status"] == "running"
