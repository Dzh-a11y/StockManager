"""One-time migration of legacy shared tables into the P5 model (P5-RD-9).

The legacy database's rows carry no generation/batch binding, so its
``dataset_versions.COMPLETE`` cannot be trusted as a verified snapshot.
``LegacyImporter`` registers the old data as a ``LEGACY_IMPORT`` candidate,
copies rows into staging batches, builds the partition manifest, and hands
the candidate to the CoverageVerifier / GenerationCommitter. On any failure
the old database and its rows are left untouched (the importer only reads
legacy tables and writes staging/bookkeeping).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from datetime import date, datetime

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    GenerationPartition,
    IngestBatch,
)

#: Legacy tables to import, mapped to their staging tables.
_LEGACY_TO_STAGING: dict[str, str] = {
    "daily_bars": "daily_bars_staging",
    "stocks": "stocks_staging",
    "fundamentals": "fundamentals_staging",
    "dividends": "dividends_staging",
}


class LegacyImportError(RuntimeError):
    """Raised when the legacy import cannot proceed."""


class LegacyImporter:
    """Copies legacy shared-table rows into staged candidate batches."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._connection_factory = connection_factory
        self._now = now

    def build_candidate(
        self,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        plan_id: str,
        candidate_id: str,
    ) -> CandidateGeneration:
        """Register the LEGACY_IMPORT candidate in WRITING state."""
        now = self._now()
        candidate = CandidateGeneration(
            candidate_generation_id=candidate_id,
            plan_id=plan_id,
            parent_generation=None,
            write_revision=0,
            status=CandidateGenerationStatus.WRITING,
            created_at=now,
            updated_at=now,
        )
        with closing(self._connection_factory()) as connection:
            connection.execute(
                """INSERT INTO candidate_generations
                   (candidate_generation_id, plan_id, parent_generation,
                    write_revision, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(candidate_generation_id) DO UPDATE SET
                     status = excluded.status, updated_at = excluded.updated_at""",
                (
                    candidate_id,
                    plan_id,
                    None,
                    0,
                    CandidateGenerationStatus.WRITING.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            connection.commit()
        return candidate

    def import_partition(
        self,
        candidate: CandidateGeneration,
        *,
        data_type: str,
        partition_key: str,
        batch_id: str,
        source: str,
        adjustment: AdjustmentMethod | None = None,
    ) -> IngestBatch | None:
        """Copy one legacy partition (a day's rows) into a staged batch.

        Rows are taken from the legacy shared table filtered by the
        partition's trading day; the source table is never modified. Returns
        the IngestBatch record, or ``None`` when the partition has no rows
        (the data type simply stays UNAVAILABLE, no batch is created).
        """
        legacy_table = _LEGACY_TO_STAGING.get(data_type)
        if legacy_table is None:
            raise LegacyImportError(f"unsupported legacy data type {data_type}")
        day = date.fromisoformat(partition_key)
        now = self._now()
        connection = self._connection_factory()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                current = connection.execute(
                    """SELECT status FROM candidate_generations
                       WHERE candidate_generation_id = ?""",
                    (candidate.candidate_generation_id,),
                ).fetchone()
                current_status = None if current is None else str(current[0])
                if current_status != (
                    CandidateGenerationStatus.WRITING.value
                ):
                    actual = "missing" if current is None else current_status
                    raise LegacyImportError(
                        f"candidate {candidate.candidate_generation_id} is "
                        f"{actual}, not WRITING"
                    )
                count = self._copy_rows(
                    connection, data_type, batch_id, day, adjustment
                )
                codes = self._partition_codes(
                    connection, data_type, day, adjustment
                )
                digest = self._batch_digest(connection, data_type, batch_id)
                if count == 0 or not codes:
                    connection.execute(
                        f"DELETE FROM {legacy_table} WHERE batch_id = ?",
                        (batch_id,),
                    )
                    connection.commit()
                    return None
                batch = IngestBatch(
                    batch_id=batch_id,
                    candidate_generation_id=candidate.candidate_generation_id,
                    data_type=data_type,
                    partition_key=partition_key,
                    codes=codes,
                    range_start=day,
                    range_end=day,
                    row_count=count,
                    source=source,
                    batch_sha256=digest,
                    created_at=now,
                )
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
                connection.execute(
                    """UPDATE candidate_generations
                       SET write_revision = write_revision + 1, updated_at = ?
                       WHERE candidate_generation_id = ?""",
                    (now.isoformat(), candidate.candidate_generation_id),
                )
                connection.commit()
                return batch
            except (sqlite3.Error, LegacyImportError) as error:
                connection.rollback()
                if isinstance(error, LegacyImportError):
                    raise
                raise LegacyImportError(
                    f"legacy import failed for {data_type} {partition_key}: {error}"
                ) from error
        finally:
            connection.close()

    def finish_candidate(
        self, candidate: CandidateGeneration
    ) -> CandidateGeneration:
        """Move the imported candidate into VERIFYING."""
        now = self._now()
        with closing(self._connection_factory()) as connection:
            connection.execute(
                """UPDATE candidate_generations SET status = ?, updated_at = ?
                   WHERE candidate_generation_id = ?""",
                (
                    CandidateGenerationStatus.VERIFYING.value,
                    now.isoformat(),
                    candidate.candidate_generation_id,
                ),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM candidate_generations "
                "WHERE candidate_generation_id = ?",
                (candidate.candidate_generation_id,),
            ).fetchone()
        if row is None:
            raise LegacyImportError("candidate row missing after finish")
        return CandidateGeneration(
            candidate_generation_id=row["candidate_generation_id"],
            plan_id=row["plan_id"],
            parent_generation=row["parent_generation"],
            write_revision=int(row["write_revision"]),
            status=CandidateGenerationStatus(row["status"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def partitions_for(
        self,
        candidate_id: str,
        *,
        generation: str,
    ) -> tuple[GenerationPartition, ...]:
        """Build the partition manifest from the candidate's batches."""
        with closing(self._connection_factory()) as connection:
            rows = connection.execute(
                """SELECT data_type, partition_key, batch_id
                   FROM ingest_batches
                   WHERE candidate_generation_id = ?
                   ORDER BY data_type, partition_key""",
                (candidate_id,),
            ).fetchall()
        return tuple(
            GenerationPartition(
                generation=generation,
                data_type=row["data_type"],
                partition_key=row["partition_key"],
                batch_id=row["batch_id"],
            )
            for row in rows
        )

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _copy_rows(
        connection: sqlite3.Connection,
        data_type: str,
        batch_id: str,
        day: date,
        adjustment: AdjustmentMethod | None,
    ) -> int:
        """Copy one day's legacy rows into the staging table; return count."""
        day_text = day.isoformat()
        if data_type == "daily_bars":
            if adjustment is None:
                raise LegacyImportError(
                    "daily_bars legacy import requires an adjustment"
                )
            cursor = connection.execute(
                """INSERT OR REPLACE INTO daily_bars_staging
                   (batch_id, code, trading_day, adjustment, open, high, low,
                    close, preclose, volume, amount, is_trading)
                   SELECT ?, code, trading_day, adjustment, open, high, low,
                          close, preclose, volume, amount, is_trading
                   FROM daily_bars
                   WHERE trading_day = ? AND adjustment = ?""",
                (batch_id, day_text, adjustment.value),
            )
        elif data_type == "stocks":
            cursor = connection.execute(
                """INSERT OR REPLACE INTO stocks_staging
                   (batch_id, code, as_of, name, exchange, is_st,
                    listed_on, delisted_on)
                   SELECT ?, code, as_of, name, exchange, is_st,
                          listed_on, delisted_on
                   FROM stocks WHERE as_of = ?""",
                (batch_id, day_text),
            )
        elif data_type == "fundamentals":
            cursor = connection.execute(
                """INSERT OR REPLACE INTO fundamentals_staging
                   (batch_id, code, report_date, published_on, pe_ttm, pb, source)
                   SELECT ?, code, report_date, published_on, pe_ttm, pb, source
                   FROM fundamentals WHERE published_on = ?""",
                (batch_id, day_text),
            )
        elif data_type == "dividends":
            cursor = connection.execute(
                """INSERT OR REPLACE INTO dividends_staging
                   (batch_id, code, ex_date, cash_dividend_per_share, source)
                   SELECT ?, code, ex_date, cash_dividend_per_share, source
                   FROM dividends WHERE ex_date = ?""",
                (batch_id, day_text),
            )
        else:  # pragma: no cover - guarded by _LEGACY_TO_STAGING
            raise LegacyImportError(f"unsupported data type {data_type}")
        return int(cursor.rowcount)

    @staticmethod
    def _partition_codes(
        connection: sqlite3.Connection,
        data_type: str,
        day: date,
        adjustment: AdjustmentMethod | None,
    ) -> tuple[str, ...]:
        """Distinct codes in the legacy partition (deterministic order)."""
        day_text = day.isoformat()
        if data_type == "daily_bars":
            sql = (
                "SELECT DISTINCT code FROM daily_bars "
                "WHERE trading_day = ? AND adjustment = ? ORDER BY code"
            )
            params: tuple[object, ...] = (day_text, adjustment.value if adjustment else "")
        elif data_type == "stocks":
            sql = "SELECT DISTINCT code FROM stocks WHERE as_of = ? ORDER BY code"
            params = (day_text,)
        elif data_type == "fundamentals":
            sql = (
                "SELECT DISTINCT code FROM fundamentals "
                "WHERE published_on = ? ORDER BY code"
            )
            params = (day_text,)
        elif data_type == "dividends":
            sql = (
                "SELECT DISTINCT code FROM dividends "
                "WHERE ex_date = ? ORDER BY code"
            )
            params = (day_text,)
        else:  # pragma: no cover - guarded by _LEGACY_TO_STAGING
            raise LegacyImportError(f"unsupported data type {data_type}")
        rows = connection.execute(sql, params).fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _batch_digest(
        connection: sqlite3.Connection, data_type: str, batch_id: str
    ) -> str:
        staging = _LEGACY_TO_STAGING[data_type]
        digest = hashlib.sha256()
        rows = connection.execute(
            f"SELECT * FROM {staging} WHERE batch_id = ? ORDER BY rowid",
            (batch_id,),
        ).fetchall()
        for row in rows:
            digest.update("|".join(str(value) for value in row).encode("utf-8"))
        return digest.hexdigest()

    @staticmethod
    def write_integrity_report(database_path: object) -> str:
        """Produce a pre-migration integrity report (JSON string).

        Uses ``storage.integrity.verify_database_integrity`` so the report is
        read-only and never modifies the legacy database.
        """
        from stock_manager.storage.integrity import verify_database_integrity

        return json.dumps(
            verify_database_integrity(database_path),  # type: ignore[arg-type]
            ensure_ascii=False,
            sort_keys=True,
        )
