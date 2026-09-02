"""Historical signal cache keys and lookup (P5A-5).

The cache key covers everything that changes pure eligibility: dataset +
immutable generation, adjustment, universe policy, evaluation window and
schedule, template id/revision, plan fingerprint, rule implementation version
and the trading-calendar fingerprint. Strategy-level parameters (fees, initial
cash, SMA periods, holding days) must NOT enter the key: changing them reuses
the same signals.
"""

from __future__ import annotations

from datetime import date

from stock_manager.domain import AdjustmentMethod
from stock_manager.research.fingerprint import canonical_json
from stock_manager.research.models import EvaluationSchedule

import hashlib


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def historical_cache_key(
    *,
    dataset_id: str,
    generation: str | None,
    adjustment: AdjustmentMethod,
    universe_policy: str,
    evaluation_start: date,
    evaluation_end: date,
    schedule: EvaluationSchedule,
    template_id: str,
    template_revision: int,
    plan_fingerprint: str,
    rule_implementation_version: str,
    calendar_fingerprint: str,
) -> str:
    """Stable cache key for one historical eligibility run."""
    if not dataset_id.strip():
        raise ValueError("dataset_id must not be empty")
    if template_revision <= 0:
        raise ValueError("template_revision must be positive")
    payload = canonical_json(
        {
            "dataset_id": dataset_id,
            "generation": generation,
            "adjustment": adjustment.value,
            "universe_policy": universe_policy,
            "evaluation_start": evaluation_start.isoformat(),
            "evaluation_end": evaluation_end.isoformat(),
            "schedule": schedule.value,
            "template_id": template_id,
            "template_revision": template_revision,
            "plan_fingerprint": plan_fingerprint,
            "rule_implementation_version": rule_implementation_version,
            "calendar_fingerprint": calendar_fingerprint,
        }
    )
    return _digest(payload)
