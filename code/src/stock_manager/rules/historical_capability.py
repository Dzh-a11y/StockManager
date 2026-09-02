"""Historical capability validation for rules and templates (P5A-2).

Every enabled rule declares a RulePitCapability; PIT_UNSUPPORTED rules must be
rejected explicitly for historical backtests, and data-dependent capabilities
(FUNDAMENTAL / UNIVERSE_STATE) are checked against the local dataset coverage
before a historical run starts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from stock_manager.domain import AdjustmentMethod, DataCoverageStatus
from stock_manager.read.historical import PointInTimeReaderProtocol
from stock_manager.rules.base import RulePitCapability, ScreeningRule
from stock_manager.templates.models import ScreeningPlan


class UnsupportedRuleForHistoricalRunError(RuntimeError):
    """Raised when a template enables a rule that cannot run lookahead-free."""


class HistoricalCapabilityError(RuntimeError):
    """Raised when required point-in-time data is unavailable."""


@dataclass(frozen=True, slots=True)
class HistoricalCapabilityReport:
    """Per-rule capability plus data readiness for one historical run."""

    rule_capabilities: tuple[tuple[str, RulePitCapability], ...]
    rejected_rules: tuple[str, ...]
    data_warnings: tuple[str, ...]
    ready: bool


class RuleRegistryView(Protocol):
    """Minimal registry surface the validator needs."""

    def get(self, rule_id: str) -> ScreeningRule: ...


class HistoricalCapabilityValidator:
    """Checks template rules and dataset coverage before a historical run."""

    def __init__(self, registry: RuleRegistryView) -> None:
        self._registry = registry

    def validate(
        self,
        plan: ScreeningPlan,
        reader: PointInTimeReaderProtocol,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> HistoricalCapabilityReport:
        """Return per-rule capabilities; reject PIT_UNSUPPORTED rules."""
        capabilities: list[tuple[str, RulePitCapability]] = []
        rejected: list[str] = []
        for configured in plan.enabled_rules:
            definition = self._registry.get(configured.rule_id).definition
            capability = definition.pit_capability
            capabilities.append((configured.rule_id, capability))
            if capability is RulePitCapability.PIT_UNSUPPORTED:
                rejected.append(configured.rule_id)
        if rejected:
            return HistoricalCapabilityReport(
                tuple(capabilities),
                tuple(rejected),
                (),
                False,
            )
        warnings_list: list[str] = self._data_warnings(
            plan, reader, dataset_id=dataset_id, adjustment=adjustment
        )
        return HistoricalCapabilityReport(
            tuple(capabilities),
            (),
            tuple(warnings_list),
            not warnings_list,
        )

    def _data_warnings(
        self,
        plan: ScreeningPlan,
        reader: PointInTimeReaderProtocol,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> list[str]:
        """Warn when a capability's data source is missing from coverage."""
        warnings_list: list[str] = []
        capabilities = {
            configured.rule_id
            for configured in plan.enabled_rules
        }
        for configured in plan.enabled_rules:
            definition = self._registry.get(configured.rule_id).definition
            capability = definition.pit_capability
            if capability is RulePitCapability.FUNDAMENTAL_PIT_READY:
                if not self._coverage_has_data(reader, "fundamentals"):
                    warnings_list.append(
                        f"rule {configured.rule_id} requires fundamentals with "
                        "published_on, but no fundamentals coverage exists"
                    )
            if capability is RulePitCapability.UNIVERSE_STATE_PIT_READY:
                if not self._coverage_has_data(reader, "stocks"):
                    warnings_list.append(
                        f"rule {configured.rule_id} requires historical stock "
                        "universe snapshots, but no stocks coverage exists"
                    )
        return warnings_list

    @staticmethod
    def _coverage_has_data(
        reader: PointInTimeReaderProtocol, data_type: str
    ) -> bool:
        """True when the data type has any stored rows (coverage not UNAVAILABLE)."""
        request = getattr(reader, "request", None)
        if request is None:
            return True
        try:
            if data_type == "fundamentals":
                items = reader.fundamentals_through(request.end)
            elif data_type == "stocks":
                items = reader.universe_as_of(request.start)
            else:  # pragma: no cover - internal guard
                return False
        except Exception:
            return False
        return len(items) > 0

    def require_ready(
        self,
        plan: ScreeningPlan,
        reader: PointInTimeReaderProtocol,
        *,
        dataset_id: str,
        adjustment: AdjustmentMethod,
    ) -> None:
        """Raise when the plan cannot run historically; otherwise no-op."""
        report = self.validate(
            plan, reader, dataset_id=dataset_id, adjustment=adjustment
        )
        if not report.ready:
            if report.rejected_rules:
                raise UnsupportedRuleForHistoricalRunError(
                    "rules not supported for historical runs: "
                    + ", ".join(sorted(report.rejected_rules))
                )
            raise HistoricalCapabilityError(
                "historical run blocked by missing data: "
                + "; ".join(sorted(report.data_warnings))
            )
