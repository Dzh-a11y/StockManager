"""P5A-1 database integrity verification tests (offline)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from stock_manager.domain import AdjustmentMethod, DailyBar, StockIdentity
from stock_manager.storage import SQLiteRepository
from stock_manager.storage.integrity import verify_database_integrity


def _seed_repository(path: Path) -> None:
    repository = SQLiteRepository(path)
    stock = StockIdentity("000001.SZ", "平安银行", "SZSE", False, None, None)
    days = [date(2020, 1, 2) + timedelta(days=i) for i in range(10)]
    days = [d for d in days if d.weekday() < 5]
    from stock_manager.domain import FundamentalSnapshot

    metadata = repository.get_latest_dataset_metadata  # noqa: B018 (import check only)
    for day in days:
        bars = [
            DailyBar(
                "000001.SZ",
                day,
                Decimal("10"),
                Decimal("11"),
                Decimal("9"),
                Decimal("10.5"),
                Decimal("10"),
                Decimal("1000"),
                Decimal("10500"),
                True,
            )
        ]
        # 通过公共方法保存日线与交易日
        repository.save_trading_days(days, _metadata(repository, day))
        repository.save_daily_bars(bars, _metadata(repository, day))


def _metadata(repository: SQLiteRepository, day: date):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from stock_manager.domain import DatasetMetadata

    return DatasetMetadata(
        "market", day, "fixture", datetime(2026, 8, 25, 18, tzinfo=ZoneInfo("Asia/Shanghai")), AdjustmentMethod.QFQ
    )


def test_integrity_report_on_empty_database(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    SQLiteRepository(path)
    report = verify_database_integrity(path)
    assert report["integrity_check"] == "ok"
    assert report["size_bytes"] > 0
    assert report["row_counts"]["daily_bars"] == 0
    assert report["duplicate_daily_bar_keys"] == 0
    assert report["bar_coverage_by_adjustment"] == {}


def test_integrity_report_reflects_seeded_data(tmp_path: Path) -> None:
    path = tmp_path / "seeded.sqlite3"
    _seed_repository(path)
    report = verify_database_integrity(path)
    assert report["integrity_check"] == "ok"
    assert report["row_counts"]["daily_bars"] > 0
    assert report["duplicate_daily_bar_keys"] == 0
    coverage = report["bar_coverage_by_adjustment"]
    assert "qfq" in coverage
    assert coverage["qfq"]["codes"] == 1
    assert coverage["qfq"]["trading_days"] > 0
    assert report["calendar_gaps_sample"] == []
    assert report["per_code_boundaries_sample"]["000001.SZ"]["bars"] > 0


def test_integrity_report_never_writes(tmp_path: Path) -> None:
    path = tmp_path / "ro.sqlite3"
    _seed_repository(path)
    import sqlite3

    def _stable(report: dict) -> dict:
        return {k: v for k, v in report.items() if k != "verified_at"}

    before = verify_database_integrity(path)
    after = verify_database_integrity(path)
    assert _stable(before) == _stable(after)
    # 只读 URI 下写入必须失败
    pytest_raises_operational(path)


def pytest_raises_operational(path: Path) -> None:
    import sqlite3

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        conn.execute("CREATE TABLE should_fail (x INTEGER)")
    except sqlite3.OperationalError:
        return
    finally:
        conn.close()
    raise AssertionError("read-only connection accepted a write")
