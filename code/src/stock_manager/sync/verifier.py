"""Coverage verification against staged candidate data (P5-RD-5).

The verifier reads the *actual staged rows* of a candidate (never trusts
provider counts, checkpoints or seed manifests), checks completeness per data
type/partition, and emits a machine-consumable VerificationReport. It
distinguishes NEEDS_REPAIR (business data missing/invalid, repairable by
refetch), VERIFICATION_FAILED (the verification itself errored) and REJECTED
(seed SHA / SQLite structure / schema untrustworthy). It never modifies the
active generation.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CoverageVerification as CoverageVerificationRecord,
    IssueType,
    Repairability,
    SyncTask,
    VerificationIssue,
    VerificationReport,
    VerificationStatus,
)

STAGING_TABLE = {
    "stocks": "stocks_staging",
    "daily_bars": "daily_bars_staging",
    "fundamentals": "fundamentals_staging",
    "dividends": "dividends_staging",
}

#: Daily-bar completeness threshold: a trading day is complete only when the
#: distinct staged bar codes cover at least 95% of the expected stock pool.
DAILY_BAR_COMPLETE_RATIO = Decimal("0.95")


class VerificationError(RuntimeError):
    """The verification run itself failed (VERIFICATION_FAILED)."""


class VerificationRejectedError(RuntimeError):
    """Input is untrustworthy (REJECTED): SHA, SQLite structure or schema."""


@dataclass(frozen=True, slots=True)
class VerificationOutcome:
    """Report plus per-partition evidence records for the committer."""

    report: VerificationReport
    records: tuple[CoverageVerificationRecord, ...]


class CoverageVerifier:
    """Reads staged candidate rows and produces structured evidence."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        trading_days: Callable[[date, date], Sequence[date]],
        expected_universe_size: Callable[[date], int],
        stock_lifecycles: (
            Callable[[date], Mapping[str, tuple[date | None, date | None]]]
            | None
        ) = None,
    ) -> None:
        self._connection_factory = connection_factory
        self._trading_days = trading_days
        self._expected_universe_size = expected_universe_size
        self._stock_lifecycles = stock_lifecycles

    def verify(
        self,
        candidate: CandidateGeneration,
        *,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
        tasks: Sequence[SyncTask],
    ) -> VerificationOutcome:
        """Verify all staged partitions referenced by the given tasks.

        Each task maps to one data type/partition; the verifier evaluates
        every partition present in ``tasks`` and returns a report plus
        per-partition evidence records. Any SQLite-level failure during
        reading raises ``VerificationError`` (VERIFICATION_FAILED), never a
        fake COMPLETE.
        """
        issues: list[VerificationIssue] = []
        records: list[CoverageVerificationRecord] = []
        candidate_id = candidate.candidate_generation_id
        write_revision = candidate.write_revision
        partition_counts = Counter(
            (task.data_type, task.partition_key) for task in tasks
        )
        try:
            with closing(self._connection_factory()) as connection:
                manifest_sha256 = self._candidate_manifest(connection, candidate_id)
                for task in tasks:
                    key = (task.data_type, task.partition_key)
                    evidence_partition_key = (
                        task.partition_key
                        if partition_counts[key] == 1
                        else f"{task.partition_key}:{task.sequence_no:05d}"
                    )
                    self._verify_partition(
                        connection,
                        candidate_id,
                        write_revision,
                        manifest_sha256,
                        task,
                        adjustment,
                        target_start,
                        target_end,
                        evidence_partition_key,
                        issues,
                        records,
                    )
        except sqlite3.Error as error:
            raise VerificationError(
                f"verification failed reading candidate {candidate_id}: {error}"
            ) from error
        return VerificationOutcome(
            VerificationReport(candidate_id, tuple(issues), _now()),
            tuple(records),
        )

    # -- per-partition checks -------------------------------------------------

    @staticmethod
    def _candidate_manifest(
        connection: sqlite3.Connection, candidate_id: str
    ) -> str:
        """Deterministic digest of the candidate's batch set (P5 section 7.6)."""
        import hashlib

        digest = hashlib.sha256()
        rows = connection.execute(
            """SELECT batch_id, data_type, partition_key, row_count, batch_sha256
               FROM ingest_batches
               WHERE candidate_generation_id = ?
               ORDER BY data_type, partition_key, batch_id""",
            (candidate_id,),
        ).fetchall()
        for row in rows:
            digest.update(
                "|".join(
                    (
                        row["batch_id"],
                        row["data_type"],
                        row["partition_key"],
                        str(row["row_count"]),
                        row["batch_sha256"],
                    )
                ).encode("utf-8")
            )
        return digest.hexdigest()

    def _verify_partition(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        write_revision: int,
        manifest_sha256: str,
        task: SyncTask,
        adjustment: AdjustmentMethod,
        target_start: date,
        target_end: date,
        evidence_partition_key: str,
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        table = STAGING_TABLE.get(task.data_type)
        if table is None:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:{task.data_type}:{task.partition_key}:type",
                    candidate_generation_id=candidate_id,
                    data_type=task.data_type,
                    partition_key=evidence_partition_key,
                    trading_day=_partition_date(task.partition_key),
                    codes=tuple(sorted(task.codes)),
                    issue_type=IssueType.INVALID,
                    expected_count=0,
                    actual_count=0,
                    repairability=Repairability.MANUAL,
                    details=f"unsupported data type {task.data_type}",
                )
            )
            return
        day = _partition_date(task.partition_key)
        if task.data_type == "daily_bars":
            self._verify_daily_bars(
                connection, candidate_id, write_revision, manifest_sha256,
                task, adjustment, day, evidence_partition_key, issues, records,
            )
        elif task.data_type == "stocks":
            self._verify_stocks(
                connection, candidate_id, write_revision, manifest_sha256,
                task, day, evidence_partition_key, issues, records,
            )
        elif task.data_type == "fundamentals":
            self._verify_fundamentals(
                connection, candidate_id, write_revision, manifest_sha256,
                task, day, evidence_partition_key, issues, records,
            )
        elif task.data_type == "dividends":
            self._verify_dividends(
                connection, candidate_id, write_revision, manifest_sha256,
                task, day, evidence_partition_key, issues, records,
            )
        else:  # pragma: no cover - guarded above
            raise AssertionError("unreachable")

    def _verify_daily_bars(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        write_revision: int,
        manifest_sha256: str,
        task: SyncTask,
        adjustment: AdjustmentMethod,
        day: date,
        evidence_partition_key: str,
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        codes = tuple(sorted(task.codes))
        placeholders = ",".join("?" for _ in codes) if codes else "''"
        trading_days = tuple(
            sorted(self._trading_days(task.range_start, task.range_end))
        )
        lifecycles = (
            {}
            if self._stock_lifecycles is None
            else self._stock_lifecycles(task.range_end)
        )
        expected_days_by_code: dict[str, set[str]] = {}
        for code in codes:
            listed_on, delisted_on = lifecycles.get(code, (None, None))
            expected_days_by_code[code] = {
                trading_day.isoformat()
                for trading_day in trading_days
                if (listed_on is None or trading_day >= listed_on)
                and (delisted_on is None or trading_day <= delisted_on)
            }
        expected_days_by_code = {
            code: days for code, days in expected_days_by_code.items() if days
        }
        expected_codes = set(expected_days_by_code)
        rows = connection.execute(
            f"""SELECT code, trading_day, open, high, low, close, volume
                FROM daily_bars_staging
                WHERE batch_id IN (
                    SELECT batch_id FROM ingest_batches
                    WHERE candidate_generation_id = ?
                ) AND adjustment = ? AND trading_day BETWEEN ? AND ?
                AND code IN ({placeholders})""",
            (
                candidate_id,
                adjustment.value,
                task.range_start.isoformat(),
                task.range_end.isoformat(),
                *codes,
            ),
        ).fetchall()
        day_counts: dict[str, set[str]] = {}
        row_keys: Counter[tuple[str, str]] = Counter()
        row_values: dict[tuple[str, str], set[tuple[object, ...]]] = {}
        invalid_rows = 0
        for row in rows:
            day_counts.setdefault(row["code"], set()).add(row["trading_day"])
            row_key = (row["code"], row["trading_day"])
            row_keys[row_key] += 1
            row_values.setdefault(row_key, set()).add(
                (
                    row["open"], row["high"], row["low"], row["close"],
                    row["volume"],
                )
            )
            if not _valid_bar(row):
                invalid_rows += 1
        complete_codes = {
            code
            for code, expected_days in expected_days_by_code.items()
            if len(day_counts.get(code, set()) & expected_days)
            >= _coverage_threshold(len(expected_days))
        }
        undercovered_missing: set[str] = set()
        for trading_day in trading_days:
            day_text = trading_day.isoformat()
            expected_on_day = {
                code
                for code, expected_days in expected_days_by_code.items()
                if day_text in expected_days
            }
            if not expected_on_day:
                continue
            present_on_day = {
                code for code in expected_on_day if day_text in day_counts.get(code, set())
            }
            if _ratio(len(present_on_day), len(expected_on_day)) < DAILY_BAR_COMPLETE_RATIO:
                undercovered_missing.update(expected_on_day - present_on_day)
        present_codes = set(day_counts) & expected_codes
        expected_pairs = sum(len(days) for days in expected_days_by_code.values())
        actual_pairs = sum(
            1
            for code, expected_days in expected_days_by_code.items()
            for day_text in expected_days
            if (code, day_text) in row_keys
        )
        duplicates = sum(
            max(0, len(values) - 1) for values in row_values.values()
        )
        invalid = invalid_rows
        missing = tuple(sorted((expected_codes - complete_codes) | undercovered_missing))
        ratio = _ratio(actual_pairs, expected_pairs)
        if missing:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:daily_bars:{task.partition_key}:coverage",
                    candidate_generation_id=candidate_id,
                    data_type="daily_bars",
                    partition_key=evidence_partition_key,
                    trading_day=_partition_date(task.partition_key),
                    codes=missing,
                    issue_type=IssueType.MISSING,
                    expected_count=expected_pairs,
                    actual_count=actual_pairs,
                    repairability=Repairability.REFETCH,
                    details=(
                        f"coverage {ratio:.2%} below {DAILY_BAR_COMPLETE_RATIO:.0%}; "
                        f"missing {len(missing)} of {len(expected_codes)} codes over "
                        f"{task.range_start.isoformat()}..{task.range_end.isoformat()}"
                    ),
                )
            )
        if invalid > 0:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:daily_bars:{task.partition_key}:invalid",
                    candidate_generation_id=candidate_id,
                    data_type="daily_bars",
                    partition_key=evidence_partition_key,
                    trading_day=_partition_date(task.partition_key),
                    codes=tuple(sorted(present_codes)),
                    issue_type=IssueType.INVALID,
                    expected_count=expected_pairs,
                    actual_count=actual_pairs,
                    repairability=Repairability.REFETCH,
                    details=f"{invalid} invalid bar rows in the batch range",
                )
            )
        if duplicates > 0:
            issues.append(
                VerificationIssue(
                    issue_id=(
                        f"{candidate_id}:daily_bars:"
                        f"{evidence_partition_key}:duplicate"
                    ),
                    candidate_generation_id=candidate_id,
                    data_type="daily_bars",
                    partition_key=evidence_partition_key,
                    trading_day=day,
                    codes=tuple(sorted(present_codes)),
                    issue_type=IssueType.DUPLICATE,
                    expected_count=expected_pairs,
                    actual_count=actual_pairs,
                    repairability=Repairability.REBUILD,
                    details=f"{duplicates} duplicate code/day rows across batches",
                )
            )
        status = (
            VerificationStatus.COMPLETE
            if not missing and duplicates == 0 and invalid == 0
            else VerificationStatus.INCOMPLETE
        )
        records.append(
            CoverageVerificationRecord(
                candidate_generation_id=candidate_id,
                data_type="daily_bars",
                partition_key=evidence_partition_key,
                expected_count=expected_pairs,
                actual_count=actual_pairs,
                distinct_count=len(present_codes),
                duplicate_count=duplicates,
                invalid_count=invalid,
                coverage_ratio=ratio,
                missing_items=missing,
                status=status,
                verified_revision=write_revision,
                manifest_sha256=manifest_sha256,
                verified_at=_now(),
                details_json="{}",
            )
        )

    def _verify_stocks(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        write_revision: int,
        manifest_sha256: str,
        task: SyncTask,
        day: date,
        evidence_partition_key: str,
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        rows = connection.execute(
            """SELECT code FROM stocks_staging
               WHERE batch_id IN (
                   SELECT batch_id FROM ingest_batches
                   WHERE candidate_generation_id = ?
               ) AND as_of = ?""",
            (candidate_id, day.isoformat()),
        ).fetchall()
        actual = len(rows)
        expected = max(1, self._expected_universe_size(day))
        present = {row["code"] for row in rows}
        missing = tuple(sorted(set(task.codes) - present))
        ratio = _ratio(actual, expected)
        if ratio < DAILY_BAR_COMPLETE_RATIO:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:stocks:{day.isoformat()}",
                    candidate_generation_id=candidate_id,
                    data_type="stocks",
                    partition_key=evidence_partition_key,
                    trading_day=day,
                    codes=missing,
                    issue_type=IssueType.MISSING,
                    expected_count=expected,
                    actual_count=actual,
                    repairability=Repairability.REFETCH,
                    details=(
                        f"stock snapshot coverage {ratio:.2%} below "
                        f"{DAILY_BAR_COMPLETE_RATIO:.0%}"
                    ),
                )
            )
        status = (
            VerificationStatus.COMPLETE
            if ratio >= DAILY_BAR_COMPLETE_RATIO
            else VerificationStatus.INCOMPLETE
        )
        records.append(
            CoverageVerificationRecord(
                candidate_generation_id=candidate_id,
                data_type="stocks",
                partition_key=evidence_partition_key,
                expected_count=expected,
                actual_count=actual,
                distinct_count=actual,
                duplicate_count=0,
                invalid_count=0,
                coverage_ratio=ratio,
                missing_items=missing,
                status=status,
                verified_revision=write_revision,
                manifest_sha256=manifest_sha256,
                verified_at=_now(),
                details_json="{}",
            )
        )

    def _verify_fundamentals(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        write_revision: int,
        manifest_sha256: str,
        task: SyncTask,
        day: date,
        evidence_partition_key: str,
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        codes = tuple(sorted(task.codes))
        placeholders = ",".join("?" for _ in codes) if codes else "''"
        rows = connection.execute(
            f"""SELECT code, report_date, published_on FROM fundamentals_staging
               WHERE batch_id IN (
                   SELECT batch_id FROM ingest_batches
                   WHERE candidate_generation_id = ? AND data_type = ?
                     AND partition_key = ?
               ) AND code IN ({placeholders})""",
            (candidate_id, "fundamentals", task.partition_key, *codes),
        ).fetchall()
        # PIT 约束:published_on > day 的记录不得出现在该日可见集合。
        violated = tuple(
            sorted(
                {
                    row["code"]
                    for row in rows
                    if row["published_on"] > day.isoformat()
                }
            )
        )
        if violated:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:fundamentals:{day.isoformat()}:pit",
                    candidate_generation_id=candidate_id,
                    data_type="fundamentals",
                    partition_key=evidence_partition_key,
                    trading_day=day,
                    codes=violated,
                    issue_type=IssueType.PIT_VIOLATION,
                    expected_count=0,
                    actual_count=len(violated),
                    repairability=Repairability.REBUILD,
                    details="records published after the partition day are visible",
                )
            )
        visible = [row for row in rows if row["published_on"] <= day.isoformat()]
        distinct_visible = {row["code"] for row in visible}
        actual = len(distinct_visible)
        expected_codes = set(task.codes)
        missing = tuple(sorted(expected_codes - distinct_visible))
        ratio = _ratio(len(distinct_visible), len(expected_codes))
        if missing and ratio < DAILY_BAR_COMPLETE_RATIO:
            issues.append(
                VerificationIssue(
                    issue_id=(
                        f"{candidate_id}:fundamentals:"
                        f"{evidence_partition_key}:coverage"
                    ),
                    candidate_generation_id=candidate_id,
                    data_type="fundamentals",
                    partition_key=evidence_partition_key,
                    trading_day=day,
                    codes=missing,
                    issue_type=IssueType.MISSING,
                    expected_count=len(expected_codes),
                    actual_count=len(distinct_visible),
                    repairability=Repairability.REFETCH,
                    details=(
                        f"fundamentals coverage {ratio:.2%} below "
                        f"{DAILY_BAR_COMPLETE_RATIO:.0%}"
                    ),
                )
            )
        status = (
            VerificationStatus.COMPLETE
            if not violated and ratio >= DAILY_BAR_COMPLETE_RATIO
            else VerificationStatus.INCOMPLETE
        )
        records.append(
            CoverageVerificationRecord(
                candidate_generation_id=candidate_id,
                data_type="fundamentals",
                partition_key=evidence_partition_key,
                expected_count=len(expected_codes),
                actual_count=actual,
                distinct_count=len(distinct_visible),
                duplicate_count=0,
                invalid_count=len(violated),
                coverage_ratio=ratio,
                missing_items=tuple(sorted(set(violated) | set(missing))),
                status=status,
                verified_revision=write_revision,
                manifest_sha256=manifest_sha256,
                verified_at=_now(),
                details_json="{}",
            )
        )

    def _verify_dividends(
        self,
        connection: sqlite3.Connection,
        candidate_id: str,
        write_revision: int,
        manifest_sha256: str,
        task: SyncTask,
        day: date,
        evidence_partition_key: str,
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        rows = connection.execute(
            """SELECT COUNT(*) AS n FROM dividends_staging
               WHERE batch_id IN (
                   SELECT batch_id FROM ingest_batches
                   WHERE candidate_generation_id = ?
               ) AND ex_date = ?""",
            (candidate_id, day.isoformat()),
        ).fetchone()
        actual = int(rows["n"])
        missing: tuple[str, ...] = ()
        if actual == 0:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:dividends:{day.isoformat()}",
                    candidate_generation_id=candidate_id,
                    data_type="dividends",
                    partition_key=evidence_partition_key,
                    trading_day=day,
                    codes=(),
                    issue_type=IssueType.MISSING,
                    expected_count=0,
                    actual_count=0,
                    repairability=Repairability.MANUAL,
                    details="no dividends staged for the day (may legitimately be empty)",
                )
            )
            missing = ("none",)
        records.append(
            CoverageVerificationRecord(
                candidate_generation_id=candidate_id,
                data_type="dividends",
                partition_key=evidence_partition_key,
                expected_count=0,
                actual_count=actual,
                distinct_count=actual,
                duplicate_count=0,
                invalid_count=0,
                coverage_ratio=Decimal("1") if actual > 0 else Decimal("0"),
                missing_items=missing,
                status=(
                    VerificationStatus.COMPLETE
                    if actual > 0
                    else VerificationStatus.UNAVAILABLE
                ),
                verified_revision=write_revision,
                manifest_sha256=manifest_sha256,
                verified_at=_now(),
                details_json="{}",
            )
        )


def _partition_date(partition_key: str) -> date:
    """Parse the leading date of a partition key.

    Supports ``YYYY-MM-DD`` (single day) and ``YYYY-MM-DD..YYYY-MM-DD``
    (batch range partition keys produced by the batch-granularity planner).
    """
    head = partition_key.split(":")[0].split("..")[0]
    return date.fromisoformat(head)


def _ratio(actual: int, expected: int) -> Decimal:
    if expected <= 0:
        return Decimal("0")
    return Decimal(actual) / Decimal(expected)


def _coverage_threshold(expected: int) -> int:
    """Return ceil(expected * 95%) without floating-point rounding."""
    if expected <= 0:
        return 0
    return (expected * 95 + 99) // 100


def _valid_bar(row: sqlite3.Row) -> bool:
    try:
        high = Decimal(row["high"])
        low = Decimal(row["low"])
        open_ = Decimal(row["open"])
        close = Decimal(row["close"])
        volume = Decimal(row["volume"])
    except (InvalidOperation, TypeError, ValueError):
        return False
    if high < low or high < 0 or low < 0:
        return False
    if open_ < 0 or close < 0:
        return False
    if volume < 0:
        return False
    return True


def _now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))
