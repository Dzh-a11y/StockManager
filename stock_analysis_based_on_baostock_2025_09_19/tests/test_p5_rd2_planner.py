"""Offline tests for P5-RD-2: deterministic SyncPlanner."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from stock_manager.domain import (
    AdjustmentMethod,
    CandidateGenerationStatus,
    IssueType,
    Repairability,
    SyncPlanMode,
    SyncPlanStatus,
    SyncSource,
    SyncTaskStatus,
    VerificationIssue,
    VerificationReport,
)
from stock_manager.sync.planner import (
    DATA_TYPE_ORDER,
    PlanInput,
    PlanRejectedError,
    SyncPlanner,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAY = date(2026, 9, 1)

CALENDAR = (
    date(2026, 8, 31),
    DAY,
    date(2026, 9, 2),
    date(2026, 9, 3),
)
CODES = ("sh.600000", "sz.000001", "sh.600519", "sz.300750")


def _coverage(_adjustment: AdjustmentMethod, _data_type: str) -> tuple[date | None, date | None]:
    return None, None


def _universe(as_of: date) -> tuple[str, ...]:
    return tuple(CODES)


def _planner() -> SyncPlanner:
    return SyncPlanner(
        calendar=lambda start, end: tuple(
            d for d in CALENDAR if start <= d <= end
        ),
        coverage=_coverage,
        universe_codes=_universe,
        now=lambda: NOW,
    )


def _issue(
    *,
    data_type: str = "daily_bars",
    partition_key: str = "2026-09-01",
    codes: tuple[str, ...] = ("sh.600000",),
    issue_type: IssueType = IssueType.MISSING,
    repairability: Repairability = Repairability.REFETCH,
    expected_count: int = 1,
    actual_count: int = 0,
) -> VerificationIssue:
    return VerificationIssue(
        issue_id="issue",
        candidate_generation_id="cand-1",
        data_type=data_type,
        partition_key=partition_key,
        trading_day=date.fromisoformat(partition_key),
        codes=codes,
        issue_type=issue_type,
        expected_count=expected_count,
        actual_count=actual_count,
        repairability=repairability,
        details="missing data",
    )


class TestPlanInputFingerprint:
    def test_identical_inputs_same_identity(self) -> None:
        a = PlanInput(
            SyncPlanMode.BOOTSTRAP,
            SyncSource.BAOSTOCK,
            "market",
            AdjustmentMethod.QFQ,
            "a-share",
            date(2018, 9, 1),
            DAY,
            ("daily_bars", "fundamentals"),
        )
        b = PlanInput(
            SyncPlanMode.BOOTSTRAP,
            SyncSource.BAOSTOCK,
            "market",
            AdjustmentMethod.QFQ,
            "a-share",
            date(2018, 9, 1),
            DAY,
            ("fundamentals", "daily_bars"),
        )
        assert a.fingerprint == b.fingerprint
        assert a.plan_id == b.plan_id

    def test_range_change_changes_identity(self) -> None:
        a = PlanInput(
            SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
            AdjustmentMethod.QFQ, "a-share", date(2018, 9, 1), DAY,
            ("daily_bars",),
        )
        b = PlanInput(
            SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
            AdjustmentMethod.QFQ, "a-share", date(2018, 9, 2), DAY,
            ("daily_bars",),
        )
        assert a.fingerprint != b.fingerprint

    def test_adjustment_change_changes_identity(self) -> None:
        a = PlanInput(
            SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
            AdjustmentMethod.QFQ, "a-share", date(2018, 9, 1), DAY,
            ("daily_bars",),
        )
        b = PlanInput(
            SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
            AdjustmentMethod.UNADJUSTED, "a-share", date(2018, 9, 1), DAY,
            ("daily_bars",),
        )
        assert a.fingerprint != b.fingerprint

    def test_data_type_order_normalized(self) -> None:
        raw = ("dividends", "daily_bars", "fundamentals", "stocks")
        plan = PlanInput(
            SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
            AdjustmentMethod.QFQ, "a-share", date(2018, 9, 1), DAY, raw,
        )
        assert plan.required_data_types == DATA_TYPE_ORDER

    def test_unknown_data_type_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlanInput(
                SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
                AdjustmentMethod.QFQ, "a-share", date(2018, 9, 1), DAY,
                ("not_a_type",),
            )

    def test_reversed_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            PlanInput(
                SyncPlanMode.BOOTSTRAP, SyncSource.BAOSTOCK, "market",
                AdjustmentMethod.QFQ, "a-share", DAY, date(2018, 9, 1),
                ("daily_bars",),
            )


class TestBootstrap:
    def test_bootstrap_plan_identity_deterministic(self) -> None:
        p1 = _planner().plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
        )
        p2 = _planner().plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
        )
        assert p1.plan.plan_id == p2.plan.plan_id
        assert [t.task_id for t in p1.tasks] == [t.task_id for t in p2.tasks]
        assert [t.codes for t in p1.tasks] == [t.codes for t in p2.tasks]

    def test_bootstrap_tasks_per_type_per_day(self) -> None:
        out = _planner().plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
            required_data_types=("daily_bars", "stocks"),
        )
        # 4 trading days x 2 data types
        assert len(out.tasks) == 8
        types = [t.data_type for t in out.tasks]
        assert types == ["stocks"] * 4 + ["daily_bars"] * 4
        assert all(t.status is SyncTaskStatus.PENDING for t in out.tasks)
        assert all(t.codes == tuple(sorted(CODES)) for t in out.tasks)
        assert out.plan.mode is SyncPlanMode.BOOTSTRAP
        assert out.plan.source is SyncSource.BAOSTOCK
        assert out.plan.parent_generation is None
        assert out.plan.task_count == 8
        assert out.candidate.status is CandidateGenerationStatus.PLANNED

    def test_bootstrap_ordered_by_day(self) -> None:
        out = _planner().plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
            required_data_types=("daily_bars",),
        )
        keys = [t.partition_key for t in out.tasks]
        assert keys == sorted(keys)

    def test_empty_calendar_rejected(self) -> None:
        planner = SyncPlanner(
            calendar=lambda _s, _e: (),
            coverage=_coverage,
            universe_codes=_universe,
            now=lambda: NOW,
        )
        with pytest.raises(PlanRejectedError):
            planner.plan_bootstrap(
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                target_start=date(2026, 8, 31),
                target_end=date(2026, 9, 3),
            )


class TestIncremental:
    def test_incremental_requires_tail_gap(self) -> None:
        planner = _planner()
        with pytest.raises(PlanRejectedError):
            planner.plan_incremental(
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                active_generation="g1",
                coverage_end=DAY,
                target_end=DAY,
            )

    def test_incremental_parent_generation(self) -> None:
        out = _planner().plan_incremental(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            active_generation="g1",
            coverage_end=date(2026, 9, 1),
            target_end=date(2026, 9, 3),
            required_data_types=("daily_bars",),
        )
        assert out.plan.mode is SyncPlanMode.INCREMENTAL
        assert out.plan.parent_generation == "g1"
        # 增量窗口从 coverage_end 之后第一个交易日开始(09-02),到 target_end。
        assert out.plan.target_start == date(2026, 9, 2)
        assert out.plan.target_end == date(2026, 9, 3)
        # 09-02, 09-03 两个交易日
        assert len(out.tasks) == 2

    def test_incremental_fingerprint_differs_from_bootstrap(self) -> None:
        inc = _planner().plan_incremental(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            active_generation="g1",
            coverage_end=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
            required_data_types=("daily_bars",),
        )
        boot = _planner().plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
            required_data_types=("daily_bars",),
        )
        assert inc.plan.plan_fingerprint != boot.plan.plan_fingerprint


class TestRepair:
    def _report(self, *issues: VerificationIssue) -> VerificationReport:
        return VerificationReport("cand-1", issues, NOW)

    def test_refetch_issues_become_tasks(self) -> None:
        report = self._report(
            _issue(data_type="daily_bars", partition_key="2026-09-01"),
            _issue(data_type="fundamentals", partition_key="2026-09-01"),
        )
        out = _planner().plan_repair(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            report=report,
            active_generation="g1",
        )
        assert out.plan.mode is SyncPlanMode.REPAIR
        assert out.plan.parent_generation == "g1"
        assert len(out.tasks) == 2
        assert [t.data_type for t in out.tasks] == ["daily_bars", "fundamentals"]

    def test_duplicate_issues_deduplicated(self) -> None:
        report = self._report(
            _issue(partition_key="2026-09-01", codes=("sh.600000", "sz.000001")),
            _issue(partition_key="2026-09-01", codes=("sz.000001", "sh.600000")),
        )
        out = _planner().plan_repair(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            report=report,
            active_generation="g1",
        )
        assert len(out.tasks) == 1

    def test_manual_issues_rejected(self) -> None:
        report = self._report(
            _issue(repairability=Repairability.MANUAL),
        )
        with pytest.raises(PlanRejectedError):
            _planner().plan_repair(
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                report=report,
                active_generation="g1",
            )

    def test_rebuild_issues_rejected(self) -> None:
        report = self._report(
            _issue(issue_type=IssueType.ADJUSTMENT_MISMATCH, repairability=Repairability.REBUILD),
        )
        with pytest.raises(PlanRejectedError):
            _planner().plan_repair(
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                report=report,
                active_generation="g1",
            )

    def test_no_refetchable_issues_rejected(self) -> None:
        report = self._report(
            _issue(repairability=Repairability.MANUAL),
        )
        with pytest.raises(PlanRejectedError):
            _planner().plan_repair(
                dataset_id="market",
                adjustment=AdjustmentMethod.QFQ,
                report=report,
                active_generation="g1",
            )

    def test_same_report_same_plan(self) -> None:
        report = self._report(
            _issue(data_type="daily_bars", partition_key="2026-09-01"),
            _issue(data_type="daily_bars", partition_key="2026-09-02"),
        )
        a = _planner().plan_repair(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            report=report, active_generation="g1",
        )
        b = _planner().plan_repair(
            dataset_id="market", adjustment=AdjustmentMethod.QFQ,
            report=report, active_generation="g1",
        )
        assert a.plan.plan_id == b.plan.plan_id
        assert [t.task_id for t in a.tasks] == [t.task_id for t in b.tasks]


class TestLegacyImport:
    def test_legacy_import_source(self) -> None:
        out = _planner().plan_legacy_import(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
            required_data_types=("daily_bars",),
        )
        assert out.plan.mode is SyncPlanMode.LEGACY_IMPORT
        assert out.plan.source is SyncSource.LEGACY_DATABASE
        assert out.plan.parent_generation is None


class TestPlannerNeverTouchesProvider:
    def test_planning_never_calls_coverage(self) -> None:
        calls: list[str] = []

        def coverage(_a: AdjustmentMethod, _t: str) -> tuple[date | None, date | None]:
            calls.append("coverage")
            return None, None

        planner = SyncPlanner(
            calendar=lambda start, end: tuple(
                d for d in CALENDAR if start <= d <= end
            ),
            coverage=coverage,
            universe_codes=_universe,
            now=lambda: NOW,
        )
        planner.plan_bootstrap(
            dataset_id="market",
            adjustment=AdjustmentMethod.QFQ,
            target_start=date(2026, 8, 31),
            target_end=date(2026, 9, 3),
        )
        assert calls == []
