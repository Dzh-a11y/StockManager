"""Atomic generation publishing and read gating (P5-RD-6).

``GenerationCommitter`` publishes a VERIFIED candidate within one SQLite
write transaction: it re-checks the candidate/verification state, materializes
the generation manifest (partition -> batch), writes the published generation,
and atomically switches the active pointer. Any failure rolls back wholesale;
the old active generation stays readable before and after the commit.

``ReadinessGate`` is the only entry point research reads may use: it resolves
the active generation for a dataset/adjustment and validates the requested
window, data types and adjustment against that generation.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from datetime import date, datetime
from zoneinfo import ZoneInfo

from stock_manager.domain import (
    ActiveGeneration,
    AdjustmentMethod,
    CandidateGeneration,
    CandidateGenerationStatus,
    GenerationPartition,
    PublishedGeneration,
    ReadinessResult,
    ReadinessStatus,
)

#: Data types the committer understands (must match planner/verifier order).
DATA_TYPE_ORDER: tuple[str, ...] = ("stocks", "daily_bars", "fundamentals", "dividends")

#: staging table -> (published table, column list without batch_id).
_STAGING_TO_PUBLISHED: dict[str, tuple[str, str]] = {
    "stocks": (
        "stocks",
        "code, as_of, name, exchange, is_st, listed_on, delisted_on, batch_id",
    ),
    "daily_bars": (
        "daily_bars",
        "code, trading_day, adjustment, open, high, low, close, preclose, "
        "volume, amount, is_trading, batch_id",
    ),
    "fundamentals": (
        "fundamentals",
        "code, report_date, published_on, pe_ttm, pb, source, batch_id",
    ),
    "dividends": (
        "dividends",
        "code, ex_date, cash_dividend_per_share, source, batch_id",
    ),
}


class PublishError(RuntimeError):
    """Raised when a candidate cannot be published."""


class ReadGateError(RuntimeError):
    """Raised when a read request is malformed."""


class GenerationCommitter:
    """Publishes a verified candidate atomically (P5 section 6.4)."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._connection_factory = connection_factory
        self._now = now

    def publish(
        self,
        candidate: CandidateGeneration,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        verifications: Sequence[object],
        partitions: Sequence[GenerationPartition],
    ) -> PublishedGeneration:
        """Publish a VERIFIED candidate in one transaction.

        ``verifications`` must be the persisted coverage records with
        ``status COMPLETE`` for every required partition; ``partitions`` maps
        each partition to its batch. The commit re-reads the candidate and
        verification rows inside the transaction and aborts on any mismatch.
        """
        if candidate.status is not CandidateGenerationStatus.VERIFIED:
            raise PublishError(
                f"candidate {candidate.candidate_generation_id} is "
                f"{candidate.status.value}, not VERIFIED"
            )
        required_types = {p.data_type for p in partitions}
        for verification in verifications:
            if verification.status.value != "COMPLETE":
                raise PublishError(
                    f"verification for {verification.data_type}/"
                    f"{verification.partition_key} is not COMPLETE"
                )
        missing_types = required_types - {
            v.data_type for v in verifications
        }
        if missing_types:
            raise PublishError(
                f"missing verifications for types: {sorted(missing_types)}"
            )
        if not partitions:
            raise PublishError("cannot publish a candidate with no partitions")
        if any(p.generation != candidate.candidate_generation_id for p in partitions):
            raise PublishError("partition generation must match the candidate id")
        now = self._now()
        manifest_sha256 = self._manifest_digest(partitions)
        with self._connection_factory() as connection:
            try:
                self._recheck_inside_txn(
                    connection, candidate, verifications, partitions
                )
                connection.execute(
                    """UPDATE candidate_generations SET status = ?, updated_at = ?
                       WHERE candidate_generation_id = ?""",
                    (
                        CandidateGenerationStatus.PUBLISHED.value,
                        now.isoformat(),
                        candidate.candidate_generation_id,
                    ),
                )
                for partition in partitions:
                    connection.execute(
                        """INSERT OR REPLACE INTO generation_partitions
                           (generation, data_type, partition_key, batch_id)
                           VALUES (?, ?, ?, ?)""",
                        (
                            partition.generation,
                            partition.data_type,
                            partition.partition_key,
                            partition.batch_id,
                        ),
                    )
                    self._copy_staging_to_published(
                        connection, partition
                    )
                connection.execute(
                    """INSERT OR REPLACE INTO dataset_versions
                       (dataset_id, generation, source, adjustment, created_at,
                        status, coverage_start, coverage_end, manifest_sha256,
                        parent_generation)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        dataset_id,
                        candidate.candidate_generation_id,
                        "published",
                        adjustment.value,
                        now.isoformat(),
                        CandidateGenerationStatus.PUBLISHED.value,
                        None,
                        None,
                        manifest_sha256,
                        candidate.parent_generation,
                    ),
                )
                connection.execute(
                    """INSERT OR REPLACE INTO active_generations
                       (dataset_id, adjustment, generation, activated_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        dataset_id,
                        adjustment.value,
                        candidate.candidate_generation_id,
                        now.isoformat(),
                    ),
                )
                connection.commit()
            except sqlite3.Error as error:
                connection.rollback()
                raise PublishError(
                    f"publish failed for candidate "
                    f"{candidate.candidate_generation_id}: {error}"
                ) from error
        return PublishedGeneration(
            generation=candidate.candidate_generation_id,
            dataset_id=dataset_id,
            adjustment=adjustment,
            parent_generation=candidate.parent_generation,
            manifest_sha256=manifest_sha256,
            published_at=now,
            status=CandidateGenerationStatus.PUBLISHED,
        )

    @staticmethod
    def _copy_staging_to_published(
        connection: sqlite3.Connection, partition: GenerationPartition
    ) -> None:
        """Materialize one partition's staged rows into the published table.

        Runs inside the publish transaction so readers either see the old
        generation entirely or the new one entirely, never a mix.
        """
        staging = _STAGING_TO_PUBLISHED.get(partition.data_type)
        if staging is None:
            return
        published, columns = staging
        connection.execute(
            f"""INSERT OR REPLACE INTO {published} ({columns})
                SELECT {columns} FROM {published}_staging
                WHERE batch_id = ?""",
            (partition.batch_id,),
        )
        connection.execute(
            f"DELETE FROM {published}_staging WHERE batch_id = ?",
            (partition.batch_id,),
        )

    def _recheck_inside_txn(
        self,
        connection: sqlite3.Connection,
        candidate: CandidateGeneration,
        verifications: Sequence[object],
        partitions: Sequence[GenerationPartition],
    ) -> None:
        """Re-verify state inside the transaction (plan section 6.3/6.4)."""
        row = connection.execute(
            "SELECT status, write_revision FROM candidate_generations "
            "WHERE candidate_generation_id = ?",
            (candidate.candidate_generation_id,),
        ).fetchone()
        if row is None:
            raise PublishError("candidate row missing inside transaction")
        if row["status"] != CandidateGenerationStatus.VERIFIED.value:
            raise PublishError(
                f"candidate status changed to {row['status']} before publish"
            )
        if int(row["write_revision"]) != candidate.write_revision:
            raise PublishError(
                "candidate write_revision changed after verification; "
                "verification is invalidated"
            )
        for verification in verifications:
            check = connection.execute(
                """SELECT status, verified_revision, manifest_sha256
                   FROM coverage_verifications
                   WHERE candidate_generation_id = ? AND data_type = ?
                     AND partition_key = ?""",
                (
                    verification.candidate_generation_id,
                    verification.data_type,
                    verification.partition_key,
                ),
            ).fetchone()
            if check is None:
                raise PublishError("verification row missing inside transaction")
            if check["status"] != "COMPLETE":
                raise PublishError("verification not COMPLETE inside transaction")
            if int(check["verified_revision"]) != candidate.write_revision:
                raise PublishError("verification revision mismatch inside transaction")

    @staticmethod
    def _manifest_digest(
        partitions: Sequence[GenerationPartition],
    ) -> str:
        digest = hashlib.sha256()
        for partition in sorted(
            partitions,
            key=lambda p: (p.data_type, p.partition_key, p.batch_id),
        ):
            digest.update(
                "|".join(
                    (
                        partition.generation,
                        partition.data_type,
                        partition.partition_key,
                        partition.batch_id,
                    )
                ).encode("utf-8")
            )
        return digest.hexdigest()


