"""Explicit composition of screening rule results."""

from collections.abc import Sequence

from stock_manager.domain import RuleResult


RULE_ID = "composite"
FUNDAMENTAL_RULES = frozenset({"pe_positive", "non_st"})
TECHNICAL_TRIGGER_RULES = frozenset({"volume_price_5d", "limit_up_breakout"})
TECHNICAL_REQUIRED_RULES = frozenset({"limit_up_3m", "volatility_multiple"})
REQUIRED_RULES = FUNDAMENTAL_RULES | TECHNICAL_TRIGGER_RULES | TECHNICAL_REQUIRED_RULES


def evaluate_composite(results: Sequence[RuleResult]) -> RuleResult:
    """Apply the documented legacy composition using explicit rule identifiers."""
    by_id = {result.rule_id: result for result in results}
    if len(by_id) != len(results):
        raise ValueError("composite rule results must have unique rule_id values")
    missing = REQUIRED_RULES - by_id.keys()
    if missing:
        raise ValueError(f"missing required rule results: {', '.join(sorted(missing))}")
    fundamentals_pass = all(by_id[rule_id].passed for rule_id in FUNDAMENTAL_RULES)
    trigger_pass = any(by_id[rule_id].passed for rule_id in TECHNICAL_TRIGGER_RULES)
    technical_required_pass = all(by_id[rule_id].passed for rule_id in TECHNICAL_REQUIRED_RULES)
    passed = fundamentals_pass and trigger_pass and technical_required_pass
    actual = {rule_id: by_id[rule_id].passed for rule_id in sorted(REQUIRED_RULES)}
    threshold = {
        "all": sorted(FUNDAMENTAL_RULES | TECHNICAL_REQUIRED_RULES),
        "any": sorted(TECHNICAL_TRIGGER_RULES),
    }
    reason = "all composite conditions passed" if passed else "one or more composite conditions failed"
    return RuleResult(RULE_ID, passed, actual, threshold, reason)
