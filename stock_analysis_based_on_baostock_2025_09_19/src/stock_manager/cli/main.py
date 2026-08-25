"""Stable command-line interface for StockManager."""

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
    provider = BaostockProvider(request_interval_seconds=0)
    service = DataSyncService(provider, repository, args.lock_dir, config)
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


if __name__ == "__main__":
    raise SystemExit(main())
