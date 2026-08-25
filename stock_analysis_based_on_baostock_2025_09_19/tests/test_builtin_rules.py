import json
from pathlib import Path

from stock_manager.rules.base import WindowUnit
from stock_manager.rules.builtin import build_default_registry
from stock_manager.templates.compiler import TemplateCompiler
from stock_manager.templates.models import parse_template


def test_default_registry_contains_all_builtin_rules() -> None:
    ids = tuple(item.rule_id for item in build_default_registry().definitions())

    assert ids == (
        "annual_min_volume",
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

    assert len(plan.enabled_rules) == 7
    annual = next(item for item in plan.enabled_rules if item.rule_id == "annual_min_volume")
    assert annual.data_requirement.market_history_unit is WindowUnit.CALENDAR_DAYS
    assert annual.data_requirement.history_length == 365
    signal = next(group for group in plan.composition.groups if group.group_id == "signal")
    assert "annual_min_volume" in signal.rule_ids
