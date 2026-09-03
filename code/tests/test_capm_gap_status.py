"""Offline regression tests for honest, generation-bound CAPM gap status."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import DepositRate, IndexDailyBar, IndexIdentity, IndexReturnVersion
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync.data_sync_service import DataSyncService, SyncConfig


START = date(2026, 9, 1)
END = date(2026, 9, 7)
DAYS = (START, date(2026, 9, 2), date(2026, 9, 3), date(2026, 9, 4), END)
NOW = datetime(2026, 9, 7, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


class CompleteReferenceFixture:
    source_name = "baostock"

    def fetch_trading_days(self, start: date, end: date) -> Sequence[date]:
        return tuple(start + timedelta(days=i) for i in range((end - start).days + 1)
                     if (start + timedelta(days=i)).weekday() < 5)

    def fetch_indexes(self, as_of: date) -> Sequence[IndexIdentity]:
        return tuple(IndexIdentity(index_id, code, index_id, "broad", IndexReturnVersion.PRICE,
                                   "baostock") for index_id, code in (
            ("hs300.price", "sh.000300"), ("partial.price", "sh.000940"),
            ("empty.price", "sh.000951"),
        ))

    def fetch_deposit_rates(self) -> Sequence[DepositRate]:
        return (DepositRate("1_year", date(2015, 10, 24), Decimal("0.015"), "baostock"),)

    def fetch_index_daily_bars(self, indexes: Sequence[IndexIdentity], start: date,
                               end: date) -> Sequence[IndexDailyBar]:
        return tuple(IndexDailyBar(index.index_id, day, Decimal(100 + i), index.return_version)
                     for index in indexes for i, day in enumerate(DAYS) if start <= day <= end)


def published_repository(tmp_path: Path) -> SQLiteRepository:
    repo = SQLiteRepository(tmp_path / "capm.sqlite3")
    service = DataSyncService(CompleteReferenceFixture(), repo, tmp_path / "locks",
                             SyncConfig(time(17, 30), timedelta(seconds=300), 0, 45, 3),
                             clock=lambda: NOW)
    assert service.sync_capm_reference_data(START, END).published
    return repo


def record_gaps(repo: SQLiteRepository, index_id: str, missing: Sequence[date],
                status: str = "ACCEPTED_WITH_GAPS") -> None:
    """Model already-published source evidence without requesting any network."""
    with sqlite3.connect(repo.database_path) as connection:
        connection.executemany("DELETE FROM index_daily_bars WHERE index_id=? AND trading_day=?",
                               [(index_id, day.isoformat()) for day in missing])
        details = {"codes": [index_id], "availability": "SOURCE_GAPS",
                   "acceptance_policy": "source_available_v1",
                   "unavailable_ranges": [[str(day), str(day)] for day in missing]}
        connection.execute(
            """UPDATE coverage_verifications SET expected_count=?, actual_count=?,
               distinct_count=?, coverage_ratio=?, missing_items=?, status=?, details_json=?
               WHERE data_type='index_daily_bars' AND json_extract(details_json, '$.codes[0]')=?""",
            (len(DAYS), len(DAYS) - len(missing), len(DAYS) - len(missing),
             str(Decimal(len(DAYS) - len(missing)) / Decimal(len(DAYS))),
             ",".join(str(day) for day in missing), status, json.dumps(details), index_id),
        )


def test_unsynced_status_does_not_publish_or_count_legacy_rows(tmp_path: Path) -> None:
    repo = SQLiteRepository(tmp_path / "empty.sqlite3")
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""INSERT INTO index_catalog
            (index_id, provider_code, name, category, return_version, source)
            VALUES ('legacy.price','sh.000940','legacy','broad','price_return','baostock')""")
    status = repo.capm_data_status()
    assert status["generation"] is None
    assert status["coverage_status"] == "NOT_SYNCED"
    assert status["index_count"] == status["bar_count"] == status["rate_count"] == 0
    assert status["gap_index_count"] == status["missing_count"] == 0
    assert status["has_effective_one_year_rate"] is False
    assert status["indexes"] == []


