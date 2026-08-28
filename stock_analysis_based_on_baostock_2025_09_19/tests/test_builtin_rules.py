from __future__ import annotations

import json
from pathlib import Path

from stock_manager.rules.base import WindowUnit
from stock_manager.rules.builtin import build_default_registry
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template


def test_default_registry_contains_all_builtin_rules() -> None:
    ids = tuple(item.rule_id for item in build_default_registry().definitions())

    assert ids == (
        "annual_min_close_price",
        "annual_min_volume",
        "avg_close_above",
        "consecutive_up_days",
        "limit_up_3m",
        "limit_up_breakout",
        "non_st",
        "pe_positive",
        "volatility_multiple",
        "volume_price_5d",
    )


def test_system_default_template_compiles_all_rules() -> None:
    path = Path(__file__).parents[1] / "config/rule_templates/system-default.json"
    raw = json.loads(path.read_text(encoding="utf-8"))

    plan = TemplateCompiler(build_default_registry()).compile(parse_template(raw))

    # 新增两条规则默认关闭,系统默认策略行为保持不变。
    assert len(plan.enabled_rules) == 8
    assert "consecutive_up_days" in plan.disabled_rule_ids
    assert "avg_close_above" in plan.disabled_rule_ids
    annual = next(item for item in plan.enabled_rules if item.rule_id == "annual_min_volume")
    assert annual.data_requirement.market_history_unit is WindowUnit.CALENDAR_DAYS
    assert annual.data_requirement.history_length == 365
    close = next(
        item for item in plan.enabled_rules if item.rule_id == "annual_min_close_price"
    )
    assert close.data_requirement.market_history_unit is WindowUnit.CALENDAR_DAYS
    assert close.data_requirement.history_length == 365
    signal = next(group for group in plan.composition.groups if group.group_id == "signal")
    assert "annual_min_volume" in signal.rule_ids
    assert "annual_min_close_price" in signal.rule_ids


def test_new_rules_declare_trading_session_history() -> None:
    registry = build_default_registry()

    up = registry.get("consecutive_up_days")
    up_parameters = up.parse_parameters({"lookback_trading_sessions": 5})
    up_requirement = up.data_requirement(up_parameters)
    assert up_requirement.market_history_unit is WindowUnit.TRADING_SESSIONS
    # 连阳需要窗口首日的参照日,因此多取 1 个交易日。
    assert up_requirement.history_length == 6

    avg = registry.get("avg_close_above")
    avg_parameters = avg.parse_parameters(
        {"lookback_trading_sessions": 5, "minimum_average_close": "10"}
    )
    avg_requirement = avg.data_requirement(avg_parameters)
    assert avg_requirement.market_history_unit is WindowUnit.TRADING_SESSIONS
    assert avg_requirement.history_length == 5
