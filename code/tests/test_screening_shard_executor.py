"""Offline tests for P4-4 ScreeningShardExecutor (sharded screening)."""

from __future__ import annotations

import json
import sqlite3
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
    StockIdentity,
    SyncRecord,
    SyncStatus,
)
from stock_manager.read.plan_view import PicklableScreeningPlan
from stock_manager.read.sqlite_reader import SQLiteMarketDataReaderFactory
from stock_manager.rules.builtin import build_default_registry
from stock_manager.services.screening_data import ScreeningDataPlan, ScreeningDataPlanner
from stock_manager.services.screening_shard_executor import ScreeningShardExecutor
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template


class _BrokenFactory(SQLiteMarketDataReaderFactory):
    """Module-level broken factory so spawn can pickle it."""

    def create(self):
        reader = super().create()
        original = reader.read_batch

        def failing(request):
            if len(request.codes) > 0 and "sh.600001" in request.codes:
                raise RuntimeError("worker boom")
            return original(request)

        reader.read_batch = failing  # type: ignore[method-assign]
        return reader


TARGET_DAY = date(2026, 8, 25)
SHANGHAI = ZoneInfo("Asia/Shanghai")
QFQ = AdjustmentMethod.QFQ
NOW = datetime(2026, 8, 25, 18, tzinfo=SHANGHAI)


def _seed(database_path: Path, code_count: int = 6) -> SQLiteRepository:
    repository = SQLiteRepository(database_path)
    days = tuple(date(2026, 8, day) for day in range(20, 26))
    stocks = tuple(
        StockIdentity(
            f"sh.{600000 + index:06d}",
            f"Name{index}",
            "SH",
            False,
            date(2000, 1, 1),
            None,
        )
        for index in range(code_count)
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
    metadata = DatasetMetadata("market", TARGET_DAY, "fixture", NOW, QFQ)
    success = SyncRecord(
        "market", TARGET_DAY, SyncStatus.SUCCESS, "fixture", QFQ, NOW, NOW, None
    )
    repository.save_market_snapshot(
        stocks, bars, fundamentals, dividends, days, metadata, success
    )
    return repository


def _plan() -> PicklableScreeningPlan:
    registry = build_default_registry()
    template_path = (
        Path(__file__).parents[1] / "config" / "rule_templates" / "system-default.json"
    )
    template = parse_template(json.loads(template_path.read_text(encoding="utf-8")))
    plan = TemplateCompiler(registry).compile(template)
    return PicklableScreeningPlan.from_plan(plan)


def _executor(tmp_path: Path, max_workers: int, threshold: int = 0) -> tuple[ScreeningShardExecutor, SQLiteRepository]:
    database = tmp_path / "market.sqlite3"
    repository = _seed(database)
    registry = build_default_registry()
    executor = ScreeningShardExecutor(
        SQLiteMarketDataReaderFactory(database),
        registry,
        max_workers=max_workers,
        batch_size=2,
        small_data_serial_threshold=threshold,
    )
    return executor, repository


def _run(executor, repository, codes):
    registry = build_default_registry()
    template_path = (
        Path(__file__).parents[1] / "config" / "rule_templates" / "system-default.json"
    )
    template = parse_template(json.loads(template_path.read_text(encoding="utf-8")))
    plan = TemplateCompiler(registry).compile(template)
    metadata = repository.get_dataset_metadata("market", TARGET_DAY, QFQ)
    assert metadata is not None
    stocks = repository.get_stocks(TARGET_DAY)
    trading_days = tuple(repository.get_trading_days(date.min, TARGET_DAY))
    data_plan = ScreeningDataPlanner().plan(plan, TARGET_DAY, trading_days)
    return executor.execute(
        PicklableScreeningPlan.from_plan(plan),
        "market",
        TARGET_DAY,
        QFQ,
        metadata,
        stocks,
        codes,
        data_plan,
    )


def test_parallel_equals_serial_results(tmp_path: Path) -> None:
    serial_executor, repository = _executor(tmp_path, max_workers=1, threshold=0)
    parallel_executor, _ = _executor(tmp_path, max_workers=4, threshold=0)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(6))

    serial = _run(serial_executor, repository, codes)
    parallel = _run(parallel_executor, repository, codes)

    assert len(serial) == 6
    assert serial == parallel
    assert [item.code for item in serial] == list(codes)


