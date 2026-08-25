"""Non-ST stock rule."""

from stock_manager.domain import RuleResult, StockIdentity


RULE_ID = "non_st"


def evaluate_non_st(stock: StockIdentity) -> RuleResult:
    """Pass when the normalized stock identity is not marked ST."""
    passed = not stock.is_st
    reason = f"{stock.code} is not ST" if passed else f"{stock.code} is marked ST"
    return RuleResult(RULE_ID, passed, stock.is_st, {"is_st": False}, reason)
