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
from datetime import date, datetime

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    DailyBar,
    DividendRecord,
    FundamentalSnapshot,
    IngestBatch,
    StockIdentity,
    SyncTask,
    SyncTaskStatus,
)

_STAGING_TABLE = {
    "stocks": "stocks_staging",
    "daily_bars": "daily_bars_staging",
    "fundamentals": "fundamentals_staging",
    "dividends": "dividends_staging",
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
        batch_id = self._batch_id(candidate, task)
        now = self._now()
        with self._connection_factory() as connection:
            try:
                self._delete_batch_rows(connection, table, batch_id)
                count = self._insert_rows(
                    connection, table, batch_id, task, rows, adjustment
                )
                digest = self._batch_digest(connection, table, batch_id)
            except sqlite3.Error as error:
                connection.rollback()
                raise StagingWriteError(
                    f"staging write failed for batch {batch_id}: {error}"
                ) from error
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
        with self._connection_factory() as connection:
            self._save_batch_record(connection, batch)
            self._bump_revision(connection, candidate.candidate_generation_id, now)
        return batch

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
            candidate.plan_id,
            candidate.parent_generation,
            candidate.write_revision,
            None,
        )

    # -- internals ------------------------------------------------------------

    def _batch_id(self, candidate: CandidateGeneration, task: SyncTask) -> str:
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
        plan_id: str,
        parent_generation: str | None,
        write_revision: int,
        source: str | None,
    ) -> None:
        now = self._now()
        with self._connection_factory() as connection:
            connection.execute(
                """INSERT INTO candidate_generations
                   (candidate_generation_id, plan_id, parent_generation,
                    write_revision, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(candidate_generation_id) DO UPDATE SET
                     status = excluded.status,
                     updated_at = excluded.updated_at""",
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
            connection.commit()

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
        if table == "stocks_staging":
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
                    if isinstance(row, StockIdentity)
                ],
            )
        elif table == "daily_bars_staging":
            if adjustment is None:
                raise StagingWriteError(
                    "daily_bars batch requires an explicit adjustment"
                )
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
                    if isinstance(row, DailyBar)
                ],
            )
        elif table == "fundamentals_staging":
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
                    if isinstance(row, FundamentalSnapshot)
                ],
            )
        elif table == "dividends_staging":
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
                    if isinstance(row, DividendRecord)
                ],
            )
        else:  # pragma: no cover - guarded by _STAGING_TABLE lookup
            raise StagingWriteError(f"unsupported staging table {table}")
        connection.commit()
        return len(rows)

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
        connection.commit()

    @staticmethod
    def _bump_revision(
        connection: sqlite3.Connection,
        candidate_generation_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            """UPDATE candidate_generations
               SET write_revision = write_revision + 1, updated_at = ?
               WHERE candidate_generation_id = ?""",
            (now.isoformat(), candidate_generation_id),
        )
        connection.commit()