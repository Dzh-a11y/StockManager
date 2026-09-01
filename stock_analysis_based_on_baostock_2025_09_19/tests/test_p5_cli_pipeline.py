"""Offline tests for P5 CLI pipeline commands (sync-start/retry/status)."""

from __future__ import annotations

import io
import json
from datetime import date, datetime, time as wall_time, timedelta
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
)
from stock_manager.storage import SQLiteRepository

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
DAYS = (date(2026, 9, 1), date(2026, 9, 2))
CODES = ("sh.600000", "sz.000001")


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "sync_config.json"
    path.write_text(
        json.dumps(
            {
                "metadata": {"version": 2, "timezone": "Asia/Shanghai"},
                "policy": {
                    "cutoff_time": "15:00:00",
                    "retry_cooldown_seconds": 5,
                    "minimum_request_interval_seconds": 0.0,
                    "calendar_horizon_days": 5,
                    "dividend_lookback_years": 1,
                    "retention_days": 360,
                },
                "history": {
                    "target_years": 8,
                    "coverage_policy": "latest_completed_trading_day",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


class FixtureProvider:
    source_name = "fixture"

    def fetch_stocks(self, as_of: date) -> list[StockIdentity]:
        return [StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES]

    def fetch_daily_bars(self, codes, start, end, adjustment) -> list[DailyBar]:
        bars: list[DailyBar] = []
        day = start
        while day <= end:
            for code in codes:
                bars.append(
                    DailyBar(
                        code, day, Decimal("10"), Decimal("11"), Decimal("9"),
                        Decimal("10.5"), Decimal("10"), Decimal("1000"),
                        Decimal("10500"), True,
                    )
                )
            day = day + timedelta(days=1)
        return bars

    def fetch_fundamentals(self, codes, as_of) -> list[FundamentalSnapshot]:
        return [
            FundamentalSnapshot(c, as_of, as_of, Decimal("8"), Decimal("1"), "fixture")
            for c in codes
        ]


@pytest.fixture()
def repo(tmp_path: Path) -> SQLiteRepository:
    r = SQLiteRepository(tmp_path / "market.sqlite3")
    r.save_trading_days(
        DAYS,
        DatasetMetadata("trading_calendar", DAYS[0], "fixture", NOW, AdjustmentMethod.UNADJUSTED),
    )
    r.save_stocks(
        [StockIdentity(c, "股票", "SSE", False, None, None) for c in CODES],
        DatasetMetadata("market", DAYS[0], "fixture", NOW, AdjustmentMethod.QFQ),
    )
    return r


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(argv, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class TestSyncStartCommand:
    def test_start_bootstrap_offline(
        self, repo: SQLiteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "stock_manager.cli.main.BaostockProvider", lambda **kwargs: FixtureProvider()
        )
        config = _config(tmp_path)
        code, out, err = _run(
            [
                "sync-start",
                "--db", str(repo.database_path),
                "--config", str(config),
                "--lock-dir", str(tmp_path / "locks"),
                "--mode", "BOOTSTRAP",
                "--start", DAYS[0].isoformat(),
                "--end", DAYS[-1].isoformat(),
                "--adjustment", "qfq",
                "--data-types", "daily_bars",
            ]
        )
        assert code == 0, err
        payload = json.loads(out)
        assert payload["published"] is True
        assert payload["plan_status"] == "SUCCEEDED"
        # 正式表已发布
        bars = repo.get_daily_bars(CODES, DAYS[0], DAYS[-1], AdjustmentMethod.QFQ)
        assert len(bars) == 4


class TestSyncStatusCommand:
    def test_status_after_start(
        self, repo: SQLiteRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "stock_manager.cli.main.BaostockProvider", lambda **kwargs: FixtureProvider()
        )
        config = _config(tmp_path)
        code, out, err = _run(
            [
                "sync-start",
                "--db", str(repo.database_path),
                "--config", str(config),
                "--lock-dir", str(tmp_path / "locks"),
                "--mode", "BOOTSTRAP",
                "--start", DAYS[0].isoformat(),
                "--end", DAYS[-1].isoformat(),
                "--adjustment", "qfq",
                "--data-types", "daily_bars",
            ]
        )
        assert code == 0, err
        code, out, err = _run(
            [
                "sync-status",
                "--db", str(repo.database_path),
            ]
        )
        assert code == 0, err
        payload = json.loads(out)
        assert payload
        assert payload[0]["status"] == "SUCCEEDED"
        assert payload[0]["task_counts"]["SUCCESS"] == 2
