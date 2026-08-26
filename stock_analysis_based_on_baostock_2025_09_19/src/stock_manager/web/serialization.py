"""Deterministic JSON-safe serialization for domain and web payloads."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


def to_jsonable(value: object) -> object:
    """Recursively convert domain/dataclass/Decimal/Enum/date values to JSON-safe types.

    - dataclass -> dict
    - Mapping (incl. MappingProxyType) -> dict with str keys
    - tuple/list -> list
    - date/datetime -> ISO 8601 string
    - Decimal -> decimal string (precision-preserving, never float)
    - Enum -> value
    - set -> sorted list
    - everything else returned as-is (None/bool/int/str or already-safe values)
    """
    if is_dataclass(value) and not isinstance(value, type):
        return {name: to_jsonable(getattr(value, name)) for name in _field_names(value)}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, set):
        return [to_jsonable(item) for item in sorted(value, key=str)]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    return value


def _field_names(value: object) -> tuple[str, ...]:
    if is_dataclass(value) and not isinstance(value, type):
        return tuple(item.name for item in fields(value))
    return ()
