"""Reference-specific planning and checks on the shared P5 sync pipeline.

The catalogue task discovers inputs; its persisted staging rows expand into
independently checkpointed index tasks. No stock universe or stock coverage
threshold participates in reference sync.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod, CoverageVerification, DepositRate, IndexIdentity, IndexReturnVersion,
    IssueType, Repairability, SyncPlan, SyncTask, SyncTaskStatus,
    VerificationIssue, VerificationStatus,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync.planner import PlanInput, PlannedOutput, SyncPlanner
from stock_manager.sync.verifier import CoverageVerifier

DATASET = "capm"
DATA_TYPES = ("index_catalog", "deposit_rates", "index_daily_bars")
ACCEPTANCE_POLICY = "source_available_v1"


class CapmPlanner(SyncPlanner):
    def __init__(self, repository: SQLiteRepository, now: Callable[[], datetime]) -> None:
        super().__init__(calendar=repository.get_trading_days,
                         coverage=lambda adjustment, kind: (None, None),
                         universe_codes=lambda day: (), now=now)
        self.repository = repository

    def _emit(self, input_: PlanInput, *, parent_generation: str | None) -> PlannedOutput:
        active = self.repository.get_active_generation(DATASET, AdjustmentMethod.UNADJUSTED)
        return super()._emit(input_, parent_generation=active.generation if active else None)

    def _window_tasks(self, input_: PlanInput) -> tuple[SyncTask, ...]:
        if not self._calendar(input_.target_start, input_.target_end):
            raise ValueError("reference target contains no trading days")
        return tuple(self._make_task(
            input_, seq, kind, f"{input_.target_start}..{input_.target_end}", (),
            input_.target_start, input_.target_end,
        ) for seq, kind in enumerate(DATA_TYPES[:2]))


class ReferenceTasks:
    def __init__(self, repository: SQLiteRepository,
                 connect: Callable[[], sqlite3.Connection]) -> None:
        self.repository = repository
        self.connect = connect

    def cached(self, task: SyncTask) -> Sequence[object] | None:
        """A local repair never re-fetches the same successful metadata snapshot."""
        if task.data_type not in ("index_catalog", "deposit_rates"):
            return None
        active = self.repository.get_active_generation(DATASET, AdjustmentMethod.UNADJUSTED)
        if active is None:
            return None
        with closing(self.connect()) as connection:
            plan = connection.execute(
                "SELECT target_end FROM sync_plans WHERE candidate_generation_id=?", (active.generation,),
            ).fetchone()
            if plan is None or plan[0] < task.range_end.isoformat():
                return None
            if task.data_type == "index_catalog":
                rows = connection.execute(
                    """SELECT i.* FROM index_catalog i WHERE EXISTS (SELECT 1
                       FROM generation_partitions g WHERE g.generation=?
                       AND g.data_type='index_catalog' AND g.batch_id=i.batch_id) ORDER BY i.index_id""",
                    (active.generation,),
                ).fetchall()
                if not rows:
                    return None
                return tuple(IndexIdentity(r["index_id"], r["provider_code"], r["name"],
                    r["category"], IndexReturnVersion(r["return_version"]), r["source"]) for r in rows)
            rows = connection.execute(
                """SELECT r.* FROM deposit_rates r WHERE r.effective_on<=? AND EXISTS (
                   SELECT 1 FROM generation_partitions g WHERE g.generation=?
                   AND g.data_type='deposit_rates' AND g.batch_id=r.batch_id)
                   ORDER BY r.term, r.effective_on""",
                (task.range_end.isoformat(), active.generation),
            ).fetchall()
            if not any(r["term"] == "1_year" and r["effective_on"] <= task.range_start.isoformat() for r in rows):
                return None
            return tuple(DepositRate(r["term"], date.fromisoformat(r["effective_on"]),
                                     Decimal(r["annual_rate"]), r["source"]) for r in rows)

    def indexes(self, task: SyncTask) -> tuple[IndexIdentity, ...]:
        with closing(self.connect()) as connection:
            rows = connection.execute(
                """SELECT s.* FROM index_catalog_staging s JOIN ingest_batches b
                   ON b.batch_id=s.batch_id JOIN sync_plans p
                   ON p.candidate_generation_id=b.candidate_generation_id
                   WHERE p.plan_id=? ORDER BY s.index_id""", (task.plan_id,),
            ).fetchall()
        return tuple(IndexIdentity(r["index_id"], r["provider_code"], r["name"],
                                   r["category"], IndexReturnVersion(r["return_version"]),
                                   r["source"]) for r in rows
                     if not task.codes or r["index_id"] in task.codes)

    def expand(self, plan: SyncPlan) -> Sequence[SyncTask]:
        tasks = tuple(self.repository.list_sync_tasks(plan.plan_id))
        catalog = tasks[0]
        if catalog.status is not SyncTaskStatus.SUCCESS:
            return ()
        indexes = self.indexes(catalog)
        if not indexes:
            raise ValueError("provider returned an empty index catalogue")
        # Persist all task definitions together: a crash cannot leave a partially
        # expanded plan that might later be published as complete.
        with closing(self.connect()) as connection:
            expanded = connection.execute(
                "SELECT 1 FROM sync_tasks WHERE plan_id=? AND data_type='index_daily_bars'",
                (plan.plan_id,),
            ).fetchone()
            if expanded:
                return ()
            expected = tuple(self.repository.get_trading_days(plan.target_start, plan.target_end))
            published: dict[str, set[date]] = {}
            for row in connection.execute(
                """SELECT b.index_id, b.trading_day FROM index_daily_bars b
                   WHERE b.trading_day BETWEEN ? AND ? AND EXISTS (
                       SELECT 1 FROM generation_partitions g JOIN active_generations a
                       ON a.generation=g.generation WHERE a.dataset_id='capm'
                       AND a.adjustment='unadjusted' AND g.data_type='index_daily_bars'
                       AND g.batch_id=b.batch_id)""",
                (plan.target_start.isoformat(), plan.target_end.isoformat()),
            ):
                published.setdefault(row["index_id"], set()).add(date.fromisoformat(row["trading_day"]))
            additions: list[SyncTask] = []
            for item in indexes:
                present = published.get(item.index_id, set())
                # Explicit successful empty responses are coverage observations,
                # not fabricated prices. Do not keep querying unsupported history.
                empty_ranges = self.repository.capm_unavailable_ranges(item.index_id)
                missing = [day for day in expected if day not in present and not any(
                    row[0] <= day.isoformat() <= row[1] for row in empty_ranges)]
                groups: list[list[date]] = []
                positions = {day: pos for pos, day in enumerate(expected)}
                for day in missing:
                    if groups and positions[day] == positions[groups[-1][-1]] + 1:
                        groups[-1].append(day)
                    else:
                        groups.append([day])
                for group in groups:
                    seq = len(additions) + 2
                    additions.append(replace(catalog, task_id=f"{plan.plan_id}:{seq:05d}",
                        sequence_no=seq, data_type="index_daily_bars",
                        partition_key=f"{group[0]}..{group[-1]}", codes=(item.index_id,),
                        range_start=group[0], range_end=group[-1], dependencies=(catalog.task_id,),
                        status=SyncTaskStatus.PENDING, attempt_count=0, row_count=None,
                        started_at=None, finished_at=None, error_code=None, error_message=None))
            if not additions:
                additions.append(replace(catalog, task_id=f"{plan.plan_id}:00002", sequence_no=2,
                    data_type="index_daily_bars", codes=(), status=SyncTaskStatus.PENDING,
                    attempt_count=0, row_count=None, started_at=None, finished_at=None))
            self.repository.append_sync_tasks(plan.plan_id, additions)
        return tuple(additions)


class CapmVerifier(CoverageVerifier):
    """Accept valid source responses, recording gaps separately from quality errors."""

    def _verify_partition(
        self, connection: sqlite3.Connection, candidate_id: str, write_revision: int,
        manifest_sha256: str, task: SyncTask, adjustment: AdjustmentMethod,
        target_start: date, target_end: date, evidence_partition_key: str,
        issues: list[VerificationIssue], records: list[CoverageVerification],
    ) -> None:
        if task.data_type not in DATA_TYPES:
            raise ValueError(f"unsupported reference task: {task.data_type}")
        rows = connection.execute(
            f"""SELECT s.* FROM {task.data_type}_staging s JOIN ingest_batches b
                ON b.batch_id=s.batch_id WHERE b.candidate_generation_id=?
                AND b.data_type=? AND b.partition_key=? AND b.codes=?""",
            (candidate_id, task.data_type, task.partition_key, ",".join(task.codes)),
        ).fetchall()
        missing: list[str] = []
        batches = connection.execute(
            """SELECT row_count FROM ingest_batches WHERE candidate_generation_id=?
               AND data_type=? AND partition_key=? AND codes=?""",
            (candidate_id, task.data_type, task.partition_key, ",".join(task.codes)),
        ).fetchall()
        invalid = int(task.status is not SyncTaskStatus.SUCCESS or len(batches) != 1
                      or sum(row["row_count"] for row in batches) != len(rows))
        duplicates = 0
        distinct = len(rows)
        blocking = bool(invalid)
        details: dict[str, object] = {}
        expected = len(rows)
        if task.data_type == "index_catalog":
            if not rows:
                missing.append("empty index catalogue")
            invalid += sum(r["return_version"] not in {v.value for v in IndexReturnVersion}
                           or not r["index_id"] or not r["provider_code"] for r in rows)
            blocking = bool(missing or invalid)
        elif task.data_type == "deposit_rates":
            if not any(r["term"] == "1_year" and r["effective_on"] <= target_start.isoformat() for r in rows):
                missing.append("no effective one-year rate at window start")
            for row in rows:
                invalid += not _valid_decimal(row["annual_rate"], positive=False)
            blocking = bool(missing or invalid)
        else:
            days = tuple(sorted(set(self._trading_days(task.range_start, task.range_end))))
            expected = len(days) if task.codes else 0
            actual_days: set[date] = set()
            identity = None
            if task.codes:
                identity = connection.execute(
                    """SELECT s.* FROM index_catalog_staging s JOIN ingest_batches b
                       ON b.batch_id=s.batch_id WHERE b.candidate_generation_id=? AND s.index_id=?""",
                    (candidate_id, task.codes[0]),
                ).fetchone()
                invalid += int(len(task.codes) != 1 or identity is None)
                details["index_id"] = task.codes[0]
                if identity is not None:
                    details.update(provider_code=identity["provider_code"], index_name=identity["name"])
            for row in rows:
                try:
                    day = date.fromisoformat(row["trading_day"])
                except (TypeError, ValueError):
                    invalid += 1
                    continue
                actual_days.add(day)
                invalid += (not _valid_decimal(row["close"], positive=True)
                            or row["index_id"] not in task.codes or identity is None
                            or row["return_version"] != identity["return_version"] or day not in days)
            distinct = len({(r["index_id"], r["trading_day"]) for r in rows})
            duplicates = len(rows) - distinct
            missing = [str(day) for day in days if task.codes and day not in actual_days]
            blocking = bool(invalid or duplicates)
            # A missing incremental slice is allowed if the default benchmark
            # already has prices in this plan's active, published history.
            if task.codes == ("hs300.price",) and not rows:
                parent_price = connection.execute(
                    """SELECT 1 FROM index_daily_bars p JOIN generation_partitions g
                       ON g.batch_id=p.batch_id JOIN active_generations a ON a.generation=g.generation
                       WHERE a.dataset_id='capm' AND a.adjustment='unadjusted'
                       AND g.data_type='index_daily_bars' AND p.index_id='hs300.price'
                       AND p.trading_day BETWEEN ? AND ? LIMIT 1""",
                    (target_start.isoformat(), target_end.isoformat()),
                ).fetchone()
                if parent_price is None:
                    blocking = True
                    details["validation_errors"] = ["default benchmark hs300.price has no prices in target range"]
            details.update(codes=task.codes, acceptance_policy=ACCEPTANCE_POLICY)
            if missing and not blocking:
                details["unavailable_ranges"] = _source_gap_ranges(days, set(missing))
                details["availability"] = "SOURCE_GAPS" if rows else "NO_SOURCE_DATA"
            else:
                details["unavailable_ranges"] = []
        if blocking:
            issues.append(VerificationIssue(
                f"{candidate_id}:{task.task_id}", candidate_id, task.data_type,
                evidence_partition_key, task.range_start, task.codes,
                IssueType.INVALID if invalid else IssueType.DUPLICATE if duplicates else IssueType.MISSING,
                expected, len(rows), Repairability.REFETCH,
                f"missing={missing[:10]}, invalid={invalid}, duplicates={duplicates}; "
                f"{details.get('validation_errors', [])}",
            ))
        status = (VerificationStatus.INCOMPLETE if blocking else
                  VerificationStatus.ACCEPTED_WITH_GAPS if missing else VerificationStatus.COMPLETE)
        ratio = min(Decimal(distinct) / Decimal(expected), Decimal(1)) if expected else Decimal(1)
        records.append(CoverageVerification(
            candidate_id, task.data_type, evidence_partition_key, expected, len(rows),
            distinct, duplicates, invalid, ratio, tuple(missing), status,
            write_revision, manifest_sha256, datetime.now(ZoneInfo("Asia/Shanghai")), json.dumps(details),
        ))


def _source_gap_ranges(days: Sequence[date], missing: set[str]) -> list[list[str]]:
    """Compact missing trading-day runs without merging across a present bar."""
    ranges: list[list[str]] = []
    in_gap = False
    for day in days:
        value = day.isoformat()
        if value not in missing:
            in_gap = False
        elif in_gap:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
            in_gap = True
    return ranges


def _valid_decimal(value: str, *, positive: bool) -> bool:
    try:
        number = Decimal(value)
        return number.is_finite() and (number > 0 if positive else number >= 0)
    except (InvalidOperation, TypeError, ValueError):
        return False
