"""Seed SHA-256, external manifests, provenance and WAL checkpoint (P5-RD-7).

The authoritative SHA-256 of a seed database lives in an *external* sidecar
manifest (writing the hash back into the database would change the file and
create a self-reference). ``SeedPackageVerifier`` validates a downloaded seed
against its manifest with streaming hashing; ``TransferPreparer`` checkpoints
WAL and produces the migration sidecar for Mac -> Windows physical copies.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

#: Manifest keys the verifier accepts (unknown keys are tolerated but these
#: are mandatory).
REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "filename",
    "sha256",
    "schema_version",
    "source",
    "source_generation",
    "adjustment",
    "created_at",
)


class SeedVerificationError(RuntimeError):
    """Raised when a seed package fails verification (REJECTED)."""


class TransferError(RuntimeError):
    """Raised when a physical transfer cannot be prepared/verified."""


@dataclass(frozen=True, slots=True)
class SeedManifest:
    """Validated external manifest of a seed database package."""

    filename: str
    sha256: str
    schema_version: int
    source: str
    source_generation: str
    coverage_start: str | None
    coverage_end: str | None
    adjustment: str
    created_at: str
    raw: dict[str, object]

    @classmethod
    def load(cls, manifest_path: Path) -> "SeedManifest":
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise SeedVerificationError(
                f"unable to read seed manifest {manifest_path}: {error}"
            ) from error
        if not isinstance(raw, dict):
            raise SeedVerificationError("seed manifest must be a JSON object")
        missing = [
            field for field in REQUIRED_MANIFEST_FIELDS if field not in raw
        ]
        if missing:
            raise SeedVerificationError(
                f"seed manifest missing required fields: {missing}"
            )
        for field in ("filename", "sha256", "source", "source_generation", "adjustment"):
            if not isinstance(raw[field], str) or not raw[field].strip():
                raise SeedVerificationError(f"manifest field {field} must be text")
        if not isinstance(raw["schema_version"], int):
            raise SeedVerificationError("manifest schema_version must be an integer")
        if not isinstance(raw["created_at"], str) or not raw["created_at"].strip():
            raise SeedVerificationError("manifest created_at must be text")
        return cls(
            filename=str(raw["filename"]),
            sha256=str(raw["sha256"]),
            schema_version=int(raw["schema_version"]),
            source=str(raw["source"]),
            source_generation=str(raw["source_generation"]),
            coverage_start=(
                str(raw["coverage_start"]) if raw.get("coverage_start") else None
            ),
            coverage_end=(
                str(raw["coverage_end"]) if raw.get("coverage_end") else None
            ),
            adjustment=str(raw["adjustment"]),
            created_at=str(raw["created_at"]),
            raw=raw,
        )


def stream_sha256(
    path: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    chunk_size: int = 1 << 20,
) -> str:
    """Compute a file's SHA-256 without loading it into memory.

    ``progress`` receives ``(bytes_done, total_bytes)`` so a GB-scale seed can
    show a live progress bar.
    """
    digest = hashlib.sha256()
    if not path.is_file():
        raise SeedVerificationError(f"file not found: {path}")
    total = path.stat().st_size
    done = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
            done += len(chunk)
            if progress is not None:
                progress(done, total)
    return digest.hexdigest()


class SeedPackageVerifier:
    """Verifies a downloaded seed database against its external manifest."""

    def verify(
        self,
        seed_path: Path,
        manifest: SeedManifest,
        *,
        expected_schema_version: int,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Raise SeedVerificationError unless the seed is trustworthy."""
        if not seed_path.is_file():
            raise SeedVerificationError(f"seed file not found: {seed_path}")
        if seed_path.name != manifest.filename:
            raise SeedVerificationError(
                f"seed filename {seed_path.name} does not match manifest "
                f"{manifest.filename}"
            )
        actual = stream_sha256(seed_path, progress=progress)
        if actual != manifest.sha256:
            raise SeedVerificationError(
                f"SHA-256 mismatch: expected {manifest.sha256}, got {actual}"
            )
        self.check_database(seed_path, expected_schema_version=expected_schema_version)

    @staticmethod
    def check_database(
        seed_path: Path,
        *,
        expected_schema_version: int,
    ) -> None:
        """Validate SQLite integrity and schema version (read-only)."""
        try:
            connection = sqlite3.connect(f"file:{seed_path}?mode=ro", uri=True)
        except sqlite3.Error as error:
            raise SeedVerificationError(
                f"unable to open seed database: {error}"
            ) from error
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise SeedVerificationError(
                    f"seed integrity_check failed: {row}"
                )
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version != expected_schema_version:
                raise SeedVerificationError(
                    f"seed schema version {version} does not match expected "
                    f"{expected_schema_version}"
                )
        except sqlite3.Error as error:
            raise SeedVerificationError(
                f"seed database check failed: {error}"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def check_no_absolute_paths(seed_path: Path) -> None:
        """Refuse a seed whose database rows embed absolute machine paths."""
        connection = sqlite3.connect(f"file:{seed_path}?mode=ro", uri=True)
        try:
            for table in (
                "dataset_metadata",
                "sync_runs",
                "backfill_runs_v2",
            ):
                rows = connection.execute(
                    f"SELECT * FROM {table} LIMIT 200"
                ).fetchall()
                for row in rows:
                    for value in row:
                        if (
                            isinstance(value, str)
                            and ("/Users/" in value or "C:\\Users" in value)
                        ):
                            raise SeedVerificationError(
                                f"seed contains an absolute path in {table}: {value}"
                            )
        finally:
            connection.close()


class TransferPreparer:
    """Prepares and verifies physical database transfers (P5 section 10)."""

    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        expected_schema_version: int,
    ) -> None:
        self._now = now
        self._expected_schema_version = expected_schema_version

    def prepare(
        self,
        database_path: Path,
        out_directory: Path,
    ) -> Path:
        """Checkpoint WAL, integrity-check, and write a transfer manifest.

        Returns the path to the generated ``*.transfer.json`` sidecar.
        """
        if not database_path.is_file():
            raise TransferError(f"database not found: {database_path}")
        connection = sqlite3.connect(database_path)
        try:
            checkpoint = connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is not None and checkpoint[0] == 1:
                raise TransferError("WAL checkpoint failed: database is busy")
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise TransferError("integrity_check failed before transfer")
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
        finally:
            connection.close()
        sha256 = stream_sha256(database_path)
        out_directory.mkdir(parents=True, exist_ok=True)
        manifest_path = out_directory / f"{database_path.name}.transfer.json"
        manifest = {
            "filename": database_path.name,
            "sha256": sha256,
            "schema_version": version,
            "generated_at": self._now().isoformat(),
            "note": (
                "SHA-256 proves the copy is identical; any later legal write "
                "changes the file hash. New state is proven by generation, "
                "manifest and coverage."
            ),
        }
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return manifest_path

    def verify_transfer(
        self,
        database_path: Path,
        manifest_path: Path,
    ) -> None:
        """Re-hash a transferred database and compare with its manifest."""
        if not database_path.is_file():
            raise TransferError(f"transferred database not found: {database_path}")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TransferError(f"unable to read transfer manifest: {error}") from error
        if manifest.get("filename") != database_path.name:
            raise TransferError(
                f"manifest filename {manifest.get('filename')} does not match "
                f"{database_path.name}"
            )
        expected = manifest.get("sha256")
        if not isinstance(expected, str) or not expected:
            raise TransferError("transfer manifest has no sha256")
        actual = stream_sha256(database_path)
        if actual != expected:
            raise TransferError(
                f"transferred database SHA-256 mismatch: expected {expected}, "
                f"got {actual}"
            )
        self.check_database(database_path)
        self.check_no_absolute_paths(database_path)

    def check_database(self, database_path: Path) -> None:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            if row is None or row[0] != "ok":
                raise TransferError("integrity_check failed on transferred database")
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version != self._expected_schema_version:
                raise TransferError(
                    f"transferred database schema {version} does not match "
                    f"expected {self._expected_schema_version}"
                )
        finally:
            connection.close()

    @staticmethod
    def check_no_absolute_paths(database_path: Path) -> None:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            for table in ("dataset_metadata", "sync_runs", "backfill_runs_v2"):
                rows = connection.execute(
                    f"SELECT * FROM {table} LIMIT 200"
                ).fetchall()
                for row in rows:
                    for value in row:
                        if (
                            isinstance(value, str)
                            and ("/Users/" in value or "C:\\Users" in value)
                        ):
                            raise TransferError(
                                f"transferred database contains an absolute path "
                                f"in {table}: {value}"
                            )
        finally:
            connection.close()
