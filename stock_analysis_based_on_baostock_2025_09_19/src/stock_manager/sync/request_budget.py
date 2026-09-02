"""Persistent request budget and blacklist circuit for external providers."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
BAOSTOCK_DAILY_HARD_LIMIT = 50_000


class ProviderRequestBudgetExceededError(RuntimeError):
    """Raised before a request would exceed the configured daily budget."""


class ProviderCircuitOpenError(RuntimeError):
    """Raised before a request while a persisted provider circuit is open."""


@dataclass(frozen=True, slots=True)
class ProviderBudgetStatus:
    """Current persisted request count and blacklist circuit state."""

    source: str
    request_day: str
    request_count: int
    soft_limit: int
    hard_limit: int
    circuit_open_until: datetime | None
    circuit_reason: str | None
    blacklist_count: int


class SQLiteProviderRequestBudget:
    """Atomically reserve provider requests across processes and restarts."""

    def __init__(
        self,
        database_path: Path,
        *,
        source: str,
        soft_limit: int = 45_000,
        hard_limit: int = BAOSTOCK_DAILY_HARD_LIMIT,
        now: Callable[[], datetime],
    ) -> None:
        if not source.strip():
            raise ValueError("source must not be empty")
        if soft_limit <= 0:
            raise ValueError("soft_limit must be positive")
        if hard_limit <= 0 or hard_limit > BAOSTOCK_DAILY_HARD_LIMIT:
            raise ValueError(
                f"hard_limit must be within 1..{BAOSTOCK_DAILY_HARD_LIMIT}"
            )
        if soft_limit > hard_limit:
            raise ValueError("soft_limit must not exceed hard_limit")
        self._database_path = database_path
        self._source = source
        self._soft_limit = soft_limit
        self._hard_limit = hard_limit
        self._now = now

    def before_request(self, operation: str) -> None:
        """Reserve one request before the SDK call starts."""
        if not operation.strip():
            raise ValueError("operation must not be empty")
        now = self._local_now()
        request_day = now.date().isoformat()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            breaker = connection.execute(
                """SELECT circuit_open_until, reason
                   FROM provider_circuit_breakers WHERE source = ?""",
                (self._source,),
            ).fetchone()
            if breaker is not None and breaker["circuit_open_until"] is not None:
                open_until = datetime.fromisoformat(breaker["circuit_open_until"])
                if open_until > now:
                    connection.rollback()
                    raise ProviderCircuitOpenError(
                        f"provider circuit is open until {open_until.isoformat()}: "
                        f"{breaker['reason']}"
                    )
            row = connection.execute(
                """SELECT request_count FROM provider_request_ledger
                   WHERE source = ? AND request_day = ?""",
                (self._source, request_day),
            ).fetchone()
            count = 0 if row is None else int(row["request_count"])
            if count >= self._soft_limit:
                connection.rollback()
                raise ProviderRequestBudgetExceededError(
                    f"{self._source} daily request soft limit "
                    f"{self._soft_limit} reached before {operation}; "
                    f"hard limit is {self._hard_limit}"
                )
            connection.execute(
                """INSERT INTO provider_request_ledger
                       (source, request_day, request_count, updated_at)
                   VALUES (?, ?, 1, ?)
                   ON CONFLICT(source, request_day) DO UPDATE SET
                     request_count = request_count + 1,
                     updated_at = excluded.updated_at""",
                (self._source, request_day, now.isoformat()),
            )
            connection.commit()
        except (sqlite3.Error, ProviderCircuitOpenError, ProviderRequestBudgetExceededError):
            connection.rollback()
            raise
        finally:
            connection.close()

    def trip_blacklist(self, error_code: str, message: str) -> None:
        """Persist a year-escalating blacklist circuit (six hours per hit)."""
        now = self._local_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT occurrence_year, occurrence_count
                   FROM provider_circuit_breakers WHERE source = ?""",
                (self._source,),
            ).fetchone()
            if row is None or int(row["occurrence_year"]) != now.year:
                count = 1
            else:
                count = int(row["occurrence_count"]) + 1
            open_until = now + timedelta(hours=6 * count)
            reason = f"{error_code}: {message}"
            connection.execute(
                """INSERT INTO provider_circuit_breakers
                       (source, circuit_open_until, reason, occurrence_year,
                        occurrence_count, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source) DO UPDATE SET
                     circuit_open_until = excluded.circuit_open_until,
                     reason = excluded.reason,
                     occurrence_year = excluded.occurrence_year,
                     occurrence_count = excluded.occurrence_count,
                     updated_at = excluded.updated_at""",
                (
                    self._source,
                    open_until.isoformat(),
                    reason,
                    now.year,
                    count,
                    now.isoformat(),
                ),
            )
            connection.commit()
        except sqlite3.Error:
            connection.rollback()
            raise
        finally:
            connection.close()

    def status(self) -> ProviderBudgetStatus:
        """Return today's count plus the persisted circuit state."""
        now = self._local_now()
        request_day = now.date().isoformat()
        connection = self._connect()
        try:
            ledger = connection.execute(
                """SELECT request_count FROM provider_request_ledger
                   WHERE source = ? AND request_day = ?""",
                (self._source, request_day),
            ).fetchone()
            breaker = connection.execute(
                """SELECT circuit_open_until, reason, occurrence_count
                   FROM provider_circuit_breakers WHERE source = ?""",
                (self._source,),
            ).fetchone()
        finally:
            connection.close()
        return ProviderBudgetStatus(
            source=self._source,
            request_day=request_day,
            request_count=0 if ledger is None else int(ledger["request_count"]),
            soft_limit=self._soft_limit,
            hard_limit=self._hard_limit,
            circuit_open_until=(
                None
                if breaker is None or breaker["circuit_open_until"] is None
                else datetime.fromisoformat(breaker["circuit_open_until"])
            ),
            circuit_reason=None if breaker is None else breaker["reason"],
            blacklist_count=(
                0 if breaker is None else int(breaker["occurrence_count"])
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _local_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("request budget clock must return an aware datetime")
        return value.astimezone(SHANGHAI)