def test_small_data_uses_serial_path(tmp_path: Path) -> None:
    executor, repository = _executor(tmp_path, max_workers=4, threshold=50)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(3))
    results = _run(executor, repository, codes)
    assert len(results) == 3


def test_empty_codes_returns_empty(tmp_path: Path) -> None:
    executor, repository = _executor(tmp_path, max_workers=4, threshold=0)
    assert _run(executor, repository, ()) == ()


def test_progress_serial_per_code(tmp_path: Path) -> None:
    """串行路径必须保持逐股进度回调（Web 依赖）。"""
    executor, repository = _executor(tmp_path, max_workers=1, threshold=0)
    events: list[dict[str, object]] = []

    registry = build_default_registry()
    template_path = (
        Path(__file__).parents[1] / "config" / "rule_templates" / "system-default.json"
    )
    template = parse_template(json.loads(template_path.read_text(encoding="utf-8")))
    plan = TemplateCompiler(registry).compile(template)
    metadata = repository.get_dataset_metadata("market", TARGET_DAY, QFQ)
    assert metadata is not None
    stocks = repository.get_stocks(TARGET_DAY)
    trading_days = tuple(repository.get_trading_days(date.min, TARGET_DAY))
    data_plan = ScreeningDataPlanner().plan(plan, TARGET_DAY, trading_days)
    codes = ("sh.600000", "sh.600001")
    executor.execute(
        PicklableScreeningPlan.from_plan(plan),
        "market",
        TARGET_DAY,
        QFQ,
        metadata,
        stocks,
        codes,
        data_plan,
        progress_callback=events.append,
    )

    assert len(events) == 2
    assert events[0]["done"] == 1
    assert events[0]["current_code"] == "sh.600000"
    assert events[1]["done"] == 2
    assert events[1]["current_code"] == "sh.600001"


def test_repeated_runs_are_deterministic(tmp_path: Path) -> None:
    executor, repository = _executor(tmp_path, max_workers=3, threshold=0)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(5))
    first = _run(executor, repository, codes)
    second = _run(executor, repository, codes)
    assert first == second


def test_worker_failure_propagates(tmp_path: Path) -> None:
    database = tmp_path / "market.sqlite3"
    repository = _seed(database)
    registry = build_default_registry()

    executor = ScreeningShardExecutor(
        _BrokenFactory(database), registry, max_workers=2, batch_size=2,
        small_data_serial_threshold=0,
    )
    from stock_manager.read.errors import ShardReadError

    with pytest.raises(ShardReadError) as excinfo:
        _run(executor, repository, tuple(f"sh.{600000 + index:06d}" for index in range(4)))
    assert isinstance(excinfo.value.cause, RuntimeError)
    assert "worker boom" in str(excinfo.value.cause)


class _BoomRule:
    """Module-level rule that raises during evaluate (spawn-safe)."""

    definition = __import__("stock_manager.rules.base", fromlist=["RuleDefinition"]).RuleDefinition(
        "boom_rule",
        "boom",
        "evaluate always raises",
        (),
    )

    def parse_parameters(self, raw):
        return None

    def data_requirement(self, parameters):
        return __import__("stock_manager.rules.base", fromlist=["RuleDataRequirement"]).RuleDataRequirement()

    def evaluate(self, context, parameters):
        raise RuntimeError("rule computation boom")


