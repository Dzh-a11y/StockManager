"""P5C strategy template tests: default template, repository lifecycle,
validation/normalization and revision semantics."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from stock_manager.research import build_default_policy_registry
from stock_manager.research.strategies import (
    DEFAULT_STRATEGY_ID,
    StrategyTemplate,
    StrategyTemplateError,
    StrategyTemplateRepository,
    StrategyTemplateRevisionConflictError,
    StrategyTemplateService,
    default_strategy_template,
    parse_strategy_template,
    validate_and_normalize_policies,
)

REGISTRY = build_default_policy_registry()


def test_default_template_roundtrip() -> None:
    default = default_strategy_template()
    assert default.strategy_template_id == DEFAULT_STRATEGY_ID
    parsed = parse_strategy_template(default.to_dict())
    assert parsed == default
    normalized = validate_and_normalize_policies(default.policies, REGISTRY)
    assert normalized["entry"]["operator"] == "any"
    assert len(normalized["entry"]["items"]) == 1


def test_repository_lifecycle(tmp_path: Path) -> None:
    repository = StrategyTemplateRepository(tmp_path)
    assert repository.list_ids() == ()
    template = StrategyTemplate(
        strategy_template_id="my-trend",
        revision=1,
        name="我的趋势",
        description="测试",
        policies=default_strategy_template().policies,
    )
    repository.save(template, create=True)
    assert repository.has("my-trend")
    assert repository.list_ids() == ("my-trend",)
    loaded = repository.get("my-trend")
    assert loaded is not None and loaded.revision == 1
    with pytest.raises(StrategyTemplateError, match="already exists"):
        repository.save(template, create=True)
    repository.delete("my-trend")
    assert not repository.has("my-trend")


def test_service_revision_and_conflict(tmp_path: Path) -> None:
    service = StrategyTemplateService(StrategyTemplateRepository(tmp_path), REGISTRY)
    assert service.list_ids() == (DEFAULT_STRATEGY_ID,)
    default = service.get(DEFAULT_STRATEGY_ID)
    assert service.is_system(DEFAULT_STRATEGY_ID)
    with pytest.raises(StrategyTemplateError, match="read-only"):
        service.delete(DEFAULT_STRATEGY_ID, expected_revision=1)
    template = StrategyTemplate(
        strategy_template_id="b-and-h",
        revision=1,
        name="持有策略",
        description="测试",
        policies=default.policies,
    )
    created = service.create(template)
    assert created.revision == 1
    with pytest.raises(StrategyTemplateError, match="already exists"):
        service.create(template)
    updated = service.update(created, expected_revision=1)
    assert updated.revision == 2
    with pytest.raises(StrategyTemplateRevisionConflictError):
        service.update(updated, expected_revision=1)
    service.delete("b-and-h", expected_revision=2)
    assert "b-and-h" not in service.list_ids()


def test_validation_rejects_unknown_and_group_bounds() -> None:
    policies = dict(default_strategy_template().policies)
    entry = dict(policies["entry"])  # type: ignore[arg-type]
    items = list(entry["items"])  # type: ignore[arg-type]
    items.append({"policy_id": "no_such_v1", "version": 1, "parameters": {}})
    entry["items"] = items
    policies["entry"] = entry
    with pytest.raises(StrategyTemplateError, match="unknown policy"):
        validate_and_normalize_policies(policies, REGISTRY)


def test_tiers_normalized_ascending() -> None:
    policies = dict(default_strategy_template().policies)
    policies["take_profit_tiers"] = [
        {"take_profit_ratio": "0.2", "partial_ratio": "0.5"},
        {"take_profit_ratio": "0.1", "partial_ratio": "0.5"},
    ]
    normalized = validate_and_normalize_policies(policies, REGISTRY)
    ratios = [
        Decimal(item["take_profit_ratio"]) for item in normalized["take_profit_tiers"]
    ]
    assert ratios == [Decimal("0.1"), Decimal("0.2")]
    policies["take_profit_tiers"] = [{"take_profit_ratio": "0.1", "partial_ratio": "1.5"}]
    with pytest.raises(StrategyTemplateError, match="invalid take-profit tier"):
        validate_and_normalize_policies(policies, REGISTRY)


def test_file_persistence_is_json(tmp_path: Path) -> None:
    repository = StrategyTemplateRepository(tmp_path)
    repository.save(
        StrategyTemplate(
            strategy_template_id="persist-me",
            revision=1,
            name="持久化",
            description="测试",
            policies=default_strategy_template().policies,
        ),
        create=True,
    )
    path = tmp_path / "strategies" / "persist-me.json"
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["metadata"]["revision"] == 1
    assert "take_profit_tiers" in raw["policies"]
