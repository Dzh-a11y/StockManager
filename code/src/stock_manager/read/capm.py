"""Generation-gated CAPM inputs from one read-only SQLite snapshot."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from stock_manager.domain import AdjustmentMethod, DepositRate, ReadinessStatus
from stock_manager.sync.committer import ReadinessGate


@dataclass(frozen=True, slots=True)
class CapmInputs:
    stock_levels: tuple[tuple[date, Decimal], ...]
    market_levels: tuple[tuple[date, Decimal], ...]
    rates: tuple[DepositRate, ...]
    stock_generation: str
    reference_generation: str


class SQLiteCapmReader:
    def __init__(self, database_path: Path) -> None:
        self.path = database_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def read(self, stock_code: str, benchmark_id: str, rate_term: str,
             start: date, end: date) -> CapmInputs:
        gate = ReadinessGate(self._connect)
        reference = gate.evaluate(dataset_id="capm", adjustment=AdjustmentMethod.UNADJUSTED,
            required_data_types=("index_catalog", "index_daily_bars", "deposit_rates"),
            requested_start=start, requested_end=end)
        if reference.status is not ReadinessStatus.READY:
            raise ValueError(f"CAPM reference data not ready: {reference.reason}")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN")
            active = {r["dataset_id"]: r["generation"] for r in connection.execute(
                """SELECT dataset_id, generation FROM active_generations
                   WHERE (dataset_id='capm' AND adjustment='unadjusted')
                      OR (dataset_id='market' AND adjustment='qfq')""")}
            if active.get("capm") != reference.generation:
                raise ValueError("CAPM reference generation changed; please run the analysis again")
            if "market" not in active:
                raise ValueError("stock screening dataset has no published qfq generation")
            params = (start.isoformat(), end.isoformat())
            stocks = connection.execute(
                """SELECT trading_day, close FROM daily_bars b WHERE code=? AND adjustment='qfq'
                   AND trading_day BETWEEN ? AND ? AND is_trading=1 AND EXISTS (
                   SELECT 1 FROM generation_partitions g WHERE g.generation=? AND g.batch_id=b.batch_id)
                   ORDER BY trading_day""", (stock_code, *params, active["market"]),
            ).fetchall()
            market = connection.execute(
                """SELECT trading_day, close FROM index_daily_bars b WHERE index_id=?
                   AND trading_day BETWEEN ? AND ? AND EXISTS (SELECT 1 FROM generation_partitions g
                   WHERE g.generation=? AND g.batch_id=b.batch_id) ORDER BY trading_day""",
                (benchmark_id, *params, active["capm"]),
            ).fetchall()
            rates = connection.execute(
                """SELECT term, effective_on, annual_rate, source FROM deposit_rates b
                   WHERE term=? AND effective_on<=? AND EXISTS (SELECT 1 FROM generation_partitions g
                   WHERE g.generation=? AND g.batch_id=b.batch_id) ORDER BY effective_on""",
                (rate_term, end.isoformat(), active["capm"]),
            ).fetchall()
            expected = {r[0] for r in connection.execute(
                "SELECT trading_day FROM trading_days WHERE trading_day BETWEEN ? AND ?", params)}
            actual = {r["trading_day"] for r in market}
            if actual != expected or not actual:
                raise ValueError(f"index {benchmark_id} dates incomplete: {sorted(expected - actual)[:10]}")
        return CapmInputs(
            tuple((date.fromisoformat(r[0]), Decimal(r[1])) for r in stocks),
            tuple((date.fromisoformat(r[0]), Decimal(r[1])) for r in market),
            tuple(DepositRate(r[0], date.fromisoformat(r[1]), Decimal(r[2]), r[3]) for r in rates),
            active["market"], active["capm"],
        )
