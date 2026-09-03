"""Idempotent candidate batch writing into staging tables (P5-RD-4).

The StagingWriter persists fetched rows into ``*_staging`` tables keyed by
``batch_id`` so a half-written candidate is physically invisible to published
readers (they never read staging). Every successful write bumps the
candidate's ``write_revision``; the CoverageVerifier snapshots that revision
so any later write invalidates the verification.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date, datetime

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    IngestBatch,
    IndexIdentity,
    IndexDailyBar,
    DepositRate,
    StockIdentity,
    SyncTask,
    SyncTaskStatus,
)

_STAGING_TABLE = {
    "stocks": "stocks_staging",
    "daily_bars": "daily_bars_staging",
    "fundamentals": "fundamentals_staging",
    "dividends": "dividends_staging",
    "index_catalog": "index_catalog_staging",
    "index_daily_bars": "index_daily_bars_staging",
    "deposit_rates": "deposit_rates_staging",
}


class StagingWriteError(RuntimeError):
    """Raised when a batch write cannot be persisted."""


class CandidateNotWritableError(RuntimeError):
    """Raised when a candidate is not in a writable lifecycle state."""


class StagingWriter:
    """Writes one candidate's batches into isolated staging tables."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._connection_factory = connection_factory
        self._now = now

    def begin_candidate(
        self, candidate: CandidateGeneration, source: str
    ) -> None:
        """Mark a PLANNED candidate WRITING (one batch row per data type)."""
        if candidate.status is not CandidateGenerationStatus.PLANNED:
            raise CandidateNotWritableError(
                f"candidate {candidate.candidate_generation_id} is "
                f"{candidate.status.value}, not PLANNED"
            )
        self._update_status(
            candidate.candidate_generation_id,
            CandidateGenerationStatus.WRITING,
            CandidateGenerationStatus.PLANNED,
            candidate.plan_id,
            candidate.parent_generation,
            candidate.write_revision,
            source,
        )

    def write_batch(
        self,
        candidate: CandidateGeneration,
        task: SyncTask,
        rows: Sequence[object],
        *,
        source: str,
        adjustment: AdjustmentMethod | None = None,
    ) -> IngestBatch:
        """Idempotently write one task's rows into the candidate's batch.

        Re-writing the same (candidate, data_type, partition_key, codes)
        replaces that batch's staging rows only; published tables are never
        touched. Returns the persisted IngestBatch record.
        """
        if candidate.status not in (
            CandidateGenerationStatus.WRITING,
            CandidateGenerationStatus.NEEDS_REPAIR,
        ):
            raise CandidateNotWritableError(
                f"candidate {candidate.candidate_generation_id} is not writable"
            )
        table = _STAGING_TABLE.get(task.data_type)
        if table is None:
            raise StagingWriteError(
                f"no staging table for data type {task.data_type}"
            )
        batch_id = self.batch_id(candidate, task)
        now = self._now()
        connection = self._connection_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._assert_writable(
                    connection, candidate.candidate_generation_id
                )
                self._delete_batch_rows(connection, table, batch_id)
                count = self._insert_rows(
                    connection, table, batch_id, task, rows, adjustment
                )
                digest = self._batch_digest(connection, table, batch_id)
                batch = IngestBatch(
                    batch_id=batch_id,
                    candidate_generation_id=candidate.candidate_generation_id,
                    data_type=task.data_type,
                    partition_key=task.partition_key,
                    codes=task.codes,
                    range_start=task.range_start,
                    range_end=task.range_end,
                    row_count=count,
                    source=source,
                    batch_sha256=digest,
                    created_at=now,
                )
                self._save_batch_record(connection, batch)
                self._bump_revision(
                    connection, candidate.candidate_generation_id, now
                )
                connection.commit()
                return batch
            except (
                sqlite3.Error,
                StagingWriteError,
                CandidateNotWritableError,
            ) as error:
                connection.rollback()
                if isinstance(
                    error, (StagingWriteError, CandidateNotWritableError)
                ):
                    raise
                raise StagingWriteError(
                    f"staging write failed for batch {batch_id}: {error}"
                ) from error
        finally:
            connection.close()

    def recover_batch(
        self,
        candidate: CandidateGeneration,
        task: SyncTask,
        *,
        source: str,
    ) -> IngestBatch | None:
        """Register a fully written orphan batch without calling Provider again.

        Older builds committed staging rows before the ingest checkpoint. A
        SUCCESS task is recoverable only when the actual staged row count
        exactly matches its persisted ``row_count``; otherwise the caller must
        reset the task and refetch it.
        """
        table = _STAGING_TABLE.get(task.data_type)
        if table is None or task.row_count is None:
            return None
        batch_id = self.batch_id(candidate, task)
        now = self._now()
        connection = self._connection_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._assert_writable(
                connection, candidate.candidate_generation_id
            )
            existing = connection.execute(
                "SELECT batch_id FROM ingest_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return self._batch_from_database(connection, batch_id)
            actual_count = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE batch_id = ?",
                    (batch_id,),
                ).fetchone()[0]
            )
            if actual_count != task.row_count:
                connection.rollback()
                return None
            batch = IngestBatch(
                batch_id=batch_id,
                candidate_generation_id=candidate.candidate_generation_id,
                data_type=task.data_type,
                partition_key=task.partition_key,
                codes=task.codes,
                range_start=task.range_start,
                range_end=task.range_end,
                row_count=actual_count,
                source=source,
                batch_sha256=self._batch_digest(connection, table, batch_id),
                created_at=now,
            )
            self._save_batch_record(connection, batch)
            self._bump_revision(
                connection, candidate.candidate_generation_id, now
            )
            connection.commit()
            return batch
        except CandidateNotWritableError:
            connection.rollback()
            raise
        except sqlite3.Error as error:
            connection.rollback()
            raise StagingWriteError(
                f"orphan batch recovery failed for {batch_id}: {error}"
            ) from error
        finally:
            connection.close()

    def finish_candidate(self, candidate: CandidateGeneration) -> None:
        """Move a fully-written candidate into VERIFYING."""
        if candidate.status is not CandidateGenerationStatus.WRITING:
            raise CandidateNotWritableError(
                f"candidate {candidate.candidate_generation_id} is "
                f"{candidate.status.value}, not WRITING"
            )
        self._update_status(
            candidate.candidate_generation_id,
            CandidateGenerationStatus.VERIFYING,
            CandidateGenerationStatus.WRITING,
            candidate.plan_id,
            candidate.parent_generation,
            candidate.write_revision,
            None,
        )

    # -- internals ------------------------------------------------------------

    def batch_id(self, candidate: CandidateGeneration, task: SyncTask) -> str:
        """Return the deterministic batch identity for one candidate task."""
        digest = hashlib.sha256()
        digest.update(
            "|".join(
                (
                    candidate.candidate_generation_id,
                    task.data_type,
                    task.partition_key,
                    ",".join(sorted(task.codes)),
                    task.range_start.isoformat(),
                    task.range_end.isoformat(),
                )
            ).encode("utf-8")
        )
        return f"batch-{digest.hexdigest()[:16]}"

    def _update_status(
        self,
        candidate_generation_id: str,
        status: CandidateGenerationStatus,
        expected_status: CandidateGenerationStatus,
        plan_id: str,
        parent_generation: str | None,
        write_revision: int,
        source: str | None,
    ) -> None:
        now = self._now()
        with closing(self._connection_factory()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """SELECT status FROM candidate_generations
                       WHERE candidate_generation_id = ?""",
                    (candidate_generation_id,),
                ).fetchone()
                if existing is None:
                    if expected_status is not CandidateGenerationStatus.PLANNED:
                        raise CandidateNotWritableError(
                            f"candidate {candidate_generation_id} is missing"
                        )
                    connection.execute(
                        """INSERT INTO candidate_generations
                           (candidate_generation_id, plan_id, parent_generation,
                            write_revision, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            candidate_generation_id,
                            plan_id,
                            parent_generation,
                            write_revision,
                            status.value,
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                else:
                    existing_status = str(existing[0])
                if existing is not None and existing_status != expected_status.value:
                    raise CandidateNotWritableError(
                        f"candidate {candidate_generation_id} changed to "
                        f"{existing_status} before transition"
                    )
                if existing is not None:
                    connection.execute(
                        """UPDATE candidate_generations
                           SET status = ?, updated_at = ?
                           WHERE candidate_generation_id = ?""",
                        (status.value, now.isoformat(), candidate_generation_id),
                    )
                connection.commit()
            except (sqlite3.Error, CandidateNotWritableError):
                connection.rollback()
                raise

    @staticmethod
    def _assert_writable(
        connection: sqlite3.Connection, candidate_generation_id: str
    ) -> None:
        row = connection.execute(
            """SELECT status FROM candidate_generations
               WHERE candidate_generation_id = ?""",
            (candidate_generation_id,),
        ).fetchone()
        actual_status = None if row is None else str(row[0])
        if actual_status not in (
            CandidateGenerationStatus.WRITING.value,
            CandidateGenerationStatus.NEEDS_REPAIR.value,
        ):
            actual = "missing" if row is None else actual_status
            raise CandidateNotWritableError(
                f"candidate {candidate_generation_id} is {actual}, not writable"
            )

    @staticmethod
    def _delete_batch_rows(
        connection: sqlite3.Connection, table: str, batch_id: str
    ) -> None:
        connection.execute(
            f"DELETE FROM {table} WHERE batch_id = ?", (batch_id,)
        )

    def _insert_rows(
        self,
        connection: sqlite3.Connection,
        table: str,
        batch_id: str,
        task: SyncTask,
        rows: Sequence[object],
        adjustment: AdjustmentMethod | None,
    ) -> int:
        if not rows:
            return 0
        if table == "index_catalog_staging":
            self._require_row_type(rows, IndexIdentity, table)
            connection.executemany(
                "INSERT INTO index_catalog_staging VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(batch_id, r.index_id, r.provider_code, r.name, r.category,
                  r.return_version.value, r.source) for r in rows],
            )
            return len(rows)
        if table == "index_daily_bars_staging":
            self._require_row_type(rows, IndexDailyBar, table)
            connection.executemany(
                "INSERT INTO index_daily_bars_staging VALUES (?, ?, ?, ?, ?)",
                [(batch_id, r.index_id, r.trading_day.isoformat(), str(r.close),
                  r.return_version.value) for r in rows],
            )
            return len(rows)
        if table == "deposit_rates_staging":
            self._require_row_type(rows, DepositRate, table)
            connection.executemany(
                "INSERT INTO deposit_rates_staging VALUES (?, ?, ?, ?, ?)",
                [(batch_id, r.term, r.effective_on.isoformat(), str(r.annual_rate),
                  r.source) for r in rows],
            )
            return len(rows)
        if table == "stocks_staging":
            self._require_row_type(rows, StockIdentity, table)
            connection.executemany(
                """INSERT OR REPLACE INTO stocks_staging
                   (batch_id, code, as_of, name, exchange, is_st,
                    listed_on, delisted_on)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        batch_id,
                        row.code,
                        task.range_end.isoformat(),
                        row.name,
                        row.exchange,
                        int(row.is_st),
                        None if row.listed_on is None else row.listed_on.isoformat(),
                        None if row.delisted_on is None else row.delisted_on.isoformat(),
                    )
                    for row in rows
                ],
            )
        elif table == "daily_bars_staging":
            if adjustment is None:
                raise StagingWriteError(
                    "daily_bars batch requires an explicit adjustment"
                )
            self._require_row_type(rows, DailyBar, table)
            connection.executemany(
                """INSERT OR REPLACE INTO daily_bars_staging
                   (batch_id, code, trading_day, adjustment, open, high, low,
                    close, preclose, volume, amount, is_trading)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        batch_id,
                        row.code,
                        row.trading_day.isoformat(),
                        adjustment.value,
                        str(row.open),
                        str(row.high),
                        str(row.low),
                        str(row.close),
                        str(row.preclose),
                        str(row.volume),
                        str(row.amount),
                        int(row.is_trading),
                    )
                    for row in rows
                ],
            )
        elif table == "fundamentals_staging":
            self._require_row_type(rows, FundamentalSnapshot, table)
            connection.executemany(
                """INSERT OR REPLACE INTO fundamentals_staging
                   (batch_id, code, report_date, published_on, pe_ttm, pb, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        batch_id,
                        row.code,
                        row.report_date.isoformat(),
                        row.published_on.isoformat(),
                        None if row.pe_ttm is None else str(row.pe_ttm),
                        None if row.pb is None else str(row.pb),
                        row.source,
                    )
                    for row in rows
                ],
            )
        elif table == "dividends_staging":
            self._require_row_type(rows, DividendRecord, table)
            connection.executemany(
                """INSERT OR REPLACE INTO dividends_staging
                   (batch_id, code, ex_date, cash_dividend_per_share, source)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        batch_id,
                        row.code,
                        row.ex_date.isoformat(),
                        str(row.cash_dividend_per_share),
                        row.source,
                    )
                    for row in rows
                ],
            )
        else:  # pragma: no cover - guarded by _STAGING_TABLE lookup
            raise StagingWriteError(f"unsupported staging table {table}")
        return len(rows)

    @staticmethod
    def _require_row_type(
        rows: Sequence[object], expected_type: type[object], table: str
    ) -> None:
        if any(not isinstance(row, expected_type) for row in rows):
            raise StagingWriteError(
                f"batch for {table} contains an unexpected row type"
            )

    @staticmethod
    def _batch_digest(
        connection: sqlite3.Connection, table: str, batch_id: str
    ) -> str:
        digest = hashlib.sha256()
        rows = connection.execute(
            f"SELECT * FROM {table} WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        ).fetchall()
        for row in rows:
            digest.update("|".join(str(value) for value in row).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def _save_batch_record(
        connection: sqlite3.Connection, batch: IngestBatch
    ) -> None:
        connection.execute(
            """INSERT OR REPLACE INTO ingest_batches
               (batch_id, candidate_generation_id, data_type, partition_key,
                codes, range_start, range_end, row_count, source,
                batch_sha256, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                batch.batch_id,
                batch.candidate_generation_id,
                batch.data_type,
                batch.partition_key,
                ",".join(batch.codes),
                batch.range_start.isoformat(),
                batch.range_end.isoformat(),
                batch.row_count,
                batch.source,
                batch.batch_sha256,
                batch.created_at.isoformat(),
            ),
        )

    @staticmethod
    def _bump_revision(
        connection: sqlite3.Connection,
        candidate_generation_id: str,
        now: datetime,
    ) -> None:
        cursor = connection.execute(
            """UPDATE candidate_generations
               SET write_revision = write_revision + 1, updated_at = ?
               WHERE candidate_generation_id = ?""",
            (now.isoformat(), candidate_generation_id),
        )
        if cursor.rowcount != 1:
            raise StagingWriteError(
                f"candidate disappeared before revision bump: "
                f"{candidate_generation_id}"
            )

    @staticmethod
    def _batch_from_database(
        connection: sqlite3.Connection, batch_id: str
    ) -> IngestBatch:
        row = connection.execute(
            "SELECT * FROM ingest_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if row is None:
            raise StagingWriteError(f"ingest batch disappeared: {batch_id}")
        return IngestBatch(
            batch_id=row["batch_id"],
            candidate_generation_id=row["candidate_generation_id"],
            data_type=row["data_type"],
            partition_key=row["partition_key"],
            codes=tuple(row["codes"].split(",")),
            range_start=date.fromisoformat(row["range_start"]),
            range_end=date.fromisoformat(row["range_end"]),
            row_count=int(row["row_count"]),
            source=row["source"],
            batch_sha256=row["batch_sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
