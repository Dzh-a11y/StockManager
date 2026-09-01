"""Stable command-line interface for StockManager."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Sequence, TextIO

from stock_manager.domain import (
    AdjustmentMethod,
    ParameterizedScreeningResult,
    ScreeningResult,
)
from stock_manager.providers.baostock_provider import (
    BaostockProvider,
    BaostockProviderError,
)
from stock_manager.rules import load_rules_config
from stock_manager.rules.builtin import build_default_registry
from stock_manager.services import ScreeningService
from stock_manager.services.parameterized_screening_service import (
    ParameterizedScreeningService,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync import (
    CooldownActiveError,
    DataSyncService,
    RetryRequiredError,
    SyncFailedError,
    load_sync_config,
)
from stock_manager.sync.locks import dataset_lock_path, is_file_lock_held
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template
from stock_manager.web.config import WebConfig
from stock_manager.web.httpd import run_web


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("expected an ISO date (YYYY-MM-DD)") from error


def _adjustment(value: str) -> AdjustmentMethod:
    try:
        return AdjustmentMethod(value)
    except ValueError as error:
        choices = ", ".join(item.value for item in AdjustmentMethod)
        raise argparse.ArgumentTypeError(f"adjustment must be one of: {choices}") from error


def _json_value(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (Decimal, Enum)):
        return str(value.value) if isinstance(value, Enum) else str(value)
    return value


def _print_json(value: object, stream: TextIO) -> None:
    print(
        json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True),
        file=stream,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="stock-manager")
    commands = parser.add_subparsers(dest="command", required=True)

    screen = commands.add_parser("screen", help="screen local SQLite data only")
    screen.add_argument("--db", type=Path, required=True)
    screen.add_argument("--rules", type=Path, required=True)
    screen.add_argument("--dataset", default="market")
    screen.add_argument("--date", type=_iso_date, required=True)
    screen.add_argument("--adjustment", type=_adjustment, required=True)
    screen.add_argument("--code", action="append", default=[])
    screen.add_argument("--format", choices=("json", "summary"), default="json")

    screen_template = commands.add_parser(
        "screen-template", help="screen local data with a version-2 template"
    )
    screen_template.add_argument("--db", type=Path, required=True)
    screen_template.add_argument("--template", type=Path, required=True)
    screen_template.add_argument("--dataset", default="market")
    screen_template.add_argument("--date", type=_iso_date, required=True)
    screen_template.add_argument("--adjustment", type=_adjustment, required=True)
    screen_template.add_argument("--code", action="append", default=[])
    screen_template.add_argument(
        "--format", choices=("json", "summary"), default="json"
    )

    sync = commands.add_parser("sync", help="synchronize through DataSyncService")
    sync.add_argument("--db", type=Path, required=True)
    sync.add_argument("--config", type=Path, required=True)
    sync.add_argument("--lock-dir", type=Path, required=True)
    sync.add_argument("--dataset", default="market")
    sync.add_argument("--date", type=_iso_date, required=True)
    sync.add_argument("--adjustment", type=_adjustment, required=True)
    sync.add_argument("--retry", action="store_true")

    # P5-RD-8:DataSync 重构同步控制子命令
    sync_plan = commands.add_parser(
        "sync-plan", help="build a deterministic P5 sync plan (offline)"
    )
    sync_plan.add_argument("--db", type=Path, required=True)
    sync_plan.add_argument("--dataset", default="market")
    sync_plan.add_argument("--adjustment", type=_adjustment, required=True)
    sync_plan.add_argument("--mode", choices=("BOOTSTRAP", "INCREMENTAL", "LEGACY_IMPORT"), default="BOOTSTRAP")
    sync_plan.add_argument("--start", type=_iso_date, required=True)
    sync_plan.add_argument("--end", type=_iso_date, required=True)
    sync_plan.add_argument(
        "--data-types",
        default="stocks,daily_bars,fundamentals,dividends",
        help="comma-separated data types",
    )

    sync_verify = commands.add_parser(
        "sync-verify", help="verify a candidate's staged partitions (offline)"
    )
    sync_verify.add_argument("--db", type=Path, required=True)
    sync_verify.add_argument("--candidate", required=True)
    sync_verify.add_argument("--adjustment", type=_adjustment, required=True)
    sync_verify.add_argument("--start", type=_iso_date, required=True)
    sync_verify.add_argument("--end", type=_iso_date, required=True)

    # P5 流水线一键控制
    sync_start = commands.add_parser(
        "sync-start", help="plan and execute a P5 plan through the facade"
    )
    sync_start.add_argument("--db", type=Path, required=True)
    sync_start.add_argument("--config", type=Path, required=True)
    sync_start.add_argument("--lock-dir", type=Path, required=True)
    sync_start.add_argument("--dataset", default="market")
    sync_start.add_argument("--adjustment", type=_adjustment, required=True)
    sync_start.add_argument("--mode", choices=("BOOTSTRAP", "INCREMENTAL", "LEGACY_IMPORT"), default="BOOTSTRAP")
    sync_start.add_argument("--start", type=_iso_date, required=True)
    sync_start.add_argument("--end", type=_iso_date, required=True)
    sync_start.add_argument(
        "--data-types",
        default="stocks,daily_bars,fundamentals",
        help="comma-separated data types",
    )

    sync_retry = commands.add_parser(
        "sync-retry", help="explicitly retry a failed/interrupted P5 plan"
    )
    sync_retry.add_argument("--db", type=Path, required=True)
    sync_retry.add_argument("--config", type=Path, required=True)
    sync_retry.add_argument("--lock-dir", type=Path, required=True)
    sync_retry.add_argument("--plan-id", required=True)

    sync_status = commands.add_parser(
        "sync-status", help="show P5 plan/task/candidate/active generation state"
    )
    sync_status.add_argument("--db", type=Path, required=True)
    sync_status.add_argument("--plan-id", default=None, help="show one plan (default: newest)")

    sync_clean = commands.add_parser(
        "sync-clean", help="reset stale RUNNING plan/task state (no runner process)"
    )
    sync_clean.add_argument("--db", type=Path, required=True)
    sync_clean.add_argument(
        "--plan-id", default=None,
        help="clean one plan (default: all non-terminal plans)",
    )

    sync_import_legacy = commands.add_parser(
        "sync-import-legacy", help="import legacy shared tables as LEGACY_IMPORT candidate"
    )
    sync_import_legacy.add_argument("--db", type=Path, required=True)
    sync_import_legacy.add_argument("--dataset", default="market")
    sync_import_legacy.add_argument("--adjustment", type=_adjustment, required=True)
    sync_import_legacy.add_argument("--date", type=_iso_date, required=True)
    sync_import_legacy.add_argument("--plan-id", default="plan-legacy")

    db_prepare = commands.add_parser(
        "db-prepare-transfer", help="checkpoint WAL and write a transfer manifest"
    )
    db_prepare.add_argument("--db", type=Path, required=True)
    db_prepare.add_argument("--out", type=Path, required=True)

    db_verify = commands.add_parser(
        "db-verify-transfer", help="verify a transferred database against its manifest"
    )
    db_verify.add_argument("--db", type=Path, required=True)
    db_verify.add_argument("--manifest", type=Path, required=True)

    status = commands.add_parser("status", help="inspect local sync state")
    status.add_argument("--db", type=Path, required=True)
    status.add_argument("--lock-dir", type=Path, required=True)
    status.add_argument("--dataset", default="market")
    status.add_argument("--adjustment", type=_adjustment, required=True)

    smoke = commands.add_parser("smoke", help="run a bounded live Provider check")
    smoke.add_argument("--config", type=Path, required=True)
    smoke.add_argument("--code", required=True)
    smoke.add_argument("--date", type=_iso_date, required=True)
    smoke.add_argument("--adjustment", type=_adjustment, required=True)

    web = commands.add_parser(
        "web", help="serve the offline-first local screening workbench"
    )
    web.add_argument("--db", type=Path, required=True)
    web.add_argument("--system-templates", type=Path, default=Path("config/rule_templates"))
    web.add_argument("--user-templates", type=Path, default=Path("data/user-templates"))
    web.add_argument("--static", type=Path, default=Path("src/stock_manager/web/static"))
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8000)
    web.add_argument("--sync-config", type=Path, default=None)
    web.add_argument("--lock-dir", type=Path, default=None)
    return parser


def _open_existing_repository(database_path: Path) -> SQLiteRepository:
    if not database_path.is_file():
        raise ValueError(f"local SQLite database does not exist: {database_path}")
    return SQLiteRepository(database_path)


def _screen_command(args: argparse.Namespace, stdout: TextIO) -> int:
    repository = _open_existing_repository(args.db)
    service = ScreeningService(repository, load_rules_config(args.rules))
    results = service.screen(
        args.dataset, args.date, args.adjustment, tuple(args.code)
    )
    if args.format == "json":
        _print_json(results, stdout)
    else:
        _print_summary(results, stdout)
    return 0


def _screen_template_command(args: argparse.Namespace, stdout: TextIO) -> int:
    repository = _open_existing_repository(args.db)
    try:
        raw = json.loads(args.template.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to load template from {args.template}") from error
    registry = build_default_registry()
    plan = TemplateCompiler(registry).compile(parse_template(raw))
    results = ParameterizedScreeningService(repository, registry).screen(
        plan, args.dataset, args.date, args.adjustment, tuple(args.code)
    )
    if args.format == "json":
        _print_json(results, stdout)
    else:
        _print_summary(results, stdout)
    return 0


def _print_summary(
    results: Sequence[ScreeningResult | ParameterizedScreeningResult],
    stdout: TextIO,
) -> None:
    passed = sum(result.passed for result in results)
    print(f"screened={len(results)} passed={passed} failed={len(results) - passed}", file=stdout)
    for result in results:
        state = "PASS" if result.passed else "FAIL"
        print(f"{result.code}\t{state}", file=stdout)


def _sync_command(args: argparse.Namespace, stdout: TextIO) -> int:
    repository = SQLiteRepository(args.db)
    config = load_sync_config(args.config)
    provider = BaostockProvider(
        request_interval_seconds=config.minimum_request_interval_seconds
    )
    service = DataSyncService(provider, repository, args.lock_dir, config)
    if config.pipeline_default:
        # P5 默认入口:单日同步 → 执行/补齐当日计划。
        from stock_manager.domain import SyncPlanMode

        print(
            f"pipeline syncing dataset={args.dataset} date={args.date.isoformat()}",
            file=stdout,
        )
        output = service.run_pipeline_plan(
            mode="BOOTSTRAP",
            dataset_id=args.dataset,
            adjustment=args.adjustment,
            target_start=args.date,
            target_end=args.date,
            data_types=("stocks", "daily_bars", "fundamentals"),
        )
        run = service.run_pipeline_execute(output.plan.plan_id)
        _print_json(
            {
                "plan_id": run.plan_id,
                "plan_status": run.plan_status.value,
                "published": run.published,
                "warning": run.warning,
            },
            stdout,
        )
        return 0
    print(
        f"syncing dataset={args.dataset} date={args.date.isoformat()}",
        file=stdout,
    )
    outcome = service.sync(
        args.dataset, args.date, args.adjustment, retry=args.retry
    )
    _print_json(outcome, stdout)
    return 0


def _status_command(args: argparse.Namespace, stdout: TextIO) -> int:
    repository = _open_existing_repository(args.db)
    record = repository.get_latest_sync_record(args.dataset)
    metadata = repository.get_latest_dataset_metadata(
        args.dataset, args.adjustment
    )
    lock_path = None
    lock_held = False
    if record is not None:
        lock_path = dataset_lock_path(
            args.lock_dir, args.dataset, record.trading_day
        )
        lock_held = is_file_lock_held(lock_path)
    _print_json(
        {
            "dataset_id": args.dataset,
            "adjustment": args.adjustment,
            "latest_sync": record,
            "latest_metadata": metadata,
            "lock": {
                "path": None if lock_path is None else str(lock_path),
                "held": lock_held,
            },
        },
        stdout,
    )
    return 0


def _smoke_command(args: argparse.Namespace, stdout: TextIO) -> int:
    with TemporaryDirectory(prefix="stockmanager-baostock-smoke-") as directory:
        root = Path(directory)
        service = DataSyncService(
            BaostockProvider(request_interval_seconds=0),
            SQLiteRepository(root / "smoke.sqlite3"),
            root / "locks",
            load_sync_config(args.config),
        )
        outcome = service.smoke_test_provider(
            args.code, args.date, args.adjustment
        )
    _print_json(outcome, stdout)
    return 0


def _sync_plan_command(args: argparse.Namespace, stdout: TextIO) -> int:
    from stock_manager.sync.planner import DATA_TYPE_ORDER, SyncPlanner

    repository = _open_existing_repository(args.db)
    data_types = tuple(
        t for t in DATA_TYPE_ORDER if t in args.data_types.split(",")
    )
    if not data_types:
        raise ValueError("--data-types must include at least one known type")

    def calendar(start: date, end: date) -> tuple[date, ...]:
        return repository.get_trading_days(start, end)

    def coverage(
        adjustment: AdjustmentMethod, data_type: str
    ) -> tuple[date | None, date | None]:
        return repository.actual_coverage(adjustment, data_type)

    def universe(as_of: date) -> tuple[str, ...]:
        return tuple(stock.code for stock in repository.get_stocks(as_of))

    def now() -> datetime:
        return datetime.now().astimezone()

    planner = SyncPlanner(
        calendar=calendar,
        coverage=coverage,
        universe_codes=universe,
        now=now,
    )
    if args.mode == "BOOTSTRAP":
        output = planner.plan_bootstrap(
            dataset_id=args.dataset,
            adjustment=args.adjustment,
            target_start=args.start,
            target_end=args.end,
            required_data_types=data_types,
        )
    elif args.mode == "INCREMENTAL":
        active = repository.get_active_generation(args.dataset, args.adjustment)
        if active is None:
            raise ValueError("no active generation for INCREMENTAL plan")
        output = planner.plan_incremental(
            dataset_id=args.dataset,
            adjustment=args.adjustment,
            active_generation=active.generation,
            coverage_end=args.start,
            target_end=args.end,
            required_data_types=data_types,
        )
    else:
        output = planner.plan_legacy_import(
            dataset_id=args.dataset,
            adjustment=args.adjustment,
            target_start=args.start,
            target_end=args.end,
            required_data_types=data_types,
        )
    _print_json(
        {
            "plan": output.plan,
            "candidate": output.candidate,
            "task_count": len(output.tasks),
            "tasks": output.tasks,
        },
        stdout,
    )
    return 0


def _sync_verify_command(args: argparse.Namespace, stdout: TextIO) -> int:
    import sqlite3 as _sqlite3

    from stock_manager.sync.verifier import CoverageVerifier

    repository = _open_existing_repository(args.db)
    candidate = repository.get_candidate_generation(args.candidate)
    if candidate is None:
        raise ValueError(f"candidate not found: {args.candidate}")

    def factory() -> _sqlite3.Connection:
        connection = _sqlite3.connect(repository.database_path, timeout=30.0)
        connection.row_factory = _sqlite3.Row
        return connection

    verifier = CoverageVerifier(
        factory,
        trading_days=lambda start, end: repository.get_trading_days(start, end),
        expected_universe_size=lambda day: max(
            1, len(repository.get_stocks(day))
        ),
    )
    tasks = repository.list_sync_tasks(candidate.plan_id)
    outcome = verifier.verify(
        candidate,
        adjustment=args.adjustment,
        target_start=args.start,
        target_end=args.end,
        tasks=tasks,
    )
    _print_json(
        {
            "report": outcome.report,
            "records": outcome.records,
        },
        stdout,
    )
    return 0


def _sync_import_legacy_command(args: argparse.Namespace, stdout: TextIO) -> int:
    import sqlite3 as _sqlite3

    from stock_manager.sync.legacy import LegacyImporter

    repository = _open_existing_repository(args.db)

    def factory() -> _sqlite3.Connection:
        connection = _sqlite3.connect(repository.database_path, timeout=30.0)
        connection.row_factory = _sqlite3.Row
        return connection

    def now() -> datetime:
        return datetime.now().astimezone()

    importer = LegacyImporter(factory, now=now)
    candidate_id = f"cand-legacy-{args.date.isoformat()}"
    candidate = importer.build_candidate(
        dataset_id=args.dataset,
        adjustment=args.adjustment,
        plan_id=args.plan_id,
        candidate_id=candidate_id,
    )
    batches: list[object] = []
    for data_type, adjustment in (
        ("stocks", None),
        ("daily_bars", args.adjustment),
        ("fundamentals", None),
        ("dividends", None),
    ):
        batch = importer.import_partition(
            candidate,
            data_type=data_type,
            partition_key=args.date.isoformat(),
            batch_id=f"batch-{data_type}-{args.date.isoformat()}",
            source="legacy",
            adjustment=adjustment,
        )
        if batch is not None:
            batches.append(batch)
    finished = importer.finish_candidate(candidate)
    _print_json(
        {
            "candidate": finished,
            "batches": batches,
            "partitions": importer.partitions_for(
                finished.candidate_generation_id,
                generation=finished.candidate_generation_id,
            ),
        },
        stdout,
    )
    return 0


def _db_prepare_transfer_command(args: argparse.Namespace, stdout: TextIO) -> int:
    from stock_manager.sync.seed import TransferPreparer

    def now() -> datetime:
        return datetime.now().astimezone()

    preparer = TransferPreparer(now=now, expected_schema_version=1)
    manifest_path = preparer.prepare(args.db, args.out)
    _print_json({"manifest": str(manifest_path)}, stdout)
    return 0


def _db_verify_transfer_command(args: argparse.Namespace, stdout: TextIO) -> int:
    from stock_manager.sync.seed import TransferPreparer

    def now() -> datetime:
        return datetime.now().astimezone()

    preparer = TransferPreparer(now=now, expected_schema_version=1)
    preparer.verify_transfer(args.db, args.manifest)
    _print_json({"verified": True, "database": str(args.db)}, stdout)
    return 0


def _sync_start_command(args: argparse.Namespace, stdout: TextIO) -> int:
    from stock_manager.domain import SyncPlanMode
    from stock_manager.sync.pipeline import PipelineError, RetryCooldownError

    repository = SQLiteRepository(args.db)
    config = load_sync_config(args.config)
    provider = BaostockProvider(
        request_interval_seconds=config.minimum_request_interval_seconds
    )
    service = DataSyncService(provider, repository, args.lock_dir, config)
    if args.mode == "LEGACY_IMPORT":
        # 旧库导入:直接走 LegacyImporter 一次性导入再验证
        from stock_manager.sync.legacy import LegacyImporter

        def now() -> datetime:
            return datetime.now().astimezone()

        import sqlite3 as _sqlite3

        def factory() -> _sqlite3.Connection:
            connection = _sqlite3.connect(repository.database_path, timeout=30.0)
            connection.row_factory = _sqlite3.Row
            return connection

        importer = LegacyImporter(factory, now=now)
        candidate_id = f"cand-legacy-{args.end.isoformat()}"
        candidate = importer.build_candidate(
            dataset_id=args.dataset,
            adjustment=args.adjustment,
            plan_id="plan-legacy",
            candidate_id=candidate_id,
        )
        for data_type, adjustment in (
            ("stocks", None),
            ("daily_bars", args.adjustment),
            ("fundamentals", None),
            ("dividends", None),
        ):
            importer.import_partition(
                candidate,
                data_type=data_type,
                partition_key=args.end.isoformat(),
                batch_id=f"batch-{data_type}-{args.end.isoformat()}",
                source="legacy",
                adjustment=adjustment,
            )
        finished = importer.finish_candidate(candidate)
        _print_json(
            {
                "mode": "LEGACY_IMPORT",
                "candidate": finished,
                "note": (
                    "导入完成;请用 sync-verify 验证后用 committer 发布,"
                    "或等待 Web 工作台引导"
                ),
            },
            stdout,
        )
        return 0
    try:
        output = service.run_pipeline_plan(
            mode=args.mode,
            dataset_id=args.dataset,
            adjustment=args.adjustment,
            target_start=args.start,
            target_end=args.end,
            data_types=tuple(args.data_types.split(",")),
        )
        run = service.run_pipeline_execute(output.plan.plan_id)
    except (PipelineError, RetryCooldownError, ValueError) as error:
        raise ValueError(str(error)) from error
    _print_json(
        {
            "plan_id": run.plan_id,
            "plan_status": run.plan_status.value,
            "published": run.published,
            "report_issues": run.report_issues,
            "warning": run.warning,
            "candidate_status": (
                None if run.candidate is None else run.candidate.status.value
            ),
        },
        stdout,
    )
    return 0


def _sync_retry_command(args: argparse.Namespace, stdout: TextIO) -> int:
    from stock_manager.sync.pipeline import PipelineError, RetryCooldownError

    repository = SQLiteRepository(args.db)
    config = load_sync_config(args.config)
    provider = BaostockProvider(
        request_interval_seconds=config.minimum_request_interval_seconds
    )
    service = DataSyncService(provider, repository, args.lock_dir, config)
    pipeline = service.build_pipeline()
    try:
        run = pipeline.retry(args.plan_id)
    except (PipelineError, RetryCooldownError, ValueError) as error:
        raise ValueError(str(error)) from error
    _print_json(
        {
            "plan_id": run.plan_id,
            "plan_status": run.plan_status.value,
            "published": run.published,
            "warning": run.warning,
        },
        stdout,
    )
    return 0


def _sync_status_command(args: argparse.Namespace, stdout: TextIO) -> int:
    repository = _open_existing_repository(args.db)
    if args.plan_id is not None:
        plans = [repository.get_sync_plan(args.plan_id)]
    else:
        plans = repository.list_sync_plans("market", AdjustmentMethod.QFQ)
    payload: list[dict[str, object]] = []
    for plan in plans:
        if plan is None:
            continue
        tasks = repository.list_sync_tasks(plan.plan_id)
        candidate = repository.get_candidate_generation(
            plan.candidate_generation_id
        )
        payload.append(
            {
                "plan_id": plan.plan_id,
                "mode": plan.mode.value,
                "status": plan.status.value,
                "target": [plan.target_start.isoformat(), plan.target_end.isoformat()],
                "task_counts": _task_counts(tasks),
                "candidate_status": (
                    None if candidate is None else candidate.status.value
                ),
            }
        )
    _print_json(payload, stdout)
    return 0


def _sync_clean_command(args: argparse.Namespace, stdout: TextIO) -> int:
    """手动清理残留的 RUNNING 计划/任务(无 runner 进程时的假状态)。"""
    from datetime import datetime as _datetime
    from zoneinfo import ZoneInfo as _ZoneInfo

    from stock_manager.domain import SyncPlanStatus, SyncTask, SyncTaskStatus

    repository = _open_existing_repository(args.db)
    now = _datetime.now(_ZoneInfo("Asia/Shanghai")).isoformat()
    if args.plan_id is not None:
        plans = [repository.get_sync_plan(args.plan_id)]
    else:
        plans = repository.list_sync_plans("market", AdjustmentMethod.QFQ)

    cleaned_plans = 0
    cleaned_tasks = 0
    for plan in plans:
        if plan is None:
            continue
        if plan.status.value == "RUNNING":
            repository.update_sync_plan_status(
                plan.plan_id, SyncPlanStatus.PLANNED, _datetime.fromisoformat(now)
            )
            cleaned_plans += 1
        for task in repository.tasks_by_status(
            plan.plan_id, (SyncTaskStatus.RUNNING, SyncTaskStatus.INTERRUPTED)
        ):
            repository.update_sync_task_status(
                SyncTask(
                    task_id=task.task_id, plan_id=task.plan_id,
                    sequence_no=task.sequence_no, data_type=task.data_type,
                    partition_key=task.partition_key, codes=task.codes,
                    range_start=task.range_start, range_end=task.range_end,
                    dependencies=task.dependencies,
                    status=SyncTaskStatus.PENDING, attempt_count=task.attempt_count,
                    not_before=None, row_count=None, error_code=None,
                    error_message=None, started_at=None, finished_at=None,
                )
            )
            cleaned_tasks += 1
    _print_json(
        {
            "cleaned_plans": cleaned_plans,
            "cleaned_tasks": cleaned_tasks,
            "note": "RUNNING/INTERRUPTED 已重置为 PLANNED/PENDING,可重新开始。",
        },
        stdout,
    )
    return 0


def _task_counts(tasks: Sequence[object]) -> dict[str, int]:
    from collections import Counter

    counts: Counter[str] = Counter()
    for task in tasks:
        counts[task.status.value] += 1
    return dict(counts)


def _web_command(args: argparse.Namespace, stdout: TextIO) -> int:
    config = WebConfig(
        database_path=args.db,
        system_template_root=args.system_templates,
        user_template_root=args.user_templates,
        static_root=args.static,
        host=args.host,
        port=args.port,
        sync_config_path=args.sync_config,
        lock_directory=args.lock_dir,
    )
    config.validate()
    print(f"serving screening workbench on http://{config.host}:{config.port}", file=stdout)
    run_web(config)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Parse arguments, execute one command, and return a process exit code."""
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "screen":
            return _screen_command(args, stdout)
        if args.command == "screen-template":
            return _screen_template_command(args, stdout)
        if args.command == "sync":
            return _sync_command(args, stdout)
        if args.command == "status":
            return _status_command(args, stdout)
        if args.command == "web":
            return _web_command(args, stdout)
        if args.command == "sync-plan":
            return _sync_plan_command(args, stdout)
        if args.command == "sync-verify":
            return _sync_verify_command(args, stdout)
        if args.command == "sync-import-legacy":
            return _sync_import_legacy_command(args, stdout)
        if args.command == "sync-start":
            return _sync_start_command(args, stdout)
        if args.command == "sync-retry":
            return _sync_retry_command(args, stdout)
        if args.command == "sync-status":
            return _sync_status_command(args, stdout)
        if args.command == "sync-clean":
            return _sync_clean_command(args, stdout)
        if args.command == "db-prepare-transfer":
            return _db_prepare_transfer_command(args, stdout)
        if args.command == "db-verify-transfer":
            return _db_verify_transfer_command(args, stdout)
        return _smoke_command(args, stdout)
    except (
        BaostockProviderError,
        CooldownActiveError,
        OSError,
        RetryRequiredError,
        sqlite3.Error,
        SyncFailedError,
        ValueError,
    ) as error:
        _print_json(
            {"error": type(error).__name__, "message": str(error)}, stderr
        )
        return 1
    except Exception as error:
        _print_json(
            {"error": type(error).__name__, "message": str(error)}, stderr
        )
        return 1


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()  # 无害 no-op（非 frozen）；frozen 下阻止 spawn 重入
    raise SystemExit(main())
