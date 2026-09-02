"""Offline tests for P5-RD-7: seed SHA-256, manifests, provenance, transfer."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from stock_manager.storage import SQLiteRepository
from stock_manager.storage.migrations import CURRENT_SCHEMA_VERSION
from stock_manager.sync.seed import (
    REQUIRED_MANIFEST_FIELDS,
    SeedManifest,
    SeedPackageVerifier,
    SeedVerificationError,
    TransferError,
    TransferPreparer,
    stream_sha256,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def _seed_db(path: Path) -> None:
    repo = SQLiteRepository(path)
    repo = None  # noqa: F841


def _manifest_for(
    path: Path,
    *,
    filename: str | None = None,
    sha: str | None = None,
    schema_version: int = CURRENT_SCHEMA_VERSION,
    **overrides: object,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "filename": filename or path.name,
        "sha256": sha or stream_sha256(path),
        "schema_version": schema_version,
        "source": "baostock",
        "source_generation": "seed-2026-08-31",
        "coverage_start": "2018-09-01",
        "coverage_end": "2026-08-31",
        "adjustment": "qfq",
        "created_at": "2026-09-01T12:00:00+08:00",
    }
    manifest.update(overrides)
    return manifest


class TestStreamSha256:
    def test_matches_hashlib(self, tmp_path: Path) -> None:
        path = tmp_path / "blob.bin"
        path.write_bytes(b"x" * (2 * 1024 * 1024 + 17))
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert stream_sha256(path) == expected

    def test_progress_callback(self, tmp_path: Path) -> None:
        path = tmp_path / "blob.bin"
        path.write_bytes(b"y" * 1000)
        events: list[tuple[int, int]] = []
        stream_sha256(path, progress=lambda done, total: events.append((done, total)))
        assert events
        assert events[-1] == (1000, 1000)


class TestSeedManifest:
    def test_load_valid(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        _seed_db(path)
        manifest_path = tmp_path / "seed.sqlite3.manifest.json"
        manifest_path.write_text(
            json.dumps(_manifest_for(path)), encoding="utf-8"
        )
        manifest = SeedManifest.load(manifest_path)
        assert manifest.filename == path.name
        assert manifest.schema_version == CURRENT_SCHEMA_VERSION

    def test_missing_fields_rejected(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.json"
        manifest_path.write_text(json.dumps({"filename": "x"}), encoding="utf-8")
        with pytest.raises(SeedVerificationError):
            SeedManifest.load(manifest_path)

    def test_bad_json_rejected(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.json"
        manifest_path.write_text("{not json", encoding="utf-8")
        with pytest.raises(SeedVerificationError):
            SeedManifest.load(manifest_path)

    def test_non_object_rejected(self, tmp_path: Path) -> None:
        manifest_path = tmp_path / "m.json"
        manifest_path.write_text("[1,2]", encoding="utf-8")
        with pytest.raises(SeedVerificationError):
            SeedManifest.load(manifest_path)


class TestSeedPackageVerifier:
    def test_valid_seed_passes(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        _seed_db(path)
        verifier = SeedPackageVerifier()
        verifier.verify(
            path,
            SeedManifest.load(_write_manifest(tmp_path, path)),
            expected_schema_version=CURRENT_SCHEMA_VERSION,
        )

    def test_sha_mismatch_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        _seed_db(path)
        verifier = SeedPackageVerifier()
        with pytest.raises(SeedVerificationError) as exc:
            verifier.verify(
                path,
                SeedManifest.load(_write_manifest(tmp_path, path, sha="0" * 64)),
                expected_schema_version=CURRENT_SCHEMA_VERSION,
            )
        assert "SHA-256 mismatch" in str(exc.value)

    def test_filename_mismatch_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        _seed_db(path)
        verifier = SeedPackageVerifier()
        with pytest.raises(SeedVerificationError):
            verifier.verify(
                path,
                SeedManifest.load(
                    _write_manifest(tmp_path, path, filename="other.sqlite3")
                ),
                expected_schema_version=CURRENT_SCHEMA_VERSION,
            )

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.sqlite3"
        # 文件不存在时不能计算其哈希;直接用占位 manifest。
        manifest_path = tmp_path / "missing.sqlite3.manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "filename": path.name,
                    "sha256": "0" * 64,
                    "schema_version": CURRENT_SCHEMA_VERSION,
                    "source": "baostock",
                    "source_generation": "seed-2026-08-31",
                    "adjustment": "qfq",
                    "created_at": "2026-09-01T12:00:00+08:00",
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(SeedVerificationError):
            SeedPackageVerifier().verify(
                path,
                SeedManifest.load(manifest_path),
                expected_schema_version=CURRENT_SCHEMA_VERSION,
            )

    def test_corrupt_sqlite_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "corrupt.sqlite3"
        path.write_bytes(b"this is not a sqlite file at all......")
        verifier = SeedPackageVerifier()
        with pytest.raises(SeedVerificationError):
            verifier.verify(
                path,
                SeedManifest.load(_write_manifest(tmp_path, path)),
                expected_schema_version=CURRENT_SCHEMA_VERSION,
            )

    def test_schema_version_mismatch_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        _seed_db(path)
        verifier = SeedPackageVerifier()
        with pytest.raises(SeedVerificationError) as exc:
            verifier.verify(
                path,
                SeedManifest.load(_write_manifest(tmp_path, path)),
                expected_schema_version=99,
            )
        assert "schema version" in str(exc.value)

    def test_truncated_file_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "seed.sqlite3"
        _seed_db(path)
        truncated = tmp_path / "truncated.sqlite3"
        data = path.read_bytes()
        truncated.write_bytes(data[: len(data) // 2])
        verifier = SeedPackageVerifier()
        with pytest.raises(SeedVerificationError):
            verifier.verify(
                truncated,
                SeedManifest.load(_write_manifest(tmp_path, truncated)),
                expected_schema_version=CURRENT_SCHEMA_VERSION,
            )


class TestTransferPreparer:
    def test_prepare_and_verify_roundtrip(self, tmp_path: Path) -> None:
        source = tmp_path / "market.sqlite3"
        _seed_db(source)
        preparer = TransferPreparer(now=lambda: NOW, expected_schema_version=CURRENT_SCHEMA_VERSION)
        out = tmp_path / "transfer"
        sidecar = preparer.prepare(source, out)
        assert sidecar.is_file()
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        assert manifest["sha256"] == stream_sha256(source)
        # 模拟复制到不同根目录
        copied = tmp_path / "windows" / "market.sqlite3"
        copied.parent.mkdir(parents=True)
        copied.write_bytes(source.read_bytes())
        preparer.verify_transfer(copied, sidecar)

    def test_verify_after_modification_fails(self, tmp_path: Path) -> None:
        source = tmp_path / "market.sqlite3"
        _seed_db(source)
        preparer = TransferPreparer(now=lambda: NOW, expected_schema_version=CURRENT_SCHEMA_VERSION)
        sidecar = preparer.prepare(source, tmp_path / "out")
        # 复制后修改一个字节 → SHA 变化 → 校验失败
        copied = tmp_path / "modified.sqlite3"
        data = bytearray(source.read_bytes())
        data[100] ^= 0xFF
        copied.write_bytes(bytes(data))
        with pytest.raises(TransferError):
            preparer.verify_transfer(copied, sidecar)

    def test_missing_transferred_file_fails(self, tmp_path: Path) -> None:
        source = tmp_path / "market.sqlite3"
        _seed_db(source)
        preparer = TransferPreparer(now=lambda: NOW, expected_schema_version=CURRENT_SCHEMA_VERSION)
        sidecar = preparer.prepare(source, tmp_path / "out")
        with pytest.raises(TransferError):
            preparer.verify_transfer(tmp_path / "nope.sqlite3", sidecar)

    def test_missing_manifest_field_fails(self, tmp_path: Path) -> None:
        source = tmp_path / "market.sqlite3"
        _seed_db(source)
        preparer = TransferPreparer(now=lambda: NOW, expected_schema_version=CURRENT_SCHEMA_VERSION)
        sidecar = preparer.prepare(source, tmp_path / "out")
        bad = tmp_path / "bad.transfer.json"
        manifest = json.loads(sidecar.read_text(encoding="utf-8"))
        del manifest["sha256"]
        bad.write_text(json.dumps(manifest), encoding="utf-8")
        copied = tmp_path / "copied.sqlite3"
        copied.write_bytes(source.read_bytes())
        with pytest.raises(TransferError):
            preparer.verify_transfer(copied, bad)

    def test_schema_mismatch_on_receiver(self, tmp_path: Path) -> None:
        source = tmp_path / "market.sqlite3"
        _seed_db(source)
        with sqlite3.connect(source) as connection:
            connection.execute("PRAGMA user_version = 5")
        preparer = TransferPreparer(now=lambda: NOW, expected_schema_version=CURRENT_SCHEMA_VERSION)
        sidecar = preparer.prepare(source, tmp_path / "out")
        copied = tmp_path / "copied.sqlite3"
        copied.write_bytes(source.read_bytes())
        with pytest.raises(TransferError):
            preparer.verify_transfer(copied, sidecar)


def _write_manifest(
    tmp_path: Path, path: Path, **overrides: object
) -> Path:
    manifest_path = tmp_path / f"{path.name}.manifest.json"
    manifest_path.write_text(
        json.dumps(_manifest_for(path, **overrides)), encoding="utf-8"
    )
    return manifest_path
