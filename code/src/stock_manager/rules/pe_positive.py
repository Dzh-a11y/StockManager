"""Positive PE TTM rule."""

from __future__ import annotations

from decimal import Decimal

from stock_manager.domain import FundamentalSnapshot, RuleResult


RULE_ID = "pe_positive"


def evaluate_pe_positive(
    snapshot: FundamentalSnapshot | None,
    minimum_exclusive: Decimal,
) -> RuleResult:
    """Pass only when a PE TTM value exists and is above the configured minimum."""
    value = None if snapshot is None else snapshot.pe_ttm
    passed = value is not None and value > minimum_exclusive
    reason = (
        f"PE TTM {value} is above {minimum_exclusive}"
        if passed
        else "PE TTM is missing" if value is None else f"PE TTM {value} is not above {minimum_exclusive}"
    )
    return RuleResult(RULE_ID, passed, value, {"exclusive_minimum": minimum_exclusive}, reason)