def test_rule_computation_failure_propagates(tmp_path: Path) -> None:
    """矩阵：worker 内规则计算抛异常时整体失败，不返回部分筛选结果。"""
    from stock_manager.rules.registry import RuleRegistry

    database = tmp_path / "market.sqlite3"
    repository = _seed(database, code_count=4)
    registry = build_default_registry()
    registry.register(_BoomRule())
    executor = ScreeningShardExecutor(
        SQLiteMarketDataReaderFactory(database),
        registry,
        max_workers=2,
        batch_size=2,
        small_data_serial_threshold=0,
    )

    # 用只含 boom_rule 的模板编译 plan
    boom_template = {
        "metadata": {
            "schema_version": 2,
            "template_id": "boom-test",
            "revision": 1,
            "name": "boom",
            "description": "boom",
            "timezone": "Asia/Shanghai",
            "technical_adjustment": "qfq",
        },
        "rules": {"boom_rule": {"enabled": True, "parameters": {}}},
        "composition": {
            "operator": "all",
            "groups": [{"group_id": "g1", "operator": "all", "rules": ["boom_rule"]}],
        },
    }
    from stock_manager.templates.models import parse_template as pt

    plan = TemplateCompiler(registry).compile(pt(boom_template))
    metadata = repository.get_dataset_metadata("market", TARGET_DAY, QFQ)
    assert metadata is not None
    stocks = repository.get_stocks(TARGET_DAY)
    trading_days = tuple(repository.get_trading_days(date.min, TARGET_DAY))
    data_plan = ScreeningDataPlanner().plan(plan, TARGET_DAY, trading_days)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(4))

    from stock_manager.read.errors import ShardReadError

    with pytest.raises(ShardReadError) as excinfo:
        executor.execute(
            PicklableScreeningPlan.from_plan(plan),
            "market",
            TARGET_DAY,
            QFQ,
            metadata,
            stocks,
            codes,
            data_plan,
        )
    assert isinstance(excinfo.value.cause, RuntimeError)
    assert "rule computation boom" in str(excinfo.value.cause)



def test_serial_fallback_without_database_path(tmp_path: Path) -> None:
    """m6: 无 database_path 的 repository 走 _serial_screen 回退，逐股回调保持。"""
    from stock_manager.protocols import LocalRepositoryProtocol  # noqa: F401  (协议引用)
    from stock_manager.services.parameterized_screening_service import (
        ParameterizedScreeningService,
    )

    database = tmp_path / "market.sqlite3"
    repository = _seed(database, code_count=3)

    class PathlessRepository(SQLiteRepository):
        @property
        def database_path(self) -> Path:
            raise AttributeError("no database_path in this fake")

    pathless = PathlessRepository(database)
    registry = build_default_registry()
    raw = json.loads(
        (Path(__file__).parents[1] / "config" / "rule_templates" / "system-default.json")
        .read_text(encoding="utf-8"),
    )
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    service = ParameterizedScreeningService(pathless, registry)
    assert service._executor is None  # 无 reader factory -> 回退
    events: list[dict[str, object]] = []
    results = service.screen(
        plan,
        "market",
        TARGET_DAY,
        QFQ,
        ("sh.600000", "sh.600001"),
        events.append,
    )
    assert len(results) == 2
    assert len(events) == 2
    assert events[0]["done"] == 1
    assert events[1]["done"] == 2


