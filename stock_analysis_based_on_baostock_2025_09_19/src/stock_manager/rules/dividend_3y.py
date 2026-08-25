"""Dividend history rule."""

from collections.abc import Sequence
from datetime import date

from stock_manager.domain import DividendRecord, RuleResult
from stock_manager.rules._shared import require_positive_integer


RULE_ID = "dividend_3y"


def evaluate_dividend_3y(
    records: Sequence[DividendRecord],
    as_of: date,
    completed_calendar_years: int,
    minimum_records: int,
) -> RuleResult:
    """Pass on enough dividend records in the configured completed calendar years."""
    require_positive_integer(completed_calendar_years, "completed_calendar_years")
    require_positive_integer(minimum_records, "minimum_records")
    if len({record.code for record in records}) > 1:
        raise ValueError("a rule evaluation must contain exactly one stock code")
    start_year = as_of.year - completed_calendar_years
    eligible = tuple(
        record
        for record in records
        if start_year <= record.ex_date.year < as_of.year and record.ex_date <= as_of
    )
    count = len(eligible)
    passed = count >= minimum_records
    threshold = {
        "start_year_inclusive": start_year,
        "end_year_exclusive": as_of.year,
        "minimum_records": minimum_records,
    }
    reason = f"found {count} eligible dividend record(s); requires at least {minimum_records}"
    return RuleResult(RULE_ID, passed, count, threshold, reason)