class ReadinessGate:
    """Resolves the readable generation for research reads (P5 section 8)."""

    def __init__(
        self,
        connection_factory: Callable[[], sqlite3.Connection],
    ) -> None:
        self._connection_factory = connection_factory

    def evaluate(
        self,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
        required_data_types: Sequence[str],
        requested_start: date,
        requested_end: date,
    ) -> ReadinessResult:
        """Return READY only when the active generation covers the request."""
        if requested_start > requested_end:
            raise ReadGateError("requested_start must not be after requested_end")
        if not required_data_types:
            raise ReadGateError("required_data_types must not be empty")
        with self._connection_factory() as connection:
            active = connection.execute(
                """SELECT generation, activated_at FROM active_generations
                   WHERE dataset_id = ? AND adjustment = ?""",
                (dataset_id, adjustment.value),
            ).fetchone()
            if active is None:
                # 是否存在其它复权口径的 active generation?是 → ADJUSTMENT_MISMATCH。
                other = connection.execute(
                    """SELECT adjustment FROM active_generations
                       WHERE dataset_id = ? LIMIT 1""",
                    (dataset_id,),
                ).fetchone()
                if other is not None:
                    return ReadinessResult(
                        status=ReadinessStatus.ADJUSTMENT_MISMATCH,
                        dataset_id=dataset_id,
                        adjustment=adjustment,
                        generation=None,
                        reason=(
                            f"active generation exists for adjustment "
                            f"{other['adjustment']} but not for {adjustment.value}"
                        ),
                    )
                return ReadinessResult(
                    status=ReadinessStatus.NO_GENERATION,
                    dataset_id=dataset_id,
                    adjustment=adjustment,
                    generation=None,
                    reason="no active generation published for this dataset/adjustment",
                )
            generation = str(active["generation"])
            partitions = connection.execute(
                """SELECT data_type, partition_key FROM generation_partitions
                   WHERE generation = ?""",
                (generation,),
            ).fetchall()
            if not partitions:
                return ReadinessResult(
                    status=ReadinessStatus.INCOMPLETE,
                    dataset_id=dataset_id,
                    adjustment=adjustment,
                    generation=None,
                    reason=f"active generation {generation} has no partitions",
                )
            covered_types = {row["data_type"] for row in partitions}
            missing_types = set(required_data_types) - covered_types
            if missing_types:
                return ReadinessResult(
                    status=ReadinessStatus.MISSING_DATA_TYPE,
                    dataset_id=dataset_id,
                    adjustment=adjustment,
                    generation=None,
                    reason=(
                        f"active generation {generation} lacks data types: "
                        f"{sorted(missing_types)}"
                    ),
                )
            keys = [
                date.fromisoformat(row["partition_key"].split(":")[0])
                for row in partitions
            ]
            if keys:
                covered_start = min(keys)
                covered_end = max(keys)
                if requested_start < covered_start or requested_end > covered_end:
                    return ReadinessResult(
                        status=ReadinessStatus.OUT_OF_RANGE,
                        dataset_id=dataset_id,
                        adjustment=adjustment,
                        generation=None,
                        reason=(
                            f"requested {requested_start}..{requested_end} outside "
                            f"active generation {generation} coverage "
                            f"{covered_start}..{covered_end}"
                        ),
                    )
        return ReadinessResult(
            status=ReadinessStatus.READY,
            dataset_id=dataset_id,
            adjustment=adjustment,
            generation=generation,
            reason=None,
        )
