#!/usr/bin/env python3
"""P5A-0 offline PoC: validate Backtrader on the current Python runtime.

Checks, fully offline with deterministic fixtures:

1. ``import backtrader`` succeeds on the current interpreter.
2. A minimal Cerebro with multiple data feeds runs a strategy and analyzers.
3. Running the exact same input twice produces identical results (hash-equal).
4. Environment facts (machine / Python / SQLite / backtrader / pandas / numpy /
   git commit) are captured into a stable JSON report schema (v1) so later
   P5A benchmark gates can compare runs across machines.

This script never touches a Provider, never opens a network socket, and never
modifies product code. It is a benchmark/PoC script, not part of the shipped
package (same status as ``scripts/bench_p4_*``).

Usage:
    python3 scripts/p5a_poc_backtrader.py            # run and print JSON
    python3 scripts/p5a_poc_backtrader.py --no-save  # print JSON only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import resource
import subprocess
import sys
import time
import warnings
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

import backtrader as bt

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "scripts" / "bench_results"
REPORT_SCHEMA_VERSION = 1

SYMBOLS: tuple[str, ...] = ("000001.SZ", "600000.SH", "300001.SZ")
FIXTURE_DAYS = 520  # roughly two trading years per symbol
FIXTURE_START = date(2018, 9, 3)
FIXTURE_SEED = 20260901


def peak_rss_mb() -> float:
    """Peak resident set size in MiB (macOS reports bytes, Linux reports KiB)."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round((value / 1024 / 1024) if sys.platform.startswith("linux") else (value / 1024 / 1024), 1)


def git_facts() -> dict[str, str]:
    """Capture git commit/branch/dirty state; failures degrade to 'unknown'."""
    facts: dict[str, str] = {"commit": "unknown", "branch": "unknown", "dirty": "unknown"}
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
        if commit.returncode == 0:
            facts["commit"] = commit.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10
        )
        if branch.returncode == 0:
            facts["branch"] = branch.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, timeout=10
        )
        if status.returncode == 0:
            facts["dirty"] = "yes" if status.stdout.strip() else "no"
    except (OSError, subprocess.SubprocessError):
        pass
    return facts


def sqlite_version() -> str:
    import sqlite3

    return sqlite3.sqlite_version


def build_fixture(symbols: tuple[str, ...], days: int, start: date, seed: int) -> dict[str, pd.DataFrame]:
    """Deterministic synthetic OHLCV per symbol (seeded random walk, no network)."""
    rng = np.random.default_rng(seed)
    trading_days: list[date] = []
    d = start
    while len(trading_days) < days:
        if d.weekday() < 5:  # Mon-Fri approximation of the calendar; fixtures only
            trading_days.append(d)
        d += timedelta(days=1)
    frames: dict[str, pd.DataFrame] = {}
    for i, code in enumerate(symbols):
        n = len(trading_days)
        base = 10.0 + i * 5.0
        rets = rng.normal(0.0004, 0.018, n)
        close = base * np.cumprod(1.0 + rets)
        open_ = np.empty(n)
        open_[0] = close[0]
        open_[1:] = close[:-1]
        high = np.maximum(open_, close) * (1.0 + rng.uniform(0.0, 0.012, n))
        low = np.minimum(open_, close) * (1.0 - rng.uniform(0.0, 0.012, n))
        volume = rng.integers(1_000_000, 30_000_000, n).astype(np.int64)
        frames[code] = pd.DataFrame(
            {
                "open": open_.round(2),
                "high": high.round(2),
                "low": low.round(2),
                "close": close.round(2),
                "volume": volume,
                "openinterest": np.zeros(n, dtype=np.int64),
            },
            index=pd.DatetimeIndex(trading_days, name="datetime"),
        )
    return frames


class SmaCrossStrategy(bt.Strategy):
    """Minimal SMA crossover strategy used only by the offline PoC."""

    params = (("fast", 10), ("slow", 30), ("size", 100))

    def __init__(self) -> None:
        self.fast = bt.ind.SMA(period=self.p.fast)
        self.slow = bt.ind.SMA(period=self.p.slow)
        self.cross = bt.ind.CrossOver(self.fast, self.slow)
        self.trades_seen: list[float] = []

    def next(self) -> None:
        if not self.position:
            if self.cross[0] > 0:
                self.buy(size=self.p.size)
        elif self.cross[0] < 0:
            self.close()

    def notify_trade(self, trade: bt.Trade) -> None:
        if trade.isclosed:
            self.trades_seen.append(float(trade.pnlcomm))


