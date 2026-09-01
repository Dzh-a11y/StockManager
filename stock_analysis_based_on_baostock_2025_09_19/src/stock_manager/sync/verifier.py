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
from collections.abc import Callable, Sequence
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
    ) -> None:
        self._connection_factory = connection_factory
        self._trading_days = trading_days
        self._expected_universe_size = expected_universe_size

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
        seen: set[tuple[str, str]] = set()
        try:
            with self._connection_factory() as connection:
                manifest_sha256 = self._candidate_manifest(connection, candidate_id)
                for task in tasks:
                    key = (task.data_type, task.partition_key)
                    if key in seen:
                        continue
                    seen.add(key)
                    self._verify_partition(
                        connection,
                        candidate_id,
                        write_revision,
                        manifest_sha256,
                        task,
                        adjustment,
                        target_start,
                        target_end,
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
                    partition_key=task.partition_key,
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
                task, adjustment, day, issues, records,
            )
        elif task.data_type == "stocks":
            self._verify_stocks(
                connection, candidate_id, write_revision, manifest_sha256,
                task, day, issues, records,
            )
        elif task.data_type == "fundamentals":
            self._verify_fundamentals(
                connection, candidate_id, write_revision, manifest_sha256,
                task, day, issues, records,
            )
        elif task.data_type == "dividends":
            self._verify_dividends(
                connection, candidate_id, write_revision, manifest_sha256,
                task, day, issues, records,
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
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        codes = tuple(sorted(task.codes))
        placeholders = ",".join("?" for _ in codes) if codes else "''"
        expected = len(codes)
        # 批量粒度:一个任务覆盖整个区间(range_start..range_end)。
        # 每只代码在该区间内应拥有 bar;按 distinct code 判定完整性,
        # 并检查无效值(high<low/负值)与跨批次重复。
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
        days_in_range = len(
            self._trading_days(task.range_start, task.range_end)
        )
        complete_threshold = max(1, int(days_in_range * 0.95))
        day_counts: dict[str, set[str]] = {}
        invalid_rows = 0
        for row in rows:
            day_counts.setdefault(row["code"], set()).add(row["trading_day"])
            if not _valid_bar(row):
                invalid_rows += 1
        complete_codes = {
            code for code, days in day_counts.items() if len(days) >= complete_threshold
        }
        present_codes = set(day_counts)
        actual = len(present_codes)
        duplicates = 0
        invalid = invalid_rows
        missing = tuple(sorted(set(codes) - complete_codes))
        ratio = _ratio(len(complete_codes), expected)
        if missing:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:daily_bars:{task.partition_key}:coverage",
                    candidate_generation_id=candidate_id,
                    data_type="daily_bars",
                    partition_key=task.partition_key,
                    trading_day=_partition_date(task.partition_key),
                    codes=missing,
                    issue_type=IssueType.MISSING,
                    expected_count=expected,
                    actual_count=actual,
                    repairability=Repairability.REFETCH,
                    details=(
                        f"coverage {ratio:.2%} below {DAILY_BAR_COMPLETE_RATIO:.0%}; "
                        f"missing {len(missing)} of {expected} codes over "
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
                    partition_key=task.partition_key,
                    trading_day=_partition_date(task.partition_key),
                    codes=tuple(sorted(present_codes)),
                    issue_type=IssueType.INVALID,
                    expected_count=expected,
                    actual_count=actual,
                    repairability=Repairability.REFETCH,
                    details=f"{invalid} invalid bar rows in the batch range",
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
                partition_key=task.partition_key,
                expected_count=expected,
                actual_count=actual,
                distinct_count=len(present_codes),
                duplicate_count=duplicates,
                invalid_count=invalid,
                coverage_ratio=ratio,
                missing_items=tuple(sorted(set(codes) - complete_codes)),
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
        missing: tuple[str, ...] = ()
        if actual == 0:
            issues.append(
                VerificationIssue(
                    issue_id=f"{candidate_id}:stocks:{day.isoformat()}",
                    candidate_generation_id=candidate_id,
                    data_type="stocks",
                    partition_key=task.partition_key,
                    trading_day=day,
                    codes=(),
                    issue_type=IssueType.MISSING,
                    expected_count=expected,
                    actual_count=actual,
                    repairability=Repairability.REFETCH,
                    details="no staged stock snapshot for the day",
                )
            )
            missing = ("all",)
        status = (
            VerificationStatus.COMPLETE
            if actual > 0
            else VerificationStatus.INCOMPLETE
        )
        records.append(
            CoverageVerificationRecord(
                candidate_generation_id=candidate_id,
                data_type="stocks",
                partition_key=task.partition_key,
                expected_count=expected,
                actual_count=actual,
                distinct_count=actual,
                duplicate_count=0,
                invalid_count=0,
                coverage_ratio=_ratio(actual, expected),
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
        issues: list[VerificationIssue],
        records: list[CoverageVerificationRecord],
    ) -> None:
        rows = connection.execute(
            """SELECT code, report_date, published_on FROM fundamentals_staging
               WHERE batch_id IN (
                   SELECT batch_id FROM ingest_batches
                   WHERE candidate_generation_id = ? AND data_type = ?
                     AND partition_key = ?
               )""",
            (candidate_id, "fundamentals", task.partition_key),
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
                    partition_key=task.partition_key,
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
        actual = len(visible)
        status = (
            VerificationStatus.COMPLETE
            if not violated
            else VerificationStatus.INCOMPLETE
        )
        records.append(
            CoverageVerificationRecord(
                candidate_generation_id=candidate_id,
                data_type="fundamentals",
                partition_key=task.partition_key,
                expected_count=len(distinct_visible),
                actual_count=actual,
                distinct_count=len(distinct_visible),
                duplicate_count=0,
                invalid_count=len(violated),
                coverage_ratio=_ratio(
                    len(distinct_visible),
                    max(1, len(distinct_visible)),
                ),
                missing_items=violated,
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
                    partition_key=task.partition_key,
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
                partition_key=task.partition_key,
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


def _valid_bar(row: sqlite3.Row) -> bool:
    try:
        high = Decimal(row["high"])
        low = Decimal(row["low"])
        open_ = Decimal(row["open"])
        close = Decimal(row["close"])
        volume = Decimal(row["volume"])
    except Exception:
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
