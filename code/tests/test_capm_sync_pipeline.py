"""Offline reference-sync parity, isolation and publication regressions."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod, CandidateGeneration, DailyBar, DatasetMetadata, DepositRate,
    IndexDailyBar, IndexIdentity, IndexReturnVersion, IssueType, Repairability,
    StockIdentity, SyncPlanMode, SyncTask, VerificationIssue, VerificationStatus,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from stock_manager.sync.data_sync_service import DataSyncService, RetryRequiredError, SyncConfig
from stock_manager.sync.pipeline import PipelineError, RetryCooldownError
from stock_manager.sync.capm import CapmVerifier
from stock_manager.sync.verifier import VerificationOutcome

START = date(2026, 9, 1)
END = date(2026, 9, 2)
NOW = datetime(2026, 9, 2, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


class ReferenceProvider:
    source_name = "baostock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, date | None, date | None]] = []
        self.fail_rates = False
        self.missing_day: date | None = None

    def fetch_trading_days(self, start: date, end: date) -> Sequence[date]:
        self.calls.append(("calendar", start, end))
        return tuple(start + timedelta(days=i) for i in range((end-start).days+1)
                     if (start + timedelta(days=i)).weekday() < 5)

    def fetch_indexes(self, as_of: date) -> Sequence[IndexIdentity]:
        self.calls.append(("catalog", as_of, as_of))
        return (IndexIdentity("hs300.price", "sh.000300", "沪深300指数", "broad",
                              IndexReturnVersion.PRICE, "baostock"),)

    def fetch_deposit_rates(self) -> Sequence[DepositRate]:
        self.calls.append(("rates", None, None))
        if self.fail_rates:
            raise ValueError("fixture rate outage")
        return (DepositRate("1_year", date(2015, 10, 24), Decimal("0.015"), "baostock"),)

    def fetch_index_daily_bars(self, indexes: Sequence[IndexIdentity], start: date,
                               end: date) -> Sequence[IndexDailyBar]:
        self.calls.append(("index", start, end))
        return tuple(IndexDailyBar(item.index_id, day, Decimal(100 + i), item.return_version)
                     for item in indexes for i in range((end-start).days+1)
                     if (day := start + timedelta(days=i)).weekday() < 5 and day != self.missing_day)

    def fetch_daily_bars(self, codes: Sequence[str], start: date, end: date,
                         adjustment: AdjustmentMethod) -> Sequence[DailyBar]:
        return tuple(DailyBar(code, day, value, value, value, value, value,
                             Decimal(100), Decimal(1000), True)
                     for code in codes for i in range((end-start).days+1)
                     if (day := start + timedelta(days=i)).weekday() < 5
                     for value in (Decimal(100) + Decimal(i) / Decimal(10),))


def make_service(tmp_path: Path) -> tuple[DataSyncService, SQLiteRepository, ReferenceProvider, list[datetime]]:
    repo = SQLiteRepository(tmp_path / "db.sqlite3")
    provider = ReferenceProvider()
    clock = [NOW]
    service = DataSyncService(provider, repo, tmp_path / "locks", SyncConfig(
        time(17, 30), timedelta(seconds=300), 0, 45, 3,
    ), clock=lambda: clock[0])
    return service, repo, provider, clock


def test_reference_pipeline_publishes_independently_and_skips_without_network(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    run = service.sync_capm_reference_data(START, END)
    assert run.published
    assert repo.get_active_generation("capm", AdjustmentMethod.UNADJUSTED)
    assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None
    assert len(repo.get_index_daily_bars("hs300.price", START, END)) == 2
    tasks = repo.list_sync_tasks(run.plan_id)
    assert {task.data_type for task in tasks} == {"index_catalog", "index_daily_bars", "deposit_rates"}
    assert all(task.status.value == "SUCCESS" for task in tasks)
    assert repo.get_sync_plan(run.plan_id).task_count == len(tasks)
    before = list(provider.calls)
    repeat = service.sync_capm_reference_data(START, END)
    assert repeat.warning
    assert provider.calls == before


def test_failed_reference_plan_keeps_published_tables_empty_then_explicit_retry(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    provider.fail_rates = True
    with pytest.raises(PipelineError, match="fixture rate outage"):
        service.sync_capm_reference_data(START, END)
    assert repo.get_active_generation("capm", AdjustmentMethod.UNADJUSTED) is None
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT count(*) FROM index_catalog").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM index_catalog_staging").fetchone()[0] == 1
    with pytest.raises(RetryRequiredError):
        service.sync_capm_reference_data(START, END)
    with pytest.raises(RetryCooldownError):
        service.sync_capm_reference_data(START, END, retry_failed=True)
    clock[0] += timedelta(minutes=6)
    provider.fail_rates = False
    assert service.sync_capm_reference_data(START, END, retry_failed=True).published
    assert sum(call[0] == "catalog" for call in provider.calls) == 1


def test_reference_gap_is_published_with_honest_coverage_and_no_duplicate_fetch(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    provider.missing_day = END
    run = service.sync_capm_reference_data(START, END)
    assert run.published
    assert run.report_issues == 0
    record = next(r for r in repo.list_coverage_verifications(run.candidate.candidate_generation_id)
                  if r.data_type == "index_daily_bars")
    assert record.status.value == "ACCEPTED_WITH_GAPS"
    assert (record.expected_count, record.actual_count, record.coverage_ratio) == (2, 1, Decimal("0.5"))
    assert record.missing_items == (END.isoformat(),)
    details = json.loads(record.details_json)
    assert details["acceptance_policy"] == "source_available_v1"
    assert details["unavailable_ranges"] == [[END.isoformat(), END.isoformat()]]
    assert details["provider_code"] == "sh.000300"
    assert len(repo.get_index_daily_bars("hs300.price", START, END)) == 1
    assert repo.get_active_generation("market", AdjustmentMethod.QFQ) is None
    calls = list(provider.calls)
    assert service.sync_capm_reference_data(START, END).warning
    assert provider.calls == calls


def test_old_gap_failure_revalidates_staging_without_refetch_on_explicit_retry(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    provider.missing_day = END
    original = CapmVerifier.verify

    def old_policy(self: CapmVerifier, candidate: CandidateGeneration, *,
                   adjustment: AdjustmentMethod, target_start: date, target_end: date,
                   tasks: Sequence[SyncTask]) -> VerificationOutcome:
        outcome = original(self, candidate, adjustment=adjustment, target_start=target_start,
                           target_end=target_end, tasks=tasks)
        records = []
        issues = []
        for record in outcome.records:
            if record.data_type == "index_daily_bars" and record.missing_items:
                details = json.loads(record.details_json)
                details.pop("acceptance_policy", None)
                records.append(replace(record, status=VerificationStatus.INCOMPLETE,
                                       details_json=json.dumps(details)))
                issues.append(VerificationIssue("old-policy-gap", candidate.candidate_generation_id,
                    record.data_type, record.partition_key, target_start, ("hs300.price",),
                    IssueType.MISSING, record.expected_count, record.actual_count,
                    Repairability.REFETCH, "old strict coverage policy"))
            else:
                records.append(record)
        return VerificationOutcome(replace(outcome.report, issues=tuple(issues)), tuple(records))

    with patch.object(CapmVerifier, "verify", old_policy):
        first = service.sync_capm_reference_data(START, END)
    assert not first.published
    assert first.candidate.status.value == "NEEDS_REPAIR"
    calls = list(provider.calls)
    with pytest.raises(RetryRequiredError):
        service.sync_capm_reference_data(START, END)
    recovered = service.sync_capm_reference_data(START, END, retry_failed=True)
    assert recovered.published
    assert provider.calls == calls
    assert all(task.attempt_count == 1 for task in repo.list_sync_tasks(first.plan_id))


def test_default_benchmark_completely_empty_still_blocks_publication(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)

    def no_prices(indexes: Sequence[IndexIdentity], start: date, end: date) -> Sequence[IndexDailyBar]:
        return ()

    provider.fetch_index_daily_bars = no_prices
    run = service.sync_capm_reference_data(START, END)
    assert not run.published
    assert run.report_issues == 1
    assert repo.get_active_generation("capm", AdjustmentMethod.UNADJUSTED) is None


def test_default_empty_increment_is_a_gap_when_parent_has_prices(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    clock[0] += timedelta(days=1)
    next_day = END + timedelta(days=1)
    provider.missing_day = next_day
    assert service.sync_capm_reference_data(START, next_day).published
    assert repo.capm_unavailable_ranges("hs300.price") == ((str(next_day), str(next_day)),)
    calls = list(provider.calls)
    assert service.sync_capm_reference_data(START, next_day).warning
    assert calls == provider.calls


def test_gap_does_not_block_another_benchmark_but_missing_analysis_is_rejected(tmp_path: Path) -> None:
    from stock_manager.capm.service import CapmAnalysisService

    service, repo, provider, _ = make_service(tmp_path)
    first = date(2026, 1, 1)
    original_catalog = provider.fetch_indexes
    original_prices = provider.fetch_index_daily_bars

    def catalog(as_of: date) -> Sequence[IndexIdentity]:
        return (*original_catalog(as_of), IndexIdentity("industry.price", "sh.000841", "行业测试指数",
                                                        "industry", IndexReturnVersion.PRICE, "baostock"))

    def prices(indexes: Sequence[IndexIdentity], start: date, end: date) -> Sequence[IndexDailyBar]:
        return tuple(row for row in original_prices(indexes, start, end)
                     if row.index_id != "industry.price" or row.trading_day != END)

    provider.fetch_indexes = catalog
    provider.fetch_index_daily_bars = prices
    assert service.sync_capm_reference_data(first, END).published
    meta = DatasetMetadata("market", END, "fixture", NOW, AdjustmentMethod.QFQ)
    repo.save_stocks((StockIdentity("sh.600000", "fixture", "sh", False, date(2000, 1, 1), None),), meta)
    pipeline = service.build_pipeline()
    output = pipeline.plan(mode=SyncPlanMode.BOOTSTRAP, dataset_id="market",
        adjustment=AdjustmentMethod.QFQ, target_start=first, target_end=END,
        required_data_types=("daily_bars",))
    assert pipeline.execute(output.plan.plan_id).published
    calls = list(provider.calls)
    analysis = CapmAnalysisService(repo)
    assert all(r.status == "READY" for r in analysis.analyse("sh.600000", END, windows=(30, 120)))
    missing = analysis.analyse("sh.600000", END, windows=(30,), benchmark_id="industry.price")
    assert missing[0].status == "DATA_INCOMPLETE"
    assert END.isoformat() in missing[0].reason
    assert provider.calls == calls


@pytest.mark.parametrize("bad_kind", ("identity", "return_version", "non_trading_day"))
def test_invalid_index_rows_still_block_publication(tmp_path: Path, bad_kind: str) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    original = provider.fetch_index_daily_bars

    def invalid_prices(indexes: Sequence[IndexIdentity], start: date, end: date) -> Sequence[IndexDailyBar]:
        rows = original(indexes, start, end)
        row = rows[0]
        if bad_kind == "identity":
            row = replace(row, index_id="wrong.price")
        elif bad_kind == "return_version":
            row = replace(row, return_version=IndexReturnVersion.GROSS_TOTAL_RETURN)
        else:
            row = replace(row, trading_day=date(2026, 8, 29))
        return (row, *rows[1:])

    provider.fetch_index_daily_bars = invalid_prices
    run = service.sync_capm_reference_data(START, END)
    assert not run.published
    assert repo.get_active_generation("capm", AdjustmentMethod.UNADJUSTED) is None


def test_reference_increment_only_fetches_new_day_and_preserves_market(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    clock[0] += timedelta(days=1)
    new_end = END + timedelta(days=1)
    assert service.sync_capm_reference_data(START, new_end).published
    assert [call for call in provider.calls if call[0] == "index"] == [
        ("index", START, END), ("index", new_end, new_end),
    ]
    assert len(repo.get_index_daily_bars("hs300.price", START, new_end)) == 3


def test_published_index_identity_reaches_capm_via_read_only_generation_gate(tmp_path: Path) -> None:
    from stock_manager.capm.service import CapmAnalysisService
    from stock_manager.read.capm import SQLiteCapmReader

    service, repo, provider, _ = make_service(tmp_path)
    first = date(2026, 1, 1)
    assert service.sync_capm_reference_data(first, END).published
    meta = DatasetMetadata("market", END, "fixture", NOW, AdjustmentMethod.QFQ)
    repo.save_stocks((StockIdentity("sh.600000", "fixture", "sh", False, date(2000, 1, 1), None),), meta)
    pipeline = service.build_pipeline()
    output = pipeline.plan(mode=SyncPlanMode.BOOTSTRAP, dataset_id="market",
        adjustment=AdjustmentMethod.QFQ, target_start=first, target_end=END,
        required_data_types=("daily_bars",))
    assert pipeline.execute(output.plan.plan_id).published
    calls = list(provider.calls)
    inputs = SQLiteCapmReader(repo.database_path).read("sh.600000", "hs300.price", "1_year", first, END)
    assert inputs.market_levels and inputs.rates and inputs.stock_levels
    analysis_id, results = CapmAnalysisService(repo).analyse_and_save("sh.600000", END, windows=(30, 120))
    assert all(r.status == "READY" for r in results)
    assert provider.calls == calls
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT DISTINCT benchmark_id FROM capm_results WHERE analysis_id=?",
                                  (analysis_id,)).fetchall() == [("hs300.price",)]
        connection.execute("DELETE FROM index_daily_bars WHERE trading_day=?", (END.isoformat(),))
    broken = CapmAnalysisService(repo).analyse("sh.600000", END, windows=(30,))
    assert broken[0].status == "DATA_INCOMPLETE"
    assert END.isoformat() in broken[0].reason


def test_missing_local_published_day_is_repaired_without_repulling_successful_days(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("DELETE FROM index_daily_bars WHERE trading_day=?", (START.isoformat(),))
    assert service.sync_capm_reference_data(START, END).published
    assert [call for call in provider.calls if call[0] == "index"][-1] == ("index", START, START)
    assert sum(call[0] == "catalog" for call in provider.calls) == 1
    assert sum(call[0] == "rates" for call in provider.calls) == 1


def test_price_detached_from_active_manifest_is_refetched_before_repair_publish(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("UPDATE index_daily_bars SET batch_id=? WHERE trading_day=?",
                           ("unpublished-fixture", START.isoformat()))
    assert not repo.capm_coverage_intact(START, END)
    assert service.sync_capm_reference_data(START, END).published
    assert [call for call in provider.calls if call[0] == "index"][-1] == ("index", START, START)
    assert repo.capm_coverage_intact(START, END)


def test_missing_one_year_rate_is_refetched_even_when_other_terms_exist(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    original = provider.fetch_deposit_rates

    def rates() -> Sequence[DepositRate]:
        return (*original(), DepositRate("3_month", date(2015, 10, 24), Decimal("0.011"), "baostock"))

    provider.fetch_deposit_rates = rates
    assert service.sync_capm_reference_data(START, END).published
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("DELETE FROM deposit_rates WHERE term=?", ("1_year",))
    assert not repo.capm_coverage_intact(START, END)
    assert service.sync_capm_reference_data(START, END).published
    assert repo.capm_coverage_intact(START, END)
    assert repo.get_deposit_rates("1_year", START, END)
    assert sum(call[0] == "rates" for call in provider.calls) == 2
    assert sum(call[0] == "index" for call in provider.calls) == 1


def test_accepted_source_gap_is_preserved_without_requery_on_new_target(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    provider.missing_day = END
    assert service.sync_capm_reference_data(START, END).published
    clock[0] += timedelta(days=1)
    new_end = END + timedelta(days=1)
    assert service.sync_capm_reference_data(START, new_end).published
    assert [call for call in provider.calls if call[0] == "index"] == [
        ("index", START, END), ("index", new_end, new_end),
    ]
    assert repo.capm_unavailable_ranges("hs300.price") == ((END.isoformat(), END.isoformat()),)
    assert repo.capm_coverage_intact(START, new_end)
    status = repo.capm_data_status()
    assert status["indexes"][0]["expected_count"] == 3
    assert status["indexes"][0]["missing_count"] == 1


def test_source_unavailable_index_is_disclosed_without_fabrication_or_repeated_history(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    original = provider.fetch_indexes

    def catalog(as_of: date) -> Sequence[IndexIdentity]:
        return (*original(as_of), IndexIdentity("unavailable.price", "sh.000999", "fixture",
                                                "industry", IndexReturnVersion.PRICE, "baostock"))

    fetch = provider.fetch_index_daily_bars

    def prices(indexes: Sequence[IndexIdentity], start: date, end: date) -> Sequence[IndexDailyBar]:
        return fetch(indexes, start, end) if indexes[0].index_id == "hs300.price" else ()

    provider.fetch_indexes = catalog
    provider.fetch_index_daily_bars = prices
    assert service.sync_capm_reference_data(START, END).published
    assert repo.get_index_daily_bars("unavailable.price", START, END) == ()
    assert repo.capm_unavailable_ranges("unavailable.price") == ((str(START), str(END)),)
    calls = list(provider.calls)
    assert service.sync_capm_reference_data(START, END).warning
    assert provider.calls == calls


def test_both_sync_services_share_provider_process_and_file_lock(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    other = DataSyncService(ReferenceProvider(), repo, tmp_path / "locks", service._config)
    assert other._provider_process_lock is service._provider_process_lock
    assert other._provider_file_lock == service._provider_file_lock


def test_empty_catalog_is_a_retryable_failed_task_not_unrecoverable_success(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    original = provider.fetch_indexes
    provider.fetch_indexes = lambda as_of: ()
    with pytest.raises(PipelineError, match="empty index catalogue"):
        service.sync_capm_reference_data(START, END)
    plan = repo.list_sync_plans("capm", AdjustmentMethod.UNADJUSTED)[0]
    assert repo.list_sync_tasks(plan.plan_id)[0].status.value == "FAILED"
    provider.fetch_indexes = original
    clock[0] += timedelta(minutes=6)
    assert service.sync_capm_reference_data(START, END, retry_failed=True).published


def test_resuming_yesterday_also_catches_up_to_requested_new_target(tmp_path: Path) -> None:
    service, repo, provider, clock = make_service(tmp_path)
    provider.fail_rates = True
    with pytest.raises(PipelineError):
        service.sync_capm_reference_data(START, END)
    clock[0] += timedelta(days=1)
    provider.fail_rates = False
    new_end = END + timedelta(days=1)
    run = service.sync_capm_reference_data(START, new_end, retry_failed=True)
    assert run.published
    assert repo.get_sync_plan(run.plan_id).target_end == new_end
    assert len(repo.get_index_daily_bars("hs300.price", START, new_end)) == 3