def run_cerebro(frames: dict[str, pd.DataFrame], cash: float) -> tuple[dict[str, Any], list[str]]:
    """Build and run one Cerebro over the fixture; return analysis + warnings."""
    warnings_log: list[str] = []
    cerebro = bt.Cerebro(stdstats=False)
    cerebro.broker.setcash(cash)
    cerebro.addstrategy(SmaCrossStrategy)
    for code, frame in frames.items():
        data = bt.feeds.PandasData(
            dataname=frame,
            datetime=None,  # use DataFrame index as datetime
            openinterest=-1,
        )
        cerebro.adddata(data, name=code)
    cerebro.addanalyzer(bt.analyzers.Returns, _name="returns")
    cerebro.addanalyzer(bt.analyzers.DrawDown, _name="drawdown")
    cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", timeframe=bt.TimeFrame.Days, riskfreerate=0.0)
    cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        results = cerebro.run()
    for item in caught:
        message = f"{item.category.__name__}: {item.message}"
        if message not in warnings_log:
            warnings_log.append(message)
    strat = results[0]
    analysis: dict[str, Any] = {
        "final_value": round(float(cerebro.broker.getvalue()), 4),
        "returns": dict(strat.analyzers.returns.get_analysis()),
        "drawdown": dict(strat.analyzers.drawdown.get_analysis()),
        "sharpe": dict(strat.analyzers.sharpe.get_analysis()),
        "trades": dict(strat.analyzers.trades.get_analysis()),
    }
    return analysis, warnings_log


def run_poc(frames: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Run the same input twice and verify determinism."""
    cash = 100_000.0
    started = time.perf_counter()
    first, first_warnings = run_cerebro(frames, cash)
    startup_seconds = time.perf_counter() - started

    started = time.perf_counter()
    second, second_warnings = run_cerebro(frames, cash)
    run_seconds = time.perf_counter() - started

    first_hash = hashlib.sha256(json.dumps(first, sort_keys=True).encode()).hexdigest()[:16]
    second_hash = hashlib.sha256(json.dumps(second, sort_keys=True).encode()).hexdigest()[:16]

    trade_count = 0
    trades = first.get("trades", {})
    total = trades.get("total", {})
    if isinstance(total, dict):
        trade_count = int(total.get("closed", 0))

    return {
        "fixture": {
            "symbols": len(frames),
            "days_per_symbol": len(next(iter(frames.values()))),
            "seed": FIXTURE_SEED,
            "start": min(f.index.min().date().isoformat() for f in frames.values()),
            "end": max(f.index.max().date().isoformat() for f in frames.values()),
        },
        "cerebro": {
            "feed_count": len(frames),
            "initial_cash": cash,
            "final_value_first": first["final_value"],
            "final_value_second": second["final_value"],
            "hash_first": first_hash,
            "hash_second": second_hash,
            "deterministic": first_hash == second_hash,
            "trade_count": trade_count,
            "warnings": first_warnings + [w for w in second_warnings if w not in first_warnings],
        },
        "timing": {
            "first_run_seconds": round(startup_seconds, 4),
            "second_run_seconds": round(run_seconds, 4),
            "peak_rss_mb": peak_rss_mb(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="P5A-0 Backtrader offline PoC")
    parser.add_argument("--no-save", action="store_true", help="print JSON to stdout only")
    args = parser.parse_args()

    frames = build_fixture(SYMBOLS, FIXTURE_DAYS, FIXTURE_START, FIXTURE_SEED)
    poc = run_poc(frames)

    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": "p5a-0-poc",
        "generated_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "environment": {
            "machine": platform.platform(),
            "python": platform.python_version(),
            "sqlite": sqlite_version(),
            "backtrader": bt.__version__,
            "pandas": pd.__version__,
            "numpy": np.__version__,
            **git_facts(),
        },
        "poc": poc,
    }
    payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(payload)
    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"{date.today().isoformat()}_p5a_poc.json"
        out.write_text(payload + "\n", encoding="utf-8")
        print(f"saved: {out}", file=sys.stderr)
    return 0 if poc["cerebro"]["deterministic"] else 2


if __name__ == "__main__":
    sys.exit(main())
