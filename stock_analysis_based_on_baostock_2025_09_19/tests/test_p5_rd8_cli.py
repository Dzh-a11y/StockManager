"""Offline tests for P5-RD-8: CLI sync control and visible state."""

from __future__ import annotations

import io
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.cli.main import main
from stock_manager.domain import (
    AdjustmentMethod,
    DailyBar,
    DatasetMetadata,
    FundamentalSnapshot,
    StockIdentity,
    SyncRecord,
    SyncStatus,
)
from stock_manager.storage import SQLiteRepository

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)


def _populate(repo: SQLiteRepository) -> None:
    stocks = tuple(
        StockIdentity(c, "股票", "SSE", False, None, None)
        for c in ("sh.600000", "sz.000001")
    )
    bars = tuple(
        DailyBar(
            c, DAY, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10.5"),
            Decimal("10"), Decimal("1000"), Decimal("10500"), True,
        )
        for c in ("sh.600000", "sz.000001")
    )
    funds = tuple(
        FundamentalSnapshot(c, DAY, DAY, Decimal("8"), Decimal("1"), "legacy")
        for c in ("sh.600000", "sz.000001")
    )
    metadata = DatasetMetadata("market", DAY, "legacy", NOW, AdjustmentMethod.QFQ)
    record = SyncRecord(
        "market", DAY, SyncStatus.SUCCESS, "legacy", AdjustmentMethod.QFQ,
        NOW, NOW, None,
    )
    repo.save_market_snapshot(stocks, bars, funds, (), (DAY,), metadata, record)


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    return SQLiteRepository(tmp_path / "market.sqlite3")


class TestSyncPlanCommand:
    def test_bootstrap_plan(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        code, out, err = _run(
            [
                "sync-plan",
                "--db", str(repo.database_path),
                "--mode", "BOOTSTRAP",
                "--start", DAY.isoformat(),
                "--end", DAY.isoformat(),
                "--adjustment", "qfq",
                "--data-types", "daily_bars",
            ]
        )
        assert code == 0, err
        payload = json.loads(out)
        assert payload["plan"]["mode"] == "BOOTSTRAP"
        assert payload["task_count"] == 1
        assert payload["tasks"][0]["data_type"] == "daily_bars"

    def test_legacy_import_plan(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        code, out, err = _run(
            [
                "sync-plan",
                "--db", str(repo.database_path),
                "--mode", "LEGACY_IMPORT",
                "--start", DAY.isoformat(),
                "--end", DAY.isoformat(),
                "--adjustment", "qfq",
                "--data-types", "daily_bars",
            ]
        )
        assert code == 0, err
        payload = json.loads(out)
        assert payload["plan"]["mode"] == "LEGACY_IMPORT"

    def test_missing_database_fails(self, tmp_path: Path) -> None:
        code, out, err = _run(
            [
                "sync-plan",
                "--db", str(tmp_path / "nope.sqlite3"),
                "--start", DAY.isoformat(),
                "--end", DAY.isoformat(),
                "--adjustment", "qfq",
            ]
        )
        assert code == 1
        assert json.loads(err)["error"] == "ValueError"


class TestSyncImportLegacyCommand:
    def test_import_creates_candidate(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        code, out, err = _run(
            [
                "sync-import-legacy",
                "--db", str(repo.database_path),
                "--adjustment", "qfq",
                "--date", DAY.isoformat(),
            ]
        )
        assert code == 0, err
        payload = json.loads(out)
        assert payload["candidate"]["status"] == "VERIFYING"
        # dividends 为空 → 3 个分区(stocks/daily_bars/fundamentals)
        assert len(payload["partitions"]) == 3
        loaded = repo.get_candidate_generation(payload["candidate"]["candidate_generation_id"])
        assert loaded is not None

    def test_import_keeps_legacy_rows(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        _run(
            [
                "sync-import-legacy",
                "--db", str(repo.database_path),
                "--adjustment", "qfq",
                "--date", DAY.isoformat(),
            ]
        )
        # 旧表行数不变
        import sqlite3

        with sqlite3.connect(repo.database_path) as connection:
            bars = connection.execute("SELECT COUNT(*) FROM daily_bars").fetchone()[0]
            staging = connection.execute(
                "SELECT COUNT(*) FROM daily_bars_staging"
            ).fetchone()[0]
        assert bars == 2
        assert staging == 2


class TestSyncVerifyCommand:
    def test_verify_imported_candidate(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        code, out, err = _run(
            [
                "sync-import-legacy",
                "--db", str(repo.database_path),
                "--adjustment", "qfq",
                "--date", DAY.isoformat(),
            ]
        )
        assert code == 0, err
        candidate_id = json.loads(out)["candidate"]["candidate_generation_id"]
        code, out, err = _run(
            [
                "sync-verify",
                "--db", str(repo.database_path),
                "--candidate", candidate_id,
                "--adjustment", "qfq",
                "--start", DAY.isoformat(),
                "--end", DAY.isoformat(),
            ]
        )
        assert code == 0, err
        payload = json.loads(out)
        # 没有任务 → 无验证记录,但报告存在
        assert payload["report"]["candidate_generation_id"] == candidate_id

    def test_verify_missing_candidate_fails(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        code, out, err = _run(
            [
                "sync-verify",
                "--db", str(repo.database_path),
                "--candidate", "nope",
                "--adjustment", "qfq",
                "--start", DAY.isoformat(),
                "--end", DAY.isoformat(),
            ]
        )
        assert code == 1
        assert "candidate not found" in json.loads(err)["message"]


class TestTransferCommands:
    def test_prepare_and_verify(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        out_dir = tmp_path / "out"
        code, out, err = _run(
            ["db-prepare-transfer", "--db", str(repo.database_path), "--out", str(out_dir)]
        )
        assert code == 0, err
        manifest = Path(json.loads(out)["manifest"])
        assert manifest.is_file()
        # 模拟复制到不同根目录(保持文件名,符合迁移契约)
        copied_dir = tmp_path / "windows"
        copied_dir.mkdir()
        copied = copied_dir / repo.database_path.name
        copied.write_bytes(repo.database_path.read_bytes())
        code, out, err = _run(
            ["db-verify-transfer", "--db", str(copied), "--manifest", str(manifest)]
        )
        assert code == 0, err
        assert json.loads(out)["verified"] is True

    def test_verify_tampered_fails(self, repo: SQLiteRepository, tmp_path: Path) -> None:
        _populate(repo)
        out_dir = tmp_path / "out"
        code, out, err = _run(
            ["db-prepare-transfer", "--db", str(repo.database_path), "--out", str(out_dir)]
        )
        assert code == 0, err
        manifest = Path(json.loads(out)["manifest"])
        tampered_dir = tmp_path / "tamper"
        tampered_dir.mkdir()
        tampered = tampered_dir / repo.database_path.name
        data = bytearray(repo.database_path.read_bytes())
        data[200] ^= 0xFF
        tampered.write_bytes(bytes(data))
        code, out, err = _run(
            ["db-verify-transfer", "--db", str(tampered), "--manifest", str(manifest)]
        )
        assert code == 1
        assert "SHA-256 mismatch" in json.loads(err)["message"]