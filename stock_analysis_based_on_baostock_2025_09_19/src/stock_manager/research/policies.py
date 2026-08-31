"""Policy registry for research strategies (P5A-3).

Policies are referenced by stable ID + version; parameters are validated
against a declared whitelist schema so arbitrary import paths, unknown IDs
and unknown parameter keys are rejected before any backtest runs.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stock_manager.research.models import (
    PolicyKind,
    PolicyParameterSpec,
    PolicyParameterType,
    PolicySpec,
)


class UnknownPolicyError(ValueError):
    """Unknown policy id/version/kind combination."""


class InvalidPolicyParametersError(ValueError):
    """Policy parameters violate the declared schema."""


@dataclass(frozen=True, slots=True)
class PolicyDefinition:
    policy_id: str
    kind: PolicyKind
    version: int
    description: str
    parameters: tuple[PolicyParameterSpec, ...]

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise ValueError("policy_id must not be empty")
        if not self.description.strip():
            raise ValueError("description must not be empty")
        if self.version <= 0:
            raise ValueError("version must be positive")
        ids = tuple(item.parameter_id for item in self.parameters)
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate parameter_id in policy {self.policy_id}")


class PolicyRegistry:
    """Explicit registry; policies must be registered before use."""

    def __init__(self, definitions: Iterable[PolicyDefinition] = ()) -> None:
        self._definitions: dict[tuple[str, PolicyKind, int], PolicyDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: PolicyDefinition) -> None:
        key = (definition.policy_id, definition.kind, definition.version)
        if key in self._definitions:
            raise ValueError(f"duplicate policy definition: {key}")
        self._definitions[key] = definition

    def get(
        self, policy_id: str, kind: PolicyKind, version: int
    ) -> PolicyDefinition:
        try:
            return self._definitions[(policy_id, kind, version)]
        except KeyError as error:
            raise UnknownPolicyError(
                f"unknown policy {kind.value}/{policy_id} v{version}"
            ) from error

    def definitions(self) -> tuple[PolicyDefinition, ...]:
        return tuple(
            self._definitions[key]
            for key in sorted(
                self._definitions,
                key=lambda item: (item[1].value, item[0], item[2]),
            )
        )

    def validate_spec(self, spec: PolicySpec, kind: PolicyKind) -> None:
        """Validate a spec against its registered definition and schema."""
        definition = self.get(spec.policy_id, kind, spec.version)
        self._validate_parameters(
            definition, dict(spec.parameters), f"{kind.value}/{spec.policy_id}"
        )

    def validate_strategy(
        self,
        *,
        entry: PolicySpec,
        exit: PolicySpec,
        rebalance: PolicySpec,
        allocation: PolicySpec,
        ranking: PolicySpec,
        execution: PolicySpec,
    ) -> None:
        """Validate every policy of one strategy spec; raises on any mismatch."""
        self.validate_spec(entry, PolicyKind.ENTRY)
        self.validate_spec(exit, PolicyKind.EXIT)
        self.validate_spec(rebalance, PolicyKind.REBALANCE)
        self.validate_spec(allocation, PolicyKind.ALLOCATION)
        self.validate_spec(ranking, PolicyKind.RANKING)
        self.validate_spec(execution, PolicyKind.EXECUTION)

    @staticmethod
    def _validate_parameters(
        definition: PolicyDefinition,
        parameters: Mapping[str, Any],
        label: str,
    ) -> None:
        specs = {item.parameter_id: item for item in definition.parameters}
        unknown = set(parameters) - set(specs)
        if unknown:
            raise InvalidPolicyParametersError(
                f"{label} has unknown parameter(s): {', '.join(sorted(unknown))}"
            )
        missing = {pid for pid, item in specs.items() if item.required and pid not in parameters}
        if missing:
            raise InvalidPolicyParametersError(
                f"{label} is missing required parameter(s): {', '.join(sorted(missing))}"
            )
        for parameter_id, value in parameters.items():
            spec = specs[parameter_id]
            _validate_value(spec, value, label, parameter_id)


def _validate_value(
    spec: PolicyParameterSpec,
    value: object,
    label: str,
    parameter_id: str,
) -> None:
    field = f"{label}.{parameter_id}"
    if spec.value_type is PolicyParameterType.INTEGER:
        if not isinstance(value, int) or isinstance(value, bool):
            raise InvalidPolicyParametersError(f"{field} must be an integer")
        if spec.minimum is not None and value < spec.minimum:
            raise InvalidPolicyParametersError(
                f"{field} must be >= {spec.minimum}"
            )
        if spec.maximum is not None and value > spec.maximum:
            raise InvalidPolicyParametersError(
                f"{field} must be <= {spec.maximum}"
            )
    elif spec.value_type is PolicyParameterType.DECIMAL:
        try:
            decimal_value = Decimal(str(value))
        except Exception as error:
            raise InvalidPolicyParametersError(
                f"{field} must be decimal"
            ) from error
        if spec.minimum is not None and decimal_value < Decimal(str(spec.minimum)):
            raise InvalidPolicyParametersError(
                f"{field} must be >= {spec.minimum}"
            )
        if spec.maximum is not None and decimal_value > Decimal(str(spec.maximum)):
            raise InvalidPolicyParametersError(
                f"{field} must be <= {spec.maximum}"
            )
    elif spec.value_type is PolicyParameterType.BOOLEAN:
        if not isinstance(value, bool):
            raise InvalidPolicyParametersError(f"{field} must be a boolean")
    elif spec.value_type is PolicyParameterType.TEXT:
        if not isinstance(value, str) or not value.strip():
            raise InvalidPolicyParametersError(
                f"{field} must be a non-empty string"
            )
