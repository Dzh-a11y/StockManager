"""Pure, offline screening rules."""

from __future__ import annotations

from stock_manager.rules.annual_min_close_price import evaluate_annual_min_close_price
from stock_manager.rules.annual_min_volume import evaluate_annual_min_volume
from stock_manager.rules.builtin import build_default_registry
from stock_manager.rules.composite import evaluate_composite
from stock_manager.rules.config import RulesConfig, load_rules_config
from stock_manager.rules.consecutive_up_days import evaluate_consecutive_up_days
from stock_manager.rules.limit_up_3m import evaluate_limit_up_3m
from stock_manager.rules.limit_up_breakout import evaluate_limit_up_breakout
from stock_manager.rules.n_day_close_above import evaluate_n_day_close_above
from stock_manager.rules.non_st import evaluate_non_st
from stock_manager.rules.pe_positive import evaluate_pe_positive
from stock_manager.rules.price_range_ratio import evaluate_price_range_ratio
from stock_manager.rules.volatility_multiple import evaluate_volatility_multiple
from stock_manager.rules.volume_price_5d import evaluate_volume_price_5d
from stock_manager.rules.volume_sum_extreme import evaluate_volume_sum_extreme

__all__ = [
    "build_default_registry",
    "evaluate_annual_min_close_price",
    "evaluate_annual_min_volume",
    "evaluate_composite",
    "evaluate_consecutive_up_days",
    "evaluate_limit_up_3m",
    "evaluate_limit_up_breakout",
    "evaluate_n_day_close_above",
    "evaluate_non_st",
    "evaluate_pe_positive",
    "evaluate_price_range_ratio",
    "evaluate_volatility_multiple",
    "evaluate_volume_price_5d",
    "evaluate_volume_sum_extreme",
    "load_rules_config",
    "RulesConfig",
]
