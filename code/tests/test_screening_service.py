from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import StringIO
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    DividendRecord,
    FundamentalSnapshot,
    StockIdentity,
    SyncRecord,
    SyncStatus,
)
from stock_manager.cli.main import main
from stock_manager.rules import load_rules_config
from stock_manager.rules.builtin import build_default_registry
from stock_manager.services import (
    DatasetUnavailableError,
    ScreeningService,
    StockNotFoundError,
)
from stock_manager.services.parameterized_screening_service import (
    ParameterizedScreeningService,
)
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template
from stock_manager.storage.sqlite_repo import SQLiteRepository


TARGET_DAY = date(2026, 8, 25)
SHANGHAI = ZoneInfo("Asia/Shanghai")


def _seed_repository(tmp_path: Path) -> SQLiteRepository:
    repository = SQLiteRepository(tmp_path / "market.sqlite3")
    days = tuple(date(2026, 8, day) for day in range(20, 26))
    stocks = (
        StockIdentity("sh.600001", "Alpha", "SH", False, date(2000, 1, 1), None),
        StockIdentity("sz.000002", "Beta", "SZ", True, date(2001, 1, 1), None),
    )
    bars: list[DailyBar] = []
    closes = (Decimal("100"),) * 5 + (Decimal("109"),)
    for code in (stock.code for stock in stocks):
        for index, day in enumerate(days):
            close = closes[index]
            previous_close = Decimal("100") if index == 0 else closes[index - 1]
            bars.append(
                DailyBar(
                    code,
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
    metadata = DatasetMetadata(
        "market", TARGET_DAY, "fixture", now, AdjustmentMethod.QFQ
    )
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


def _service(repository: SQLiteRepository) -> ScreeningService:
    config_path = Path(__file__).parents[1] / "config" / "rules.json"
    return ScreeningService(repository, load_rules_config(config_path))


def test_screen_reads_local_snapshot_and_returns_stable_order(tmp_path: Path) -> None:
    service = _service(_seed_repository(tmp_path))

    results = service.screen(
        "market",
        TARGET_DAY,
        AdjustmentMethod.QFQ,
        ("sz.000002", "sh.600001", "sh.600001"),
    )

    assert tuple(result.code for result in results) == ("sh.600001", "sz.000002")
    assert tuple(rule.rule_id for rule in results[0].rule_results) == (
        "pe_positive",
        "non_st",
        "volume_price_5d",
        "limit_up_breakout",
        "limit_up_3m",
        "volatility_multiple",
        "composite",
    )
    assert results[0].passed is True
    assert results[1].passed is False


def test_screen_without_codes_uses_entire_local_universe(tmp_path: Path) -> None:
    results = _service(_seed_repository(tmp_path)).screen(
        "market", TARGET_DAY, AdjustmentMethod.QFQ
    )

    assert tuple(result.code for result in results) == ("sh.600001", "sz.000002")


def test_screen_rejects_unknown_code(tmp_path: Path) -> None:
    with pytest.raises(StockNotFoundError, match="sh.999999"):
        _service(_seed_repository(tmp_path)).screen(
            "market", TARGET_DAY, AdjustmentMethod.QFQ, ("sh.999999",)
        )


def test_screen_requires_exact_local_metadata(tmp_path: Path) -> None:
    with pytest.raises(DatasetUnavailableError, match="unavailable"):
        _service(_seed_repository(tmp_path)).screen(
            "other", TARGET_DAY, AdjustmentMethod.QFQ
        )


def test_screen_rejects_adjustment_different_from_rule_config(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="adjustment"):
        _service(_seed_repository(tmp_path)).screen(
            "market", TARGET_DAY, AdjustmentMethod.HFQ
        )


def test_parameterized_screening_uses_compiled_plan_and_reports_statuses(
    tmp_path: Path,
) -> None:
    repository = _seed_repository(tmp_path)
    registry = build_default_registry()
    template_path = (
        Path(__file__).parents[1]
        / "config"
        / "rule_templates"
        / "system-default.json"
    )
    template = parse_template(json.loads(template_path.read_text(encoding="utf-8")))
    plan = TemplateCompiler(registry).compile(template)

    results = ParameterizedScreeningService(repository, registry).screen(
        plan,
        "market",
        TARGET_DAY,
        AdjustmentMethod.QFQ,
        ("sh.600001",),
    )

    assert len(results) == 1
    assert results[0].name == "Alpha"
    assert results[0].passed is True
    assert results[0].template_id == "system-default"
    annual = next(
        item
        for item in results[0].rule_executions
        if item.rule_id == "annual_min_volume"
    )
    assert annual.result is not None
    assert annual.result.reason == "insufficient valid trading sessions"


def test_parameterized_screening_reports_progress(tmp_path: Path) -> None:
    repository = _seed_repository(tmp_path)
    registry = build_default_registry()
    template_path = (
        Path(__file__).parents[1]
        / "config"
        / "rule_templates"
        / "system-default.json"
    )
    template = parse_template(json.loads(template_path.read_text(encoding="utf-8")))
    plan = TemplateCompiler(registry).compile(template)
    events: list[dict[str, object]] = []

    ParameterizedScreeningService(repository, registry).screen(
        plan,
        "market",
        TARGET_DAY,
        AdjustmentMethod.QFQ,
        ("sh.600001", "sz.000002"),
        progress_callback=events.append,
    )

    assert len(events) == 2
    assert events[0]["done"] == 1
    assert events[0]["total"] == 2
    assert events[0]["current_code"] == "sh.600001"
    assert events[1]["done"] == 2
    assert events[1]["total"] == 2


def test_cli_screen_json_is_offline_and_machine_readable(tmp_path: Path) -> None:
    repository = _seed_repository(tmp_path)
    stdout = StringIO()
    stderr = StringIO()

    exit_code = main(
        (
            "screen",
            "--db",
            str(tmp_path / "market.sqlite3"),
            "--rules",
            str(Path(__file__).parents[1] / "config" / "rules.json"),
            "--date",
            TARGET_DAY.isoformat(),
            "--adjustment",
            "qfq",
            "--code",
            "sh.600001",
        ),
        stdout=stdout,
        stderr=stderr,
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert payload[0]["code"] == "sh.600001"
    assert payload[0]["metadata"]["adjustment"] == "qfq"


def test_cli_screen_template_is_offline_and_reports_template_revision(
    tmp_path: Path,
) -> None:
    _seed_repository(tmp_path)
    stdout = StringIO()
    template_path = (
        Path(__file__).parents[1]
        / "config"
        / "rule_templates"
        / "system-default.json"
    )

    exit_code = main(
        (
            "screen-template",
            "--db",
            str(tmp_path / "market.sqlite3"),
            "--template",
            str(template_path),
            "--date",
            TARGET_DAY.isoformat(),
            "--adjustment",
            "qfq",
            "--code",
            "sh.600001",
        ),
        stdout=stdout,
        stderr=StringIO(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload[0]["template_id"] == "system-default"
    assert payload[0]["template_revision"] == 1
    assert payload[0]["rule_executions"][0]["status"] == "PASSED"


def test_cli_status_reports_latest_local_snapshot(tmp_path: Path) -> None:
    repository = _seed_repository(tmp_path)
    stdout = StringIO()

    exit_code = main(
        (
            "status",
            "--db",
            str(tmp_path / "market.sqlite3"),
            "--lock-dir",
            str(tmp_path / "locks"),
            "--adjustment",
            "qfq",
        ),
        stdout=stdout,
        stderr=StringIO(),
    )

    payload = json.loads(stdout.getvalue())
    assert exit_code == 0
    assert payload["latest_sync"]["trading_day"] == TARGET_DAY.isoformat()
    assert payload["latest_sync"]["status"] == "SUCCESS"
    assert payload["lock"]["held"] is False


def test_cli_read_commands_do_not_create_a_missing_database(tmp_path: Path) -> None:
    database_path = tmp_path / "missing.sqlite3"
    stderr = StringIO()

    exit_code = main(
        (
            "status",
            "--db",
            str(database_path),
            "--lock-dir",
            str(tmp_path / "locks"),
            "--adjustment",
            "qfq",
        ),
        stdout=StringIO(),
        stderr=stderr,
    )

    assert exit_code == 1
    assert not database_path.exists()
    assert json.loads(stderr.getvalue())["error"] == "ValueError"
