"""Screen API response builder (P3-4)."""

from __future__ import annotations

from datetime import date

from stock_manager.domain import (
    AdjustmentMethod,
    DatasetMetadata,
    ParameterizedScreeningResult,
    RuleExecutionResult,
    RuleResult,
)
from stock_manager.templates.models import ScreeningPlan
from stock_manager.web.serialization import to_jsonable


def screen_response(
    dataset_id: str,
    trading_day: date,
    adjustment: AdjustmentMethod,
    plan: ScreeningPlan,
    metadata: DatasetMetadata,
    results: tuple[ParameterizedScreeningResult, ...],
) -> dict[str, object]:
    passed = sum(1 for item in results if item.passed)
    return {
        "template_id": plan.template_id,
        "template_revision": plan.revision,
        "dataset_id": dataset_id,
        "trading_day": trading_day.isoformat(),
        "adjustment": adjustment.value,
        "metadata": to_jsonable(metadata),
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": len(results) - passed,
        },
        "results": [result_to_dict(item) for item in results],
    }


def result_to_dict(result: ParameterizedScreeningResult) -> dict[str, object]:
    return {
        "code": result.code,
        "name": result.name,
        "trading_day": result.trading_day.isoformat(),
        "passed": result.passed,
        "rule_executions": [
            execution_to_dict(item) for item in result.rule_executions
        ],
    }


def execution_to_dict(execution: RuleExecutionResult) -> dict[str, object]:
    return {
        "rule_id": execution.rule_id,
        "status": execution.status.value,
        "result": (
            None if execution.result is None else rule_result_to_dict(execution.result)
        ),
    }


def rule_result_to_dict(result: RuleResult) -> dict[str, object]:
    return {
        "passed": result.passed,
        "actual_value": to_jsonable(result.actual_value),
        "threshold": to_jsonable(result.threshold),
        "reason": result.reason,
    }