def test_status_reports_actual_missing_dates_and_ratio_without_hiding_gaps(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", DAYS[1:3])
    record_gaps(repo, "empty.price", DAYS)
    status = repo.capm_data_status()
    by_id = {item["index_id"]: item for item in status["indexes"]}
    assert status["coverage_status"] == "PARTIAL"
    assert status["gap_index_count"] == 2
    assert status["missing_count"] == 7
    assert status["bar_count"] == 8
    assert by_id["hs300.price"]["coverage_status"] == "COMPLETE"
    assert by_id["hs300.price"]["coverage_ratio"] == "1"
    partial = by_id["partial.price"]
    assert partial["expected_count"] == 5
    assert partial["missing_count"] == 2
    assert partial["missing_dates"] == [str(day) for day in DAYS[1:3]]
    assert partial["coverage_ratio"] == "0.6"
    assert partial["coverage_status"] == "PARTIAL"
    assert by_id["empty.price"]["coverage_status"] == "UNAVAILABLE"
    assert by_id["empty.price"]["coverage_ratio"] == "0"
    assert by_id["empty.price"]["missing_dates"] == [str(day) for day in DAYS]
    assert repo.capm_coverage_intact(START, END)


@pytest.mark.parametrize("evidence_status", ["COMPLETE", "ACCEPTED_WITH_GAPS"])
def test_gap_evidence_accepts_legacy_and_new_status(tmp_path: Path, evidence_status: str) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", (DAYS[1],), evidence_status)
    assert repo.capm_unavailable_ranges("partial.price") == ((str(DAYS[1]), str(DAYS[1])),)


def test_gap_evidence_is_bound_to_exact_active_batch_partition(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", (DAYS[1],), "COMPLETE")
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""UPDATE coverage_verifications SET partition_key='unrelated-partition'
            WHERE data_type='index_daily_bars' AND json_extract(details_json, '$.codes[0]')='partial.price'""")
    assert repo.capm_unavailable_ranges("partial.price") == ()
    assert not repo.capm_coverage_intact(START, END)


def test_gap_evidence_excludes_batch_removed_from_active_manifest(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", (DAYS[1],), "COMPLETE")
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""DELETE FROM generation_partitions WHERE batch_id IN
            (SELECT batch_id FROM ingest_batches WHERE codes='partial.price')""")
    assert repo.capm_unavailable_ranges("partial.price") == ()


def test_accepted_gap_evidence_requires_recognized_acceptance_policy(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", (DAYS[1],))
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""UPDATE coverage_verifications
            SET details_json=json_remove(details_json, '$.acceptance_policy')
            WHERE data_type='index_daily_bars' AND json_extract(details_json, '$.codes[0]')='partial.price'""")
    assert repo.capm_unavailable_ranges("partial.price") == ()
    assert not repo.capm_coverage_intact(START, END)


def test_local_deleted_bar_is_not_excused_by_other_accepted_gaps(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", (DAYS[1],))
    assert repo.capm_coverage_intact(START, END)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("DELETE FROM index_daily_bars WHERE index_id='partial.price' AND trading_day=?",
                           (str(DAYS[2]),))
    assert not repo.capm_coverage_intact(START, END)
    partial = next(item for item in repo.capm_data_status()["indexes"] if item["index_id"] == "partial.price")
    assert partial["missing_dates"] == [str(DAYS[1]), str(DAYS[2])]


def test_status_and_integrity_ignore_bars_outside_active_manifest(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""UPDATE index_daily_bars SET batch_id='unpublished-batch'
            WHERE index_id='partial.price' AND trading_day=?""", (str(DAYS[2]),))
    assert not repo.capm_coverage_intact(START, END)
    partial = next(item for item in repo.capm_data_status()["indexes"] if item["index_id"] == "partial.price")
    assert partial["bar_count"] == 4
    assert partial["missing_dates"] == [str(DAYS[2])]


def test_status_ignores_catalog_and_rates_outside_active_manifest(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("UPDATE index_catalog SET batch_id='unpublished' WHERE index_id='empty.price'")
        connection.execute("UPDATE deposit_rates SET batch_id='unpublished'")
    status = repo.capm_data_status()
    assert status["index_count"] == 2
    assert status["bar_count"] == 10
    assert status["rate_count"] == 0
    assert status["rate_start"] is None
    assert status["rate_end"] is None
    assert not repo.capm_coverage_intact(START, END)


@pytest.mark.parametrize("year_rate_state", ["missing", "late", "unpublished"])
def test_other_rate_terms_cannot_replace_effective_published_one_year_rate(
    tmp_path: Path, year_rate_state: str,
) -> None:
    repo = published_repository(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""INSERT INTO deposit_rates
            (term, effective_on, annual_rate, source, batch_id)
            SELECT '3_month', effective_on, annual_rate, source, batch_id
            FROM deposit_rates WHERE term='1_year'""")
        if year_rate_state == "missing":
            connection.execute("DELETE FROM deposit_rates WHERE term='1_year'")
        elif year_rate_state == "late":
            connection.execute("UPDATE deposit_rates SET effective_on=? WHERE term='1_year'", (str(END),))
        else:
            connection.execute("UPDATE deposit_rates SET batch_id='unpublished' WHERE term='1_year'")
    assert repo.capm_data_status()["rate_count"] > 0
    assert not repo.capm_coverage_intact(START, END)
    assert repo.capm_data_status()["has_effective_one_year_rate"] is False


def test_effective_one_year_rate_uses_requested_start_with_inclusive_boundary(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("UPDATE deposit_rates SET effective_on=? WHERE term='1_year'", (str(DAYS[1]),))
    assert not repo.capm_coverage_intact(START, END)
    assert repo.capm_coverage_intact(DAYS[1], END)


def test_expected_window_comes_from_active_plan_not_observed_bars(tmp_path: Path) -> None:
    repo = published_repository(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""UPDATE sync_plans SET target_start='2026-09-02', target_end='2026-09-04'
            WHERE candidate_generation_id IN (SELECT generation FROM active_generations WHERE dataset_id='capm')""")
    status = repo.capm_data_status()
    assert status["coverage_status"] == "COMPLETE"
    assert all(item["expected_count"] == 3 for item in status["indexes"])
    assert all(item["coverage_start"] == "2026-09-02" for item in status["indexes"])
    assert all(item["coverage_end"] == "2026-09-04" for item in status["indexes"])


@pytest.mark.parametrize("evidence_status,invalid_count", [("INCOMPLETE", 0), ("COMPLETE", 1)])
def test_failed_or_invalid_evidence_cannot_excuse_gaps(
    tmp_path: Path, evidence_status: str, invalid_count: int,
) -> None:
    repo = published_repository(tmp_path)
    record_gaps(repo, "partial.price", (DAYS[1],), evidence_status)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("""UPDATE coverage_verifications SET invalid_count=?
            WHERE data_type='index_daily_bars' AND json_extract(details_json, '$.codes[0]')='partial.price'""",
            (invalid_count,))
    assert repo.capm_unavailable_ranges("partial.price") == ()
    assert not repo.capm_coverage_intact(START, END)
