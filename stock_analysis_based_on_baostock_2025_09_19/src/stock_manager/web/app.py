"""Local HTTP application: routes, service wiring and error handling (P3-2/3/4)."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from stock_manager import __version__
from stock_manager.domain import AdjustmentMethod, SyncStatus
from stock_manager.protocols import ProviderProtocol
from stock_manager.rules.builtin import build_default_registry
from stock_manager.rules.registry import RuleRegistry
from stock_manager.services.parameterized_screening_service import (
    ParameterizedScreeningService,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync import (
    DataSyncService,
    load_sync_config,
)
from stock_manager.sync.data_sync_service import SHANGHAI
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.repository import JsonTemplateRepository
from stock_manager.templates.service import TemplateService
from stock_manager.web.catalog import rule_catalog
from stock_manager.web.config import WebConfig
from stock_manager.web.errors import (
    ApiError,
    BadRequestError,
    NotFoundError,
    map_exception,
)
from stock_manager.services.research_backtest_service import ResearchBacktestService
from stock_manager.web.screen import screen_response
from stock_manager.web.serialization import to_jsonable
from stock_manager.web.templates import (
    full_template,
    parse_template_wrapper,
    save_confirmation,
    template_list,
)

Response = tuple[int, str, bytes]


@dataclass(frozen=True, slots=True)
class WebServices:
    repository: SQLiteRepository
    registry: RuleRegistry
    compiler: TemplateCompiler
    template_repository: JsonTemplateRepository
    template_service: TemplateService
    screening_service: ParameterizedScreeningService


_STATIC_ROUTES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
}


class WebApp:
    """Dispatch HTTP requests to the P3 local services."""

    def __init__(
        self,
        config: WebConfig,
        registry: RuleRegistry | None = None,
        provider_factory: Callable[[], ProviderProtocol] | None = None,
        shutdown_handler: Callable[[], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        config.validate()
        self._config = config
        self._provider_factory = provider_factory
        self._shutdown_handler = shutdown_handler
        self._clock = clock or (lambda: datetime.now(SHANGHAI))
        self._sync_progress: dict[str, object] = {"status": "idle"}
        self._screen_progress: dict[str, object] = {"status": "idle"}
        self._research: ResearchBacktestService | None = None
        repository = SQLiteRepository(config.database_path)
        registry = registry if registry is not None else build_default_registry()
        compiler = TemplateCompiler(registry)
        template_repository = JsonTemplateRepository(
            config.system_template_root, config.user_template_root
        )
        self._services = WebServices(
            repository=repository,
            registry=registry,
            compiler=compiler,
            template_repository=template_repository,
            template_service=TemplateService(template_repository, compiler),
            screening_service=ParameterizedScreeningService(repository, registry),
        )
        database_path = getattr(self._services.repository, "database_path", None)
        if isinstance(database_path, Path):
            self._research = ResearchBacktestService(
                self._services.repository,
                self._services.registry,
                database_path=str(database_path),
                template_loader=self._services.template_service,
                compiler=self._services.compiler,
                max_workers=2,
            )
        self._start_backfill_if_needed()

    def _start_backfill_if_needed(self) -> None:
        """Kick off a one-time history backfill when sync is configured.

        A first-run backfill can issue tens of thousands of provider requests, so it
        runs on a daemon thread and reports overall progress through the shared
        sync-progress state instead of blocking the request path. Auto-backfill only
        runs on the real provider path; an injected ``provider_factory`` is a test
        seam and skips it.
        """
        if self._provider_factory is not None:
            return
        if self._config.sync_config_path is None or self._config.lock_directory is None:
            return

        def run_backfill() -> None:
            try:
                sync_config = load_sync_config(self._config.sync_config_path)
                provider = self._make_provider(
                    request_interval_seconds=(
                        sync_config.minimum_request_interval_seconds
                    ),
                    progress_callback=self._on_backfill_batch_progress,
                )
                service = DataSyncService(
                    provider,
                    self._services.repository,
                    self._config.lock_directory,
                    sync_config,
                    progress=self._on_backfill_progress,
                )
                if sync_config.history is not None:
                    message = "自动回补（八年历史覆盖）…"
                    backfill = lambda: service.backfill_on_startup_v2(
                        "market", AdjustmentMethod.QFQ
                    )
                else:
                    message = "自动回补（补一年数据）…"
                    backfill = lambda: service.backfill_on_startup(
                        "market", AdjustmentMethod.QFQ
                    )
                self._sync_progress.update(
                    {
                        "status": "running",
                        "phase": "starting",
                        "dataset_id": "market",
                        "adjustment": "qfq",
                        "message": message,
                    }
                )
                outcome = backfill()
                if outcome is not None:
                    self._sync_progress.update(
                        {"status": "done", "message": outcome.status.value}
                    )
                else:
                    self._sync_progress.update(
                        {"status": "idle", "message": "历史数据已是最新"}
                    )
            except Exception as error:  # noqa: BLE001 - bounded at startup
                self._sync_progress.update(
                    {"status": "error", "message": str(error)}
                )

        threading.Thread(
            target=run_backfill,
            name="stockmanager-startup-backfill",
            daemon=True,
        ).start()

    def route(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        body_bytes: bytes | None,
    ) -> Response:
        try:
            body = self._parse_body(body_bytes) if body_bytes else None
            return self._dispatch(method, path, query, body)
        except ApiError as error:
            return self._error(error)
        except Exception as error:  # noqa: BLE001 - bounded at the API boundary
            return self._error(map_exception(error))

    def _dispatch(
        self,
        method: str,
        path: str,
        query: Mapping[str, list[str]],
        body: object,
    ) -> Response:
        if method == "GET":
            static = self._static_resource(path)
            if static is not None:
                return static
            if path == "/health":
                return self._json(200, {"status": "ok"})
            if path == "/api/version":
                return self._json(200, {"version": __version__})
            if path == "/api/rules":
                return self._json(200, rule_catalog(self._services.registry))
            if path == "/api/templates":
                return self._json(
                    200,
                    template_list(
                        self._services.template_service,
                        self._services.template_repository.is_system,
                    ),
                )
            if path == "/api/sync/progress":
                return self._json(200, self._sync_progress)
            if path == "/api/sync/status":
                return self._json(200, self._sync_status())
            if path == "/api/screen/progress":
                return self._json(200, self._screen_progress)
            if path == "/api/instances":
                return self._json(200, {"instances": self._list_instances()})
            if path == "/api/research/backtests":
                if self._research is None:
                    return self._error(NotFoundError("research service unavailable"))
                runs = self._research.list_runs()
                return self._json(200, {"runs": runs})
            research_match = self._research_run_id_from_path(path)
            if research_match is not None:
                run_id, suffix = research_match
                return self._handle_research_get(run_id, suffix)
            if path == "/api/bars":
                return self._handle_bars(query)
            match = self._template_id_from_path(path)
            if match is not None:
                template_id = match
                template = self._services.template_service.get(template_id)
                return self._json(
                    200,
                    full_template(
                        template,
                        self._services.template_repository.is_system(template_id),
                    ),
                )

        if path == "/api/templates/validate" and method == "POST":
            template = parse_template_wrapper(body)
            plan = self._services.compiler.compile(template)
            return self._json(200, {"valid": True, "plan": to_jsonable(plan)})

        if path == "/api/templates" and method == "POST":
            template = parse_template_wrapper(body)
            created = self._services.template_service.create(template)
            return self._json(
                201, save_confirmation(created.template_id, created.revision)
            )

        if method == "POST" and path == "/api/screen":
            return self._handle_screen(body)

        if method == "POST" and path == "/api/research/backtests":
            return self._handle_research_submit(body)

        research_match = self._research_run_id_from_path(path)
        if research_match is not None and method == "POST":
            run_id, suffix = research_match
            if suffix == "cancel":
                return self._handle_research_cancel(run_id)

        if method == "POST" and path == "/api/shutdown":
            return self._handle_shutdown(body)

        if method == "POST" and path == "/api/instances/kill":
            return self._handle_kill_instance(body)

        match = self._template_id_from_path(path)
        if match is not None:
            template_id = match
            if method == "PUT":
                return self._handle_template_update(template_id, body)
            if method == "DELETE":
                return self._handle_template_delete(template_id, body)

        raise NotFoundError("resource not found")

    @staticmethod
    def _research_run_id_from_path(path: str) -> tuple[str, str | None] | None:
        match = re.fullmatch(
            r"/api/research/backtests/([^/]+)(?:/(equity|orders|provenance|cancel))?",
            path,
        )
        if match is None:
            return None
        return match.group(1), match.group(2)

    def _handle_research_submit(self, body: object) -> Response:
        if self._research is None:
            return self._error(NotFoundError("research service unavailable"))
        data = self._object(body, "body")
        try:
            template_id = str(data["template_id"]).strip()
            template_revision = int(data["template_revision"])
            strategy_spec_id = str(data["strategy_spec_id"]).strip()
            start = date.fromisoformat(str(data["backtest_start"]))
            end = date.fromisoformat(str(data["backtest_end"]))
            initial_cash = Decimal(str(data["initial_cash"]))
            max_positions = int(data.get("max_positions", 20))
        except (KeyError, ValueError, TypeError) as error:
            return self._error(BadRequestError("invalid research request: " + str(error)))
        try:
            run_id = self._research.submit(
                template_id=template_id,
                template_revision=template_revision,
                strategy_spec_id=strategy_spec_id,
                backtest_start=start,
                backtest_end=end,
                initial_cash=initial_cash,
                max_positions=max_positions,
            )
        except Exception as error:
            return self._error(BadRequestError(str(error)))
        return self._json(202, {"run_id": run_id})

    def _handle_research_get(self, run_id: str, suffix: str | None) -> Response:
        if self._research is None:
            return self._error(NotFoundError("research service unavailable"))
        if suffix == "equity":
            points = self._research.equity(run_id)
            return self._json(200, {"run_id": run_id, "points": points, "count": len(points)})
        if suffix == "orders":
            orders = self._research.orders(run_id)
            return self._json(200, {"run_id": run_id, "orders": orders, "count": len(orders)})
        if suffix == "provenance":
            result = self._research.result(run_id)
            if result is None:
                return self._error(NotFoundError(f"no result for run {run_id}"))
            return self._json(200, {"run_id": run_id, "provenance": json.loads(result["provenance_json"])})
        status = self._research.status(run_id)
        if status is None:
            return self._error(NotFoundError(f"run {run_id} not found"))
        payload: dict[str, object] = dict(status)
        result = self._research.result(run_id)
        if result is not None:
            payload["metrics"] = json.loads(result["metrics_json"])
            payload["warnings"] = json.loads(result["warnings_json"] or "[]")
        return self._json(200, payload)

    def _handle_research_cancel(self, run_id: str) -> Response:
        if self._research is None:
            return self._error(NotFoundError("research service unavailable"))
        cancelled = self._research.cancel(run_id)
        return self._json(200, {"run_id": run_id, "cancel_requested": cancelled})

    def _handle_screen(self, body: object) -> Response:
        data = self._object(body, "body")
        unknown = set(data) - {"template", "dataset_id", "trading_day", "adjustment", "codes", "max_workers"}
        if unknown:
            raise BadRequestError(f"unknown field(s): {', '.join(sorted(unknown))}")
        required = {"template", "dataset_id", "trading_day", "adjustment"}
        missing = required - set(data)
        if missing:
            raise BadRequestError(f"missing field(s): {', '.join(sorted(missing))}")
        dataset_id = self._text(data["dataset_id"], "dataset_id")
        trading_day = self._iso_date(data["trading_day"])
        adjustment = self._adjustment(data["adjustment"])
        template = parse_template_wrapper({"template": data["template"]})
        plan = self._services.compiler.compile(template)
        codes = self._codes(data.get("codes"))
        max_workers = self._max_workers_value(data.get("max_workers"))
        self._screen_progress = {
            "status": "running",
            "dataset_id": dataset_id,
            "trading_day": trading_day.isoformat(),
            "adjustment": adjustment.value,
            "phase": "screening",
            "done": 0,
            "total": 0,
            "current_code": None,
            "message": "正在筛选…",
        }
        started = time.perf_counter()
        try:
            results = self._services.screening_service.screen(
                plan,
                dataset_id,
                trading_day,
                adjustment,
                codes,
                progress_callback=self._on_screen_progress,
                max_workers=max_workers,
            )
            self._screen_progress["status"] = "done"
            self._screen_progress["message"] = "筛选完成"
        except Exception:
            self._screen_progress["status"] = "error"
            self._screen_progress["message"] = "screening failed"
            raise
        metadata = self._services.repository.get_dataset_metadata(
            dataset_id, trading_day, adjustment
        )
        if metadata is None:
            raise NotFoundError("local dataset is unavailable")
        elapsed = round(time.perf_counter() - started, 3)
        payload = screen_response(
            dataset_id, trading_day, adjustment, plan, metadata, results
        )
        payload["elapsed_seconds"] = elapsed
        payload["max_workers"] = max_workers
        return self._json(200, payload)

    def _handle_bars(self, query: Mapping[str, list[str]]) -> Response:
        """Return recent local daily bars for one stock (K-line + volume source).

        Query params: ``code`` (required), ``adjustment`` (required),
        ``end`` (optional ISO date, defaults to the latest synced trading day)
        and ``days`` (optional 1..500, defaults to 250). Only reads the local
        SQLite database; never touches a provider.
        """
        code = self._query_text(query, "code")
        adjustment = self._adjustment(self._query_text(query, "adjustment"))
        end = self._query_date(query, "end")
        days = self._query_integer(query, "days", default=250, minimum=1, maximum=500)
        if end is None:
            latest = self._services.repository.get_latest_dataset_metadata(
                "market", adjustment
            )
            if latest is None:
                raise NotFoundError("local dataset is unavailable")
            end = latest.trading_day
        start = end - timedelta(days=max(days * 2, 60))
        bars = self._services.repository.get_daily_bars((code,), start, end, adjustment)
        return self._json(
            200,
            {
                "code": code,
                "adjustment": adjustment.value,
                "end": end.isoformat(),
                "bars": list(bars)[-days:],
            },
        )

    def _on_screen_progress(self, event: dict[str, object]) -> None:
        done = event.get("done")
        total = event.get("total")
        self._screen_progress.update(
            {
                "status": "running",
                "done": done if isinstance(done, int) else 0,
                "total": total if isinstance(total, int) else 0,
                "current_code": event.get("current_code"),
                "phase": event.get("phase") or "screening",
            }
        )

    def _sync_status_label(self, rec, complete: bool, has_bar: bool) -> str:
        """Map a sync record to a per-day status label for the visualization.

        ``complete`` is True when the day's bar universe covers the stock pool
        (fully synced). An explicit RUNNING record renders amber and FAILED red.
        Completeness is checked before SUCCESS so a day falsely marked SUCCESS
        but holding only part of the universe renders amber (partial pull) rather
        than green. A day with no bars is gray; a non-trading day is handled by
        the caller.
        """
        if rec is not None:
            if rec.status is SyncStatus.RUNNING:
                return "running"
            if rec.status is SyncStatus.FAILED:
                return "failed"
        if complete:
            return "synced"
        if has_bar:
            return "incomplete"
        return "missing"

    def _sync_status(self) -> dict[str, object]:
        """Summarize local data coverage for the sync-date visualization.

        Returns the latest synced day, coverage range/counts, the most recent
        30 natural days (per-day status) and 11 older 30-day coverage bands
        (together spanning the 360-day retention window).
        """
        repo = self._services.repository
        qfq = AdjustmentMethod.QFQ
        latest_meta = repo.get_latest_dataset_metadata("market", qfq)
        if latest_meta is None:
            return {
                "latest_synced_trading_day": None,
                "coverage_start": None,
                "coverage_end": None,
                "bars_count": 0,
                "stocks_count": 0,
                "recent_days": [],
                "older_bands": [],
            }
        anchor = latest_meta.trading_day
        start = anchor - timedelta(days=359)
        bar_days = repo.daily_bar_days(start, anchor, qfq)
        cal_days = set(repo.get_trading_days(start, anchor))
        stocks_count = len(repo.get_stocks(anchor))
        # 完整同步的判定:某天 bar 的不同股票数 >= 股票池规模的 0.95。
        # 首次启动只同步了一部分(bar 不足)时,这些天视为"未完全同步"→ 橙色。
        bar_stock_counts = repo.daily_bar_stock_counts(start, anchor, qfq)
        complete_threshold = max(1, int(stocks_count * 0.95))
        complete_days = {
            day for day, n in bar_stock_counts.items() if n >= complete_threshold
        }

        recent: list[dict[str, object]] = []
        for i in range(30):
            day = anchor - timedelta(days=i)
            if day not in cal_days:
                status = "nontrading"
            else:
                # 以 sync_record 的实际状态为准;bar 覆盖不足视为"未完全同步"。
                rec = repo.get_sync_record("market", day)
                status = self._sync_status_label(
                    rec, day in complete_days, day in bar_days
                )
            recent.append({"day": day.isoformat(), "status": status})

        bands: list[dict[str, object]] = []
        band_end = anchor - timedelta(days=30)
        for _ in range(11):
            band_start = band_end - timedelta(days=29)
            seg = [d for d in cal_days if band_start <= d <= band_end]
            coverage = (
                sum(1 for d in seg if d in complete_days) / len(seg) if seg else 0.0
            )
            incomplete = any(d in bar_days and d not in complete_days for d in seg)
            bands.append(
                {
                    "start": band_start.isoformat(),
                    "end": band_end.isoformat(),
                    "coverage": round(coverage, 2),
                    "incomplete": incomplete,
                }
            )
            band_end = band_start - timedelta(days=1)

        return {
            "latest_synced_trading_day": anchor.isoformat(),
            "coverage_start": start.isoformat(),
            "coverage_end": anchor.isoformat(),
            "stocks_count": stocks_count,
            "recent_days": recent,
            "older_bands": bands,
        }

    def _list_instances(self) -> list[dict[str, object]]:
        """Enumerate this host's stock-manager processes (duplicate diagnosis)."""
        items: list[dict[str, object]] = []
        try:
            if os.name == "nt":
                ps = (
                    "Get-CimInstance Win32_Process | "
                    "Where-Object { $_.CommandLine -match 'stock_manager' } | "
                    "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                raw = result.stdout.strip()
                if not raw:
                    return []
                import json as _json

                rows = _json.loads(raw)
                if isinstance(rows, dict):
                    rows = [rows]
                for row in rows:
                    pid = int(row["ProcessId"])
                    items.append(
                        {
                            "pid": pid,
                            "command": row.get("CommandLine", ""),
                            "is_self": pid == os.getpid(),
                        }
                    )
            else:
                result = subprocess.run(
                    ["ps", "-Ao", "pid=,command="], capture_output=True, text=True, check=False
                )
                for line in result.stdout.splitlines():
                    parts = line.strip().split(None, 1)
                    if len(parts) == 2 and "stock_manager" in parts[1]:
                        pid = int(parts[0])
                        items.append(
                            {"pid": pid, "command": parts[1], "is_self": pid == os.getpid()}
                        )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return items

    def _handle_kill_instance(self, body: object) -> Response:
        data = self._object(body, "body")
        unknown = set(data) - {"pid"}
        if unknown:
            raise BadRequestError(f"unknown field(s): {', '.join(sorted(unknown))}")
        pid = data.get("pid")
        if not isinstance(pid, int):
            raise BadRequestError("pid must be an integer")
        if not any(item["pid"] == pid for item in self._list_instances()):
            raise NotFoundError("no matching stock-manager process")
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as error:
            raise BadRequestError(f"failed to kill pid {pid}: {error}")
        return self._json(200, {"killed": pid})

    def _handle_shutdown(self, body: object) -> Response:
        """Stop the local server process after explicit confirmation.

        The default action schedules a SIGTERM to this process with a short
        delay so the HTTP response is flushed before the process exits; the
        frontend never needs a shell to restart the service. The action is
        injectable so tests can observe it without killing the test runner.
        """
        data = self._object(body, "body")
        unknown = set(data) - {"confirm"}
        if unknown:
            raise BadRequestError(f"unknown field(s): {', '.join(sorted(unknown))}")
        confirm = data.get("confirm", False)
        if not isinstance(confirm, bool) or not confirm:
            raise BadRequestError("confirm must be true to shut down the server")
        handler = self._shutdown_handler
        if handler is None:
            def terminate() -> None:
                threading.Timer(0.5, os.kill, args=(os.getpid(), signal.SIGTERM)).start()

            handler = terminate
        handler()
        return self._json(200, {"status": "shutting_down"})

    def _make_provider(
        self,
        *,
        request_interval_seconds: float = 0.0,
        progress_callback: Callable[[dict[str, object]], None] | None = None,
    ) -> ProviderProtocol:
        if self._provider_factory is not None:
            return self._provider_factory()
        from stock_manager.providers.baostock_provider import BaostockProvider

        return BaostockProvider(
            request_interval_seconds=request_interval_seconds,
            progress_callback=progress_callback,
        )

    def _on_backfill_batch_progress(self, event: dict[str, object]) -> None:
        """Update within-batch (per-code) progress from the provider callback."""
        phase = event.get("phase")
        index = event.get("index")
        total = event.get("total")
        code = event.get("current_code")
        self._sync_progress.update(
            {
                "batch_phase": phase,
                "batch_completed": index if isinstance(index, int) else 0,
                "batch_total": total if isinstance(total, int) else 0,
                "current_code": code,
            }
        )

    def _on_backfill_progress(self, event: dict[str, object]) -> None:
        """Update shared progress from the backfill's overall (global) counters."""
        self._sync_progress.update(
            {
                "status": "running",
                "phase": event.get("phase"),
                "completed": event.get("completed", 0),
                "total": event.get("total", 0),
                "current_code": event.get("current_code"),
            }
        )

    def _handle_template_update(self, template_id: str, body: object) -> Response:
        data = self._object(body, "body")
        unknown = set(data) - {"template", "expected_revision"}
        if unknown:
            raise BadRequestError(f"unknown field(s): {', '.join(sorted(unknown))}")
        if "template" not in data or "expected_revision" not in data:
            raise BadRequestError("template and expected_revision are required")
        template = parse_template_wrapper({"template": data["template"]})
        if template.metadata.template_id != template_id:
            raise BadRequestError("template_id does not match the URL resource")
        expected = self._positive_integer(data["expected_revision"], "expected_revision")
        updated = self._services.template_service.update(
            template, expected_revision=expected
        )
        return self._json(200, save_confirmation(updated.template_id, updated.revision))

    def _handle_template_delete(self, template_id: str, body: object) -> Response:
        data = self._object(body, "body")
        expected = data.get("expected_revision")
        if expected is None:
            raise BadRequestError("expected_revision is required")
        revision = self._positive_integer(expected, "expected_revision")
        self._services.template_service.delete(template_id, expected_revision=revision)
        return 204, "", b""

    def _static_resource(self, path: str) -> Response | None:
        if path not in _STATIC_ROUTES:
            return None
        file_name, content_type = _STATIC_ROUTES[path]
        candidate = (self._config.static_root / file_name).resolve()
        if not candidate.is_file():
            raise NotFoundError("static resource not found")
        try:
            content = candidate.read_bytes()
        except OSError as error:
            raise ApiError(500, "INTERNAL", "unable to read static resource") from error
        return 200, content_type, content

    @staticmethod
    def _parse_body(body_bytes: bytes) -> object:
        if len(body_bytes) > 1_000_000:
            raise BadRequestError("request body too large")
        if not body_bytes.strip():
            return {}
        try:
            return json.loads(body_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BadRequestError("request body must be valid JSON") from error

    @staticmethod
    def _object(value: object, field_name: str) -> dict[str, object]:
        if not isinstance(value, dict):
            raise BadRequestError(f"{field_name} must be an object")
        return value

    @staticmethod
    def _text(value: object, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise BadRequestError(f"{field_name} must be a non-empty string")
        return value.strip()

    @staticmethod
    def _iso_date(value: object) -> date:
        if not isinstance(value, str):
            raise BadRequestError("trading_day must be an ISO date string")
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise BadRequestError("trading_day must be an ISO date (YYYY-MM-DD)") from error

    @staticmethod
    def _adjustment(value: object) -> AdjustmentMethod:
        if not isinstance(value, str):
            raise BadRequestError("adjustment must be a string")
        try:
            return AdjustmentMethod(value)
        except ValueError as error:
            choices = ", ".join(item.value for item in AdjustmentMethod)
            raise BadRequestError(f"adjustment must be one of: {choices}") from error

    @staticmethod
    def _positive_integer(value: object, field_name: str) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise BadRequestError(f"{field_name} must be a positive integer")
        return value

    @staticmethod
    def _query_first(query: Mapping[str, list[str]], name: str) -> str | None:
        values = query.get(name)
        if not values:
            return None
        return values[0]

    def _query_text(self, query: Mapping[str, list[str]], name: str) -> str:
        value = self._query_first(query, name)
        if value is None or not value.strip():
            raise BadRequestError(f"{name} is required")
        return value.strip()

    def _query_date(self, query: Mapping[str, list[str]], name: str) -> date | None:
        value = self._query_first(query, name)
        if value is None or not value.strip():
            return None
        try:
            return date.fromisoformat(value)
        except ValueError as error:
            raise BadRequestError(f"{name} must be an ISO date (YYYY-MM-DD)") from error

    def _query_integer(
        self,
        query: Mapping[str, list[str]],
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        value = self._query_first(query, name)
        if value is None or not value.strip():
            return default
        try:
            parsed = int(value)
        except ValueError as error:
            raise BadRequestError(f"{name} must be an integer") from error
        if isinstance(parsed, bool) or not (minimum <= parsed <= maximum):
            raise BadRequestError(f"{name} must be between {minimum} and {maximum}")
        return parsed

    @staticmethod
    def _max_workers_value(value: object) -> int:
        """Parse max_workers (1..16); None defaults to 4."""
        if value is None:
            return 4
        if not isinstance(value, int) or isinstance(value, bool):
            raise BadRequestError("max_workers must be an integer")
        if not 1 <= value <= 16:
            raise BadRequestError("max_workers must be between 1 and 16")
        return value

    @staticmethod
    def _codes(value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise BadRequestError("codes must be an array of strings")
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())

    @staticmethod
    def _json(status: int, payload: object) -> Response:
        body = json.dumps(to_jsonable(payload), ensure_ascii=False).encode("utf-8")
        return status, "application/json; charset=utf-8", body

    @staticmethod
    def _error(error: ApiError) -> Response:
        return (
            error.status,
            "application/json; charset=utf-8",
            json.dumps(error.payload(), ensure_ascii=False).encode("utf-8"),
        )

    @staticmethod
    def _template_id_from_path(path: str) -> str | None:
        prefix = "/api/templates/"
        if not path.startswith(prefix):
            return None
        candidate = path[len(prefix):]
        if not candidate or "/" in candidate:
            return None
        return candidate