def test_screening_worker_detects_snapshot_change(tmp_path: Path) -> None:
    """M2 回归：筛选 worker 发现快照变化时抛 SnapshotConsistencyError 而非混合版本。"""
    from stock_manager.read.contracts import DatasetReadSnapshot
    from stock_manager.read.errors import SnapshotConsistencyError

    database = tmp_path / "market.sqlite3"
    repository = _seed(database, code_count=4)

    class ChangedMetadataFactory(SQLiteMarketDataReaderFactory):
        def create(self):
            reader = super().create()
            original = reader.read_batch

            def changed(request):
                batch = original(request)
                snapshot = batch.snapshot
                return type(batch)(
                    DatasetReadSnapshot(
                        snapshot.dataset_id,
                        snapshot.trading_day,
                        snapshot.adjustment,
                        "changed-source",
                        snapshot.synced_at,
                    ),
                    batch.codes,
                    batch.bars,
                    batch.fundamentals,
                    batch.dividends,
                )

            reader.read_batch = changed  # type: ignore[method-assign]
            return reader

    registry = build_default_registry()
    executor = ScreeningShardExecutor(
        ChangedMetadataFactory(database),
        registry,
        max_workers=1,
        batch_size=2,
        small_data_serial_threshold=0,
    )
    raw = json.loads(
        (Path(__file__).parents[1] / "config" / "rule_templates" / "system-default.json")
        .read_text(encoding="utf-8"),
    )
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    metadata = repository.get_dataset_metadata("market", TARGET_DAY, QFQ)
    assert metadata is not None
    stocks = repository.get_stocks(TARGET_DAY)
    trading_days = tuple(repository.get_trading_days(date.min, TARGET_DAY))
    data_plan = ScreeningDataPlanner().plan(plan, TARGET_DAY, trading_days)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(4))
    with pytest.raises(SnapshotConsistencyError):
        executor.execute(
            PicklableScreeningPlan.from_plan(plan),
            "market",
            TARGET_DAY,
            QFQ,
            metadata,
            stocks,
            codes,
            data_plan,
        )



def test_parallel_failure_cancels_pending_shards(tmp_path: Path) -> None:
    """M3 回归：某分片失败时其余未完成任务被取消，而非等待全部跑完。"""
    from stock_manager.read.errors import ShardReadError

    database = tmp_path / "market.sqlite3"
    repository = _seed(database, code_count=6)

    class SlowBrokenFactory(SQLiteMarketDataReaderFactory):
        """模块级：第一个分片立即失败，其余分片人为放慢。"""

        def __init__(self, path):
            super().__init__(path)
            self.slow_codes = set()

        def create(self):
            reader = super().create()
            original = reader.read_batch
            slow = self.slow_codes

            def slow_or_fail(request):
                if request.codes and request.codes[0] == "sh.600000":
                    raise RuntimeError("first shard fails")
                if request.codes and request.codes[0] in slow:
                    import time
                    time.sleep(3)
                return original(request)

            reader.read_batch = slow_or_fail  # type: ignore[method-assign]
            return reader

    import time
    factory = SlowBrokenFactory(database)
    factory.slow_codes = {"sh.600002", "sh.600004"}
    registry = build_default_registry()
    executor = ScreeningShardExecutor(
        factory,
        registry,
        max_workers=4,
        batch_size=2,
        small_data_serial_threshold=0,
    )
    raw = json.loads(
        (Path(__file__).parents[1] / "config" / "rule_templates" / "system-default.json")
        .read_text(encoding="utf-8"),
    )
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    metadata = repository.get_dataset_metadata("market", TARGET_DAY, QFQ)
    assert metadata is not None
    stocks = repository.get_stocks(TARGET_DAY)
    trading_days = tuple(repository.get_trading_days(date.min, TARGET_DAY))
    data_plan = ScreeningDataPlanner().plan(plan, TARGET_DAY, trading_days)
    codes = tuple(f"sh.{600000 + index:06d}" for index in range(6))

    start = time.perf_counter()
    with pytest.raises(ShardReadError):
        executor.execute(
            PicklableScreeningPlan.from_plan(plan),
            "market",
            TARGET_DAY,
            QFQ,
            metadata,
            stocks,
            codes,
            data_plan,
        )
    elapsed = time.perf_counter() - start
    # 若未取消，慢分片会让失败路径等待约 3 秒；取消后应远小于 3 秒。
    assert elapsed < 2.0, f"pending shards were not cancelled (elapsed={elapsed:.2f}s)"
