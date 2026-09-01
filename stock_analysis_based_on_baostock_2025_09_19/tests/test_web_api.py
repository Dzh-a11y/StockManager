"""Offline HTTP contract tests for the StockManager P3 local Web layer."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
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
            TARGET_DAY,
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
    _publish_legacy_generation(repository)
    return repository


def _publish_legacy_generation(repository: SQLiteRepository) -> None:
    """P5 §11.1:把 seed 的 legacy 共享表数据导入为 generation 并发布。

    让「legacy 数据只有经过 LEGACY_IMPORT 与新 verifier 才能被读取」的
    红线在测试种子中同样成立;发布后 ReadinessGate 才返回 READY。
    """
    import sqlite3 as _sqlite3
    from datetime import datetime as _datetime

    from stock_manager.domain import CandidateGenerationStatus
    from stock_manager.sync.committer import GenerationCommitter
    from stock_manager.sync.legacy import LegacyImporter
    from stock_manager.sync.verifier import CoverageVerifier

    def factory() -> _sqlite3.Connection:
        connection = _sqlite3.connect(repository.database_path, timeout=30.0)
        connection.row_factory = _sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    now = _datetime(2026, 8, 25, 18, 0, tzinfo=SHANGHAI)
    importer = LegacyImporter(factory, now=lambda: now)
    candidate_id = "cand-seed"
    candidate = importer.build_candidate(
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        plan_id="plan-seed",
        candidate_id=candidate_id,
    )
    for data_type, adjustment in (
        ("stocks", None),
        ("daily_bars", AdjustmentMethod.QFQ),
        ("fundamentals", None),
    ):
        importer.import_partition(
            candidate,
            data_type=data_type,
            partition_key=TARGET_DAY.isoformat(),
            batch_id=f"batch-seed-{data_type}",
            source="seed",
            adjustment=adjustment,
        )
    finished = importer.finish_candidate(candidate)

    verifier = CoverageVerifier(
        factory,
        trading_days=lambda start, end: (
            (TARGET_DAY,) if start <= TARGET_DAY <= end else ()
        ),
        expected_universe_size=lambda day: 2,
    )
    from stock_manager.domain import SyncTask, SyncTaskStatus

    tasks = tuple(
        SyncTask(
            task_id=f"seed-{data_type}",
            plan_id="plan-seed",
            sequence_no=index,
            data_type=data_type,
            partition_key=TARGET_DAY.isoformat(),
            codes=("sh.600001", "sz.000002"),
            range_start=TARGET_DAY,
            range_end=TARGET_DAY,
            dependencies=(),
            status=SyncTaskStatus.SUCCESS,
            attempt_count=1,
            not_before=None,
            row_count=2,
            error_code=None,
            error_message=None,
            started_at=now,
            finished_at=now,
        )
        for index, data_type in enumerate(
            ("stocks", "daily_bars", "fundamentals")
        )
    )
    outcome = verifier.verify(
        finished,
        adjustment=AdjustmentMethod.QFQ,
        target_start=TARGET_DAY,
        target_end=TARGET_DAY,
        tasks=tasks,
    )
    for record in outcome.records:
        repository.save_coverage_verification(record)
    repository.update_candidate_status(
        candidate_id, CandidateGenerationStatus.VERIFIED, now
    )
    verified = repository.get_candidate_generation(candidate_id)
    if verified is None:
        raise AssertionError("candidate missing after VERIFIED")
    committer = GenerationCommitter(factory, now=lambda: now)
    partitions = importer.partitions_for(candidate_id, generation=candidate_id)
    committer.publish(
        verified,
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        verifications=outcome.records,
        partitions=partitions,
    )


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


def _get_query(app: WebApp, path: str, query: dict[str, str]) -> tuple[int, object]:
    status, _content_type, data = app.route(
        "GET", path, {key: [value] for key, value in query.items()}, None
    )
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


def test_sync_status_exposes_p5_plan_state(tmp_path: Path) -> None:
    """P5-RD-8: /api/sync/status 暴露 plan/task/candidate/active generation。"""
    app = _app(tmp_path)
    # 先写入一个 P5 plan(直接经 repository,模拟流水线产物)。
    from stock_manager.domain import (
        AdjustmentMethod,
        CandidateGeneration,
        CandidateGenerationStatus,
        SyncPlan,
        SyncPlanMode,
        SyncPlanStatus,
        SyncSource,
        SyncTask,
        SyncTaskStatus,
    )
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    repo = app._services.repository
    plan = SyncPlan(
        plan_id="plan-test",
        plan_version=1,
        mode=SyncPlanMode.BOOTSTRAP,
        source=SyncSource.BAOSTOCK,
        dataset_id="market",
        adjustment=AdjustmentMethod.QFQ,
        universe_policy="a-share",
        target_start=date(2026, 9, 1),
        target_end=date(2026, 9, 1),
        latest_completed_trading_day=date(2026, 9, 1),
        parent_generation=None,
        candidate_generation_id="cand-test",
        required_data_types=("daily_bars",),
        task_count=1,
        plan_fingerprint="fp",
        status=SyncPlanStatus.SUCCEEDED,
        created_at=now,
        updated_at=now,
    )
    repo.save_sync_plan(plan)
    repo.save_candidate_generation(
        CandidateGeneration(
            candidate_generation_id="cand-test",
            plan_id="plan-test",
            parent_generation=None,
            write_revision=1,
            status=CandidateGenerationStatus.PUBLISHED,
            created_at=now,
            updated_at=now,
        )
    )
    repo.save_sync_task(
        SyncTask(
            task_id="t1",
            plan_id="plan-test",
            sequence_no=0,
            data_type="daily_bars",
            partition_key="2026-09-01",
            codes=("sh.600001",),
            range_start=date(2026, 9, 1),
            range_end=date(2026, 9, 1),
            dependencies=(),
            status=SyncTaskStatus.SUCCESS,
            attempt_count=1,
            not_before=None,
            row_count=1,
            error_code=None,
            error_message=None,
            started_at=now,
            finished_at=now,
        )
    )
    repo.save_active_generation(
        __import__(
            "stock_manager.domain", fromlist=["ActiveGeneration"]
        ).ActiveGeneration("market", AdjustmentMethod.QFQ, "cand-test", now)
    )

    status, body = _get(app, "/api/sync/status")
    assert status == 200
    assert body["p5_plans"] and body["p5_plans"][0]["plan_id"] == "plan-test"
    assert body["p5_plans"][0]["status"] == "SUCCEEDED"
    assert body["p5_plans"][0]["task_counts"]["SUCCESS"] == 1
    assert body["p5_plans"][0]["candidate_status"] == "PUBLISHED"
    assert body["active_generation"] is not None
    assert body["active_generation"]["generation"] == "cand-test"


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


def test_app_starts_on_first_run_without_database(tmp_path: Path) -> None:
    """P5 §5.1:新用户无本地库时 Web 仍启动,进入 first_run 状态而非拒绝。"""
    db = tmp_path / "missing.sqlite3"
    config = WebConfig(
        database_path=db,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user",
        static_root=STATIC_ROOT,
        sync_config_path=REPO / "config" / "sync.json",
        lock_directory=tmp_path / "locks",
    )
    app = WebApp(config, provider_factory=_fake_provider)
    assert db.is_file()  # 空库被创建
    status, progress = _get(app, "/api/sync/progress")
    assert status == 200
    assert progress["status"] == "first_run"
    # 门禁:NO_GENERATION,筛选不可用并给出原因
    status, body = _get(app, "/api/sync/status")
    assert status == 200
    assert body["active_generation"] is None
    assert body["readiness"]["status"] == "NO_GENERATION"
    assert body["readiness"]["reason"] is not None


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


def test_screen_limit_up_rule_returns_event_dates(tmp_path: Path) -> None:
    # 种子数据:2026-08-25 收盘 109 / 前收 100 = 1.09,落在涨幅开区间 (1.08, 1.12)。
    # 涨幅次数规则必须返回具体涨幅日期,供前端 K 线信息栏展示。
    app = _app(tmp_path)
    status, payload = _post(app, "/api/screen", _screen_body(_default_template()))
    assert status == 200
    execution = next(
        item
        for item in payload["results"][0]["rule_executions"]
        if item["rule_id"] == "limit_up_3m"
    )
    assert execution["status"] == "PASSED"
    actual = execution["result"]["actual_value"]
    assert actual["count"] == 1
    assert actual["trading_days"] == ["2026-08-25"]


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


def test_shutdown_endpoint_requires_confirmation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _post(app, "/api/shutdown", {})
    assert status == 400
    assert payload["error"]["code"] == "BAD_REQUEST"
    status, _payload = _post(app, "/api/shutdown", {"confirm": False})
    assert status == 400


def test_shutdown_endpoint_invokes_handler_after_confirmation(tmp_path: Path) -> None:
    database_path = tmp_path / "market.sqlite3"
    _seed_repository(database_path)
    calls: list[bool] = []
    config = WebConfig(
        database_path=database_path,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
    )
    app = WebApp(config, shutdown_handler=lambda: calls.append(True))

    status, payload = _post(app, "/api/shutdown", {"confirm": True})

    assert status == 200
    assert payload == {"status": "shutting_down"}
    assert calls == [True]


def test_screen_reads_seeded_local_data(tmp_path: Path) -> None:
    # 手动同步按钮已移除;数据由启动自动回补/CLI 写入。这里直接对已种入的本地数据筛选。
    app = _app(tmp_path)

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
    assert progress["status"] == "first_run"  # P5 §5.1:无 active generation

    # 启动自动回补通过这两个处理器写进度;这里直接驱动它们验证端点。
    app._sync_progress.update({"status": "idle"})
    app._on_backfill_progress(
        {"phase": "daily_bars", "completed": 12, "total": 100, "current_code": "sh.600000"}
    )
    app._on_backfill_batch_progress(
        {"phase": "daily_bars", "index": 3, "total": 100, "current_code": "sh.600003"}
    )
    status, progress = _get(app, "/api/sync/progress")
    assert status == 200
    assert progress["status"] == "running"
    assert progress["phase"] == "daily_bars"
    assert progress["completed"] == 12
    assert progress["batch_completed"] == 3


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


def test_sync_status_endpoint_summarizes_local_coverage(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get(app, "/api/sync/status")
    assert status == 200
    # 种子数据:最新交易日 2026-08-25,覆盖 6 个交易日,2 只股票。
    assert payload["latest_synced_trading_day"] == TARGET_DAY.isoformat()
    assert payload["coverage_end"] == TARGET_DAY.isoformat()
    assert payload["coverage_start"] == (TARGET_DAY - timedelta(days=359)).isoformat()
    assert payload["stocks_count"] == 2
    # 最近 30 自然日:每个 1 项。
    assert len(payload["recent_days"]) == 30
    for entry in payload["recent_days"][:6]:
        assert entry["status"] == "synced"
    # 更早的 11 段(每段约 30 天)均在覆盖窗口之前 → 覆盖率 0;无 bar → 不判为 incomplete。
    assert len(payload["older_bands"]) == 11
    assert all(band["coverage"] == 0 for band in payload["older_bands"])
    assert all(band["incomplete"] is False for band in payload["older_bands"])
    # 年度覆盖条:每块 1 年,固定显示目标窗口(默认 8 年:2018~2026)
    assert len(payload["year_bands"]) == 9
    assert payload["year_bands"][0]["year"] == 2018
    assert payload["year_bands"][-1]["year"] == 2026
    assert all("coverage" in band and "incomplete" in band for band in payload["year_bands"])
    assert all("has_data" in band and "trading_days" in band for band in payload["year_bands"])
    assert payload["year_bands"][0]["has_data"] is False  # 2018 未回补:无数据


def test_version_endpoint_reports_package_version(tmp_path: Path) -> None:
    from stock_manager import __version__
    app = _app(tmp_path)
    status, payload = _get(app, "/api/version")
    assert status == 200
    assert payload["version"] == __version__


def test_sync_status_label_reflects_record_status(tmp_path: Path) -> None:
    app = _app(tmp_path)
    def rec(status: SyncStatus) -> SyncRecord:
        return SyncRecord(
            "market", TARGET_DAY, status, "fixture", AdjustmentMethod.QFQ,
            datetime(2026, 8, 25, 18, tzinfo=SHANGHAI),
            datetime(2026, 8, 25, 18, tzinfo=SHANGHAI) if status is not SyncStatus.RUNNING else None,
            "boom" if status is SyncStatus.FAILED else None,
        )
    # 完整且无记录 → 绿;完整且有 SUCCESS → 绿;RUNNING → 橙;FAILED → 红。
    assert app._sync_status_label(None, True, True) == "synced"
    assert app._sync_status_label(rec(SyncStatus.SUCCESS), True, True) == "synced"
    assert app._sync_status_label(rec(SyncStatus.RUNNING), False, True) == "running"
    assert app._sync_status_label(rec(SyncStatus.FAILED), False, True) == "failed"
    # 有 bar 但不完整(部分拉取) → 橙;无 bar 无记录 → 灰。
    assert app._sync_status_label(None, False, True) == "incomplete"
    assert app._sync_status_label(None, False, False) == "missing"


def test_instances_endpoint_reports_local_processes(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get(app, "/api/instances")
    assert status == 200
    assert "instances" in payload


def test_kill_instance_rejects_unknown_pid(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _post(app, "/api/instances/kill", {"pid": 99999999})
    assert status == 404


# ---------- local daily bars (K-line source) ----------
def test_bars_returns_recent_local_daily_bars(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get_query(
        app,
        "/api/bars",
        {"code": "sh.600001", "adjustment": "qfq", "end": "2026-08-25", "days": "250"},
    )
    assert status == 200
    assert payload["code"] == "sh.600001"
    assert payload["adjustment"] == "qfq"
    assert payload["end"] == "2026-08-25"
    bars = payload["bars"]
    # 种子数据覆盖 6 个交易日(2026-08-20..08-25)。
    assert len(bars) == 6
    assert [bar["trading_day"] for bar in bars] == [
        "2026-08-20",
        "2026-08-21",
        "2026-08-22",
        "2026-08-23",
        "2026-08-24",
        "2026-08-25",
    ]
    last = bars[-1]
    assert last["close"] == "109"
    assert last["open"] == "100"
    assert last["high"] == "109"
    assert last["low"] == "99"
    assert last["volume"] == "400"
    assert last["amount"] == "1000"
    assert last["is_trading"] is True
    for bar in bars:
        assert set(bar) == {
            "code",
            "trading_day",
            "open",
            "high",
            "low",
            "close",
            "preclose",
            "volume",
            "amount",
            "is_trading",
        }


def test_bars_respects_days_limit_and_defaults_end_to_latest_synced_day(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    status, payload = _get_query(
        app, "/api/bars", {"code": "sh.600001", "adjustment": "qfq", "days": "3"}
    )
    assert status == 200
    assert payload["end"] == TARGET_DAY.isoformat()
    assert [bar["trading_day"] for bar in payload["bars"]] == [
        "2026-08-23",
        "2026-08-24",
        "2026-08-25",
    ]


def test_bars_unknown_code_returns_empty_list(tmp_path: Path) -> None:
    app = _app(tmp_path)
    status, payload = _get_query(
        app, "/api/bars", {"code": "sh.999999", "adjustment": "qfq"}
    )
    assert status == 200
    assert payload["bars"] == []


def test_bars_missing_adjustment_dataset_returns_404(tmp_path: Path) -> None:
    app = _app(tmp_path)
    # 种子数据只有 qfq 数据集;hfq 无最新元数据,end 缺省时明确 404。
    status, payload = _get_query(
        app, "/api/bars", {"code": "sh.600001", "adjustment": "hfq"}
    )
    assert status == 404
    assert payload["error"]["code"] == "NOT_FOUND"


def test_bars_rejects_invalid_parameters(tmp_path: Path) -> None:
    app = _app(tmp_path)
    cases = [
        {"adjustment": "qfq"},
        {"code": "sh.600001"},
        {"code": "sh.600001", "adjustment": "bad"},
        {"code": "sh.600001", "adjustment": "qfq", "days": "0"},
        {"code": "sh.600001", "adjustment": "qfq", "days": "501"},
        {"code": "sh.600001", "adjustment": "qfq", "days": "abc"},
        {"code": "sh.600001", "adjustment": "qfq", "end": "2026-13-01"},
    ]
    for query in cases:
        status, payload = _get_query(app, "/api/bars", query)
        assert status == 400, query
        assert payload["error"]["code"] == "BAD_REQUEST"


def test_sync_status_marks_partial_bar_day_as_incomplete(tmp_path: Path) -> None:
    """A day with only a fraction of the universe's bars is not fully synced."""
    db = tmp_path / "market.sqlite3"
    db.touch()
    repository = SQLiteRepository(db)
    DAY = date(2026, 8, 25)
    now = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)
    days = (date(2026, 8, 24), DAY)
    # 股票池 120 只,但该日只有 20 只写入了 bar(< 95%).
    stocks = tuple(
        StockIdentity(f"sh.{600000 + i:06d}", f"股{i}", "SH", False, None, None)
        for i in range(120)
    )
    bars = tuple(
        DailyBar(
            s.code, DAY, Decimal("10"), Decimal("11"), Decimal("9"),
            Decimal("10.5"), Decimal("10"), Decimal("1000"), Decimal("10500"), True,
        )
        for s in stocks[:20]
    )
    metadata = DatasetMetadata("market", DAY, "fixture", now, AdjustmentMethod.QFQ)
    success = SyncRecord(
        "market", DAY, SyncStatus.SUCCESS, "fixture", AdjustmentMethod.QFQ, now, now, None,
    )
    repository.save_market_snapshot(stocks, bars, (), (), days, metadata, success)

    config = WebConfig(
        database_path=db,
        system_template_root=SYSTEM_TEMPLATES,
        user_template_root=tmp_path / "user-templates",
        static_root=STATIC_ROOT,
    )
    app = WebApp(config)
    status, payload = _get(app, "/api/sync/status")
    assert status == 200
    assert payload["stocks_count"] == 120
    # 最新交易日有 bar 但只覆盖 20/120 → 未完全同步 → incomplete(Orange)。
    latest = next(e for e in payload["recent_days"] if e["day"] == DAY.isoformat())
    assert latest["status"] == "incomplete"



