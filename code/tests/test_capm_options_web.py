"""Local, published CAPM selectors and analysis-parameter HTTP contracts."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from stock_manager.capm import CapmAnalysisService
from stock_manager.domain import (
    AdjustmentMethod, DatasetMetadata, DepositRate, IndexDailyBar, IndexIdentity,
    IndexReturnVersion, StockIdentity, SyncPlanMode,
)
from stock_manager.storage.sqlite_repo import SQLiteRepository
from test_capm_sync_pipeline import END, NOW, START, make_service
from test_capm_sync_web import app_for
from test_web_api import _get_query, _post


def test_options_without_publication_are_empty_even_with_legacy_rows(tmp_path: Path) -> None:
    app = app_for(tmp_path)
    repo = SQLiteRepository(tmp_path / "db.sqlite3")
    repo.save_indexes((IndexIdentity("orphan.price", "sh.000999", "未发布指数", "broad",
        IndexReturnVersion.PRICE, "fixture"),))
    repo.save_deposit_rates((DepositRate("1_year", START, Decimal("0.01"), "fixture"),))
    status, payload = _get_query(app, "/api/capm/options", {"as_of": str(END)})
    assert status == 200
    assert payload == {"as_of": str(END), "generation_id": None, "benchmarks": [],
        "rate_terms": [], "defaults": {"benchmark_id": "hs300.price", "rate_term": "1_year",
                                      "periods_per_year": 252}}


def test_options_resolve_actual_effective_rates_and_ignore_unpublished_rows(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)

    def rates() -> Sequence[DepositRate]:
        return (DepositRate("1_year", date(2015, 10, 24), Decimal("0.015"), "baostock"),
                DepositRate("1_year", END, Decimal("0.012"), "baostock"),
                DepositRate("3_year", date(2027, 1, 1), Decimal("0.018"), "baostock"))

    provider.fetch_deposit_rates = rates
    assert service.sync_capm_reference_data(START, END).published
    generation = repo.capm_data_status()["generation"]
    repo.save_indexes((IndexIdentity("orphan.price", "sh.000999", "未发布指数", "industry",
        IndexReturnVersion.PRICE, "fixture"),))
    repo.save_deposit_rates((DepositRate("5_year", START, Decimal("0.99"), "fixture"),))
    app = app_for(tmp_path)
    before = list(provider.calls)
    with patch.object(SQLiteRepository, "_connect", side_effect=AssertionError("options must be read-only")):
        status, first = _get_query(app, "/api/capm/options", {"as_of": str(START)})
        assert status == 200, first
        status, second = _get_query(app, "/api/capm/options", {"as_of": str(END)})
        assert status == 200, second
    assert first["generation_id"] == generation
    assert [item["index_id"] for item in first["benchmarks"]] == ["hs300.price"]
    assert first["benchmarks"][0]["bar_count"] == 1
    assert first["benchmarks"][0]["return_version"] == "price"
    assert first["benchmarks"][0]["provider_code"] == "sh.000300"
    assert first["benchmarks"][0]["coverage_status"] == "COMPLETE"
    assert second["benchmarks"][0]["bar_count"] == 2
    assert first["rate_terms"][0] == {"term": "1_year", "label": "一年期", "annual_rate": "0.015",
                                    "effective_on": "2015-10-24", "source": "baostock"}
    assert second["rate_terms"][0]["annual_rate"] == "0.012"
    assert second["rate_terms"][0]["effective_on"] == str(END)
    assert first["rate_terms"][1] == {"term": "3_year", "label": "三年期", "annual_rate": None,
                                    "effective_on": None, "source": None}
    assert provider.calls == before


@pytest.mark.parametrize("query", [
    {}, {"as_of": [""]}, {"as_of": ["2026-09-31"]}, {"as_of": ["20260902"]},
    {"as_of": [str(END), str(START)]}, {"as_of": [str(END)], "unexpected": ["1"]},
])
def test_options_reject_malformed_unknown_or_repeated_parameters(tmp_path: Path, query: dict[str, list[str]]) -> None:
    response = app_for(tmp_path).route("GET", "/api/capm/options", query, None)
    assert response[0] == 400, response[2]


def test_analysis_forwards_local_choices_and_reports_exact_parameters(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    app = app_for(tmp_path)
    before = list(provider.calls)
    with patch.object(CapmAnalysisService, "analyse_and_save", return_value=("fixture-analysis", ())) as analyse:
        status, payload = _post(app, "/api/capm/analyses", {"stock_code": "sh.600000", "as_of": str(END),
            "benchmark_id": "hs300.price", "rate_term": "1_year", "periods_per_year": 240, "windows": [30]})
    assert status == 201, payload
    analyse.assert_called_once_with("sh.600000", END, benchmark_id="hs300.price", rate_term="1_year",
                                   windows=(30,), periods_per_year=240)
    assert payload == {"analysis_id": "fixture-analysis", "results": [], "stock_code": "sh.600000",
        "as_of": str(END), "benchmark_id": "hs300.price", "benchmark_return_version": "price",
        "rate_term": "1_year", "periods_per_year": 240}
    assert provider.calls == before


def test_analysis_computes_and_saves_selected_total_return_benchmark_and_rate(tmp_path: Path) -> None:
    service, repo, provider, _ = make_service(tmp_path)
    start = date(2026, 1, 1)
    original_catalog = provider.fetch_indexes
    original_prices = provider.fetch_index_daily_bars
    original_rates = provider.fetch_deposit_rates

    def catalog(as_of: date) -> Sequence[IndexIdentity]:
        return (*original_catalog(as_of), IndexIdentity("industry.total", "sz.399999", "行业收益测试指数",
                "industry", IndexReturnVersion.GROSS_TOTAL_RETURN, "baostock"))

    def prices(indexes: Sequence[IndexIdentity], first: date, last: date) -> Sequence[IndexDailyBar]:
        return tuple(IndexDailyBar(row.index_id, row.trading_day,
                     row.close + (Decimal(100) if row.index_id == "industry.total" else Decimal(0)), row.return_version)
                     for row in original_prices(indexes, first, last))

    def rates() -> Sequence[DepositRate]:
        return (*original_rates(), DepositRate("3_year", date(2015, 10, 24), Decimal("0.03"), "baostock"))

    provider.fetch_indexes = catalog
    provider.fetch_index_daily_bars = prices
    provider.fetch_deposit_rates = rates
    assert service.sync_capm_reference_data(start, END).published
    repo.save_stocks((StockIdentity("sh.600000", "fixture", "sh", False, date(2000, 1, 1), None),),
        DatasetMetadata("market", END, "fixture", NOW, AdjustmentMethod.QFQ))
    pipeline = service.build_pipeline()
    plan = pipeline.plan(mode=SyncPlanMode.BOOTSTRAP, dataset_id="market", adjustment=AdjustmentMethod.QFQ,
                         target_start=start, target_end=END, required_data_types=("daily_bars",))
    assert pipeline.execute(plan.plan.plan_id).published
    before = list(provider.calls)
    status, payload = _post(app_for(tmp_path), "/api/capm/analyses", {
        "stock_code": "sh.600000", "as_of": str(END), "benchmark_id": "industry.total",
        "rate_term": "3_year", "periods_per_year": 240, "windows": [30, 120],
    })
    assert status == 201, payload
    assert payload["benchmark_return_version"] == "gross_total_return"
    assert all(item["status"] == "READY" for item in payload["results"])
    for item in payload["results"]:
        estimate = item["estimate"]
        assert Decimal(estimate["alpha_annualized"]) == pytest.approx(Decimal(estimate["alpha_daily"]) * 240)
        assert estimate["periods_per_year"] == 240
    with sqlite3.connect(repo.database_path) as connection:
        rows = connection.execute("SELECT benchmark_id, benchmark_return_version, rate_term, periods_per_year"
                                  " FROM capm_results WHERE analysis_id=?", (payload["analysis_id"],)).fetchall()
    assert rows == [("industry.total", "gross_total_return", "3_year", 240)] * 2
    assert provider.calls == before


@pytest.mark.parametrize("change", [
    {"benchmark_id": "orphan.price"}, {"benchmark_id": "unknown.price"}, {"rate_term": "orphan_term"},
    {"windows": []}, {"windows": [True]}, {"windows": [0]}, {"periods_per_year": True},
    {"periods_per_year": 0}, {"periods_per_year": -1},
])
def test_analysis_rejects_unpublished_selections_and_invalid_numeric_choices(tmp_path: Path, change: dict[str, object]) -> None:
    service, repo, _, _ = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    repo.save_indexes((IndexIdentity("orphan.price", "sh.000999", "未发布指数", "broad",
        IndexReturnVersion.PRICE, "fixture"),))
    repo.save_deposit_rates((DepositRate("orphan_term", START, Decimal("0.99"), "fixture"),))
    with patch.object(CapmAnalysisService, "analyse_and_save") as analyse:
        status, payload = _post(app_for(tmp_path), "/api/capm/analyses",
            {"stock_code": "sh.600000", "as_of": str(END), **change})
    assert status == 400, payload
    assert payload["error"]["code"] == "BAD_REQUEST"
    analyse.assert_not_called()
    with sqlite3.connect(repo.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM capm_results").fetchone()[0] == 0


def test_readonly_options_use_one_snapshot_during_active_pointer_change(tmp_path: Path) -> None:
    service, repo, _, _ = make_service(tmp_path)
    assert service.sync_capm_reference_data(START, END).published
    expected = repo.capm_data_status()["generation"]
    app = app_for(tmp_path)
    with sqlite3.connect(repo.database_path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    original = SQLiteRepository._capm_coverage_snapshot.__func__

    def advance_pointer(cls: type[SQLiteRepository], connection: sqlite3.Connection,
                        start: date | None = None, end: date | None = None) -> dict[str, object]:
        result = original(cls, connection, start, end)
        # WAL permits publication while the reader keeps its established snapshot.
        with sqlite3.connect(repo.database_path) as writer:
            writer.execute("DELETE FROM active_generations WHERE dataset_id='capm'")
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        return result

    with patch.object(SQLiteRepository, "_capm_coverage_snapshot", classmethod(advance_pointer)):
        status, payload = _get_query(app, "/api/capm/options", {"as_of": str(END)})
    assert status == 200, payload
    assert payload["generation_id"] == expected
    assert payload["rate_terms"][0]["annual_rate"] == "0.015"
    assert repo.capm_data_status()["generation"] is None