# ---------- max_workers & elapsed (worker 配置) ----------
def test_screen_accepts_max_workers_and_reports_elapsed(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = _screen_body(_default_template())
    body["max_workers"] = 2
    status, payload = _post(app, "/api/screen", body)
    assert status == 200
    assert payload["max_workers"] == 2
    assert isinstance(payload["elapsed_seconds"], (int, float))
    assert payload["elapsed_seconds"] >= 0


def test_screen_max_workers_upper_bound_is_16(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = _screen_body(_default_template())
    body["max_workers"] = 17
    status, payload = _post(app, "/api/screen", body)
    assert status == 400
    assert payload["error"]["code"] == "BAD_REQUEST"
    assert "max_workers" in payload["error"]["message"]


def test_screen_max_workers_rejects_non_integer(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = _screen_body(_default_template())
    body["max_workers"] = "fast"
    status, payload = _post(app, "/api/screen", body)
    assert status == 400
    assert payload["error"]["code"] == "BAD_REQUEST"


def test_screen_max_workers_one_works(tmp_path: Path) -> None:
    app = _app(tmp_path)
    body = _screen_body(_default_template())
    body["max_workers"] = 1
    status, payload = _post(app, "/api/screen", body)
    assert status == 200
    assert payload["summary"]["total"] == 1
    assert payload["max_workers"] == 1


class TestBootstrapEndpoint:
    """P5 §5.1/§5.3:首次初始化端点。"""

    def test_bootstrap_online_runs_pipeline(self, tmp_path: Path) -> None:
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
        # 首次启动状态
        _status, progress = _get(app, "/api/sync/progress")
        assert progress["status"] == "first_run"
        # 触发在线 Bootstrap
        status, payload = _post(app, "/api/sync/bootstrap", {"source": "online", "adjustment": "qfq"})
        assert status == 200, payload
        assert payload["source"] == "online"
        # 空库无交易日历 → online bootstrap 无法规划,应返回 4xx 而非崩溃
        # (fake provider 提供 TARGET_DAY 日历;这里验证端点可达且结构化)
        assert isinstance(payload, dict)

    def test_bootstrap_bad_source_rejected(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        status, payload = _post(app, "/api/sync/bootstrap", {"source": "bogus"})
        assert status == 400

    def _configured_app(self, tmp_path: Path) -> WebApp:
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
        return WebApp(config, provider_factory=_fake_provider)

    def test_bootstrap_seed_requires_path(self, tmp_path: Path) -> None:
        app = self._configured_app(tmp_path)
        status, payload = _post(app, "/api/sync/bootstrap", {"source": "seed"})
        assert status == 400
        assert "seed_path" in payload["error"]["message"]

    def test_bootstrap_seed_invalid_manifest_rejected(self, tmp_path: Path) -> None:
        app = self._configured_app(tmp_path)
        status, payload = _post(
            app,
            "/api/sync/bootstrap",
            {"source": "seed", "seed_path": str(tmp_path / "nope.sqlite3")},
        )
        assert status == 400
        assert "manifest" in payload["error"]["message"]


class TestIncrementalBootstrap:
    def test_incremental_without_active_generation_rejected(self, tmp_path: Path) -> None:
        db = tmp_path / "m.sqlite3"
        SQLiteRepository(db)
        locks = tmp_path / "locks"
        locks.mkdir()
        config = WebConfig(
            database_path=db,
            system_template_root=SYSTEM_TEMPLATES,
            user_template_root=tmp_path / "ut",
            static_root=STATIC_ROOT,
            sync_config_path=REPO / "config" / "sync.json",
            lock_directory=locks,
        )
        app = WebApp(config, provider_factory=_fake_provider)
        status, payload = _post(app, "/api/sync/bootstrap", {"source": "incremental", "adjustment": "qfq"})
        assert status == 404
        assert "no active generation" in payload["error"]["message"]


class TestStockBasedProgress:
    def test_partial_batch_is_not_100_percent(self, tmp_path: Path) -> None:
        """P5:进度按完整入库股票数,而非批次。"""
        db = tmp_path / "m.sqlite3"
        repo = SQLiteRepository(db)
        from datetime import timedelta as _td

        # 长窗口(模拟八年)只入库 1 只股票 2 天 → 远未完整,绝不是 100%。
        target_end = date(2026, 8, 25)
        target_start = date(2018, 9, 1)
        stocks = (
            StockIdentity("sh.600001", "A", "SH", False, date(2000, 1, 1), None),
            StockIdentity("sz.000002", "B", "SZ", True, date(2000, 1, 1), None),
        )
        bar_days = (target_end, target_end - _td(days=1))
        now = datetime(2026, 8, 25, 18, 0, tzinfo=SHANGHAI)
        # 注册 500 个模拟交易日:代码只覆盖其中 2 天 → 远未完整
        from datetime import timedelta as _td2

        calendar_days = tuple(target_end - _td2(days=i) for i in range(500))
        repo.save_trading_days(
            calendar_days,
            DatasetMetadata("trading_calendar", target_end, "fx", now, AdjustmentMethod.UNADJUSTED),
        )
        repo.save_stocks(
            stocks,
            DatasetMetadata("market", target_end, "fx", now, AdjustmentMethod.QFQ),
        )
        repo.save_daily_bars(
            (
                DailyBar("sh.600001", bar_days[0], Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("100"), Decimal("1000"), True),
                DailyBar("sh.600001", bar_days[1], Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("100"), Decimal("1000"), True),
            ),
            DatasetMetadata("market", target_end, "fx", now, AdjustmentMethod.QFQ),
        )
        config = WebConfig(
            database_path=db,
            system_template_root=SYSTEM_TEMPLATES,
            user_template_root=tmp_path / "ut",
            static_root=STATIC_ROOT,
            sync_config_path=REPO / "config" / "sync.json",
            lock_directory=tmp_path / "locks",
        )
        app = WebApp(config, provider_factory=_fake_provider)
        from stock_manager.domain import BackfillRunStatus, BackfillRunV2

        repo.save_backfill_run_v2(
            BackfillRunV2(
                run_id="r1",
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                target_start=target_start,
                target_end=target_end,
                status=BackfillRunStatus.RUNNING,
                started_at=now,
                finished_at=None,
                error_message=None,
            )
        )
        _status, body = _get(app, "/api/sync/backfill/progress")
        assert body["status"] == "RUNNING"
        # 池 2 只,窗口 2000+ 天,1 只远未完整 → 进度极低,绝不是 100%
        assert body["progress"] < 0.1
        assert body["total_days"] == 2  # 股票池规模


class TestListingWindowProgress:
    def test_newly_listed_stock_not_falsely_incomplete(self, tmp_path: Path) -> None:
        """P5 §7.3:窗口中途上市的股票只按上市以来交易日判完整。"""
        db = tmp_path / "m.sqlite3"
        repo = SQLiteRepository(db)
        from datetime import timedelta as _td

        window_start = date(2018, 9, 1)
        window_end = date(2026, 8, 25)
        now = datetime(2026, 8, 25, 18, 0, tzinfo=SHANGHAI)
        # 股票 A:窗口初上市(覆盖全窗口);股票 B:2026-08-01 才上市(只应算 8 月)。
        stocks = (
            StockIdentity("sh.600001", "A", "SH", False, date(2000, 1, 1), None),
            StockIdentity("sz.000002", "B", "SZ", False, date(2026, 8, 1), None),
        )
        repo.save_stocks(
            stocks,
            DatasetMetadata("market", window_end, "fx", now, AdjustmentMethod.QFQ),
        )
        # 交易日:窗口内 100 个自然日(模拟 100 个交易日)
        calendar = tuple(window_end - _td(days=i) for i in range(100))
        repo.save_trading_days(
            calendar,
            DatasetMetadata("trading_calendar", window_end, "fx", now, AdjustmentMethod.UNADJUSTED),
        )
        # 股票 A:覆盖窗口全部 100 天;股票 B:8-01 上市,覆盖 8-01 之后全部 25 天。
        b_start = date(2026, 8, 1)
        b_days = tuple(d for d in calendar if d >= b_start)  # 08-01..08-25 共 25 天
        bars_a = tuple(
            DailyBar("sh.600001", d, Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("100"), Decimal("1000"), True)
            for d in calendar
        )
        bars_b = tuple(
            DailyBar("sz.000002", d, Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("10"), Decimal("100"), Decimal("1000"), True)
            for d in b_days
        )
        repo.save_daily_bars(
            bars_a + bars_b,
            DatasetMetadata("market", window_end, "fx", now, AdjustmentMethod.QFQ),
        )
        config = WebConfig(
            database_path=db,
            system_template_root=SYSTEM_TEMPLATES,
            user_template_root=tmp_path / "ut",
            static_root=STATIC_ROOT,
            sync_config_path=REPO / "config" / "sync.json",
            lock_directory=tmp_path / "locks",
        )
        app = WebApp(config, provider_factory=_fake_provider)
        from stock_manager.domain import BackfillRunStatus, BackfillRunV2

        repo.save_backfill_run_v2(
            BackfillRunV2(
                run_id="r2",
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                target_start=window_start,
                target_end=window_end,
                status=BackfillRunStatus.RUNNING,
                started_at=now,
                finished_at=None,
                error_message=None,
            )
        )
        _status, body = _get(app, "/api/sync/backfill/progress")
        assert body["status"] == "RUNNING"
        # 两只都完整:股票 B 只看上市以来(08-01 后 25 天全有)
        assert body["covered_days"] == 2
        assert body["total_days"] == 2
        assert body["progress"] == 1.0


class TestPipelineProgressEndpoint:
    """P5:新架构回补的实时进度端点。"""

    def test_pipeline_progress_none_without_plan(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        status, body = _get(app, "/api/sync/pipeline/progress")
        assert status == 200
        assert body["status"] == "none"

    def test_pipeline_progress_reports_plan(self, tmp_path: Path) -> None:
        app = _app(tmp_path)
        repo = app._services.repository
        from datetime import datetime
        from zoneinfo import ZoneInfo

        from stock_manager.domain import (
            AdjustmentMethod,
            CandidateGeneration,
            CandidateGenerationStatus,
            SyncPlan,
            SyncPlanMode,
            SyncPlanStatus,
            SyncSource,
            SyncTask,
            SyncTaskStatus,
        )

        now = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        plan = SyncPlan(
            plan_id="plan-prog",
            plan_version=1,
            mode=SyncPlanMode.BOOTSTRAP,
            source=SyncSource.BAOSTOCK,
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            universe_policy="a-share",
            target_start=date(2018, 7, 12),
            target_end=date(2026, 9, 1),
            latest_completed_trading_day=date(2026, 9, 1),
            parent_generation=None,
            candidate_generation_id="cand-prog",
            required_data_types=("daily_bars",),
            task_count=2,
            plan_fingerprint="fp",
            status=SyncPlanStatus.RUNNING,
            created_at=now,
            updated_at=now,
        )
        repo.save_sync_plan(plan)
        repo.save_candidate_generation(
            CandidateGeneration(
                candidate_generation_id="cand-prog",
                plan_id="plan-prog",
                parent_generation=None,
                write_revision=0,
                status=CandidateGenerationStatus.PLANNED,
                created_at=now,
                updated_at=now,
            )
        )
        repo.save_sync_task(
            SyncTask(
                task_id="t1", plan_id="plan-prog", sequence_no=0,
                data_type="daily_bars", partition_key="2018-07-12..2026-09-01",
                codes=("sh.600000", "sz.000001"), range_start=date(2018, 7, 12),
                range_end=date(2026, 9, 1), dependencies=(),
                status=SyncTaskStatus.SUCCESS, attempt_count=1, not_before=None,
                row_count=2, error_code=None, error_message=None,
                started_at=now, finished_at=now,
            )
        )
        repo.save_sync_task(
            SyncTask(
                task_id="t2", plan_id="plan-prog", sequence_no=1,
                data_type="daily_bars", partition_key="2018-07-12..2026-09-01",
                codes=("sh.600519", "sz.300750"), range_start=date(2018, 7, 12),
                range_end=date(2026, 9, 1), dependencies=(),
                status=SyncTaskStatus.RUNNING, attempt_count=1, not_before=None,
                row_count=None, error_code=None, error_message=None,
                started_at=now, finished_at=None,
            )
        )
        repo.update_task_progress(
            "t2",
            '{"data_type":"daily_bars","partition_key":"2018-07-12..2026-09-01",'
            '"completed":1,"total":2,"current_code":"sh.600519","status":"RUNNING"}',
        )
        status, body = _get(app, "/api/sync/pipeline/progress")
        assert status == 200
        assert body["plan_id"] == "plan-prog"
        assert body["status"] == "RUNNING"
        assert body["completed_tasks"] == 1
        assert body["total_tasks"] == 2
        assert body["progress"] == 0.5
        assert body["batch"]["completed"] == 1
        assert body["batch"]["total"] == 2
        assert body["batch"]["current_code"] == "sh.600519"
