from __future__ import annotations

import math
from dataclasses import fields
from functools import lru_cache
from numbers import Integral, Real
from typing import Any


@lru_cache(maxsize=64)
def _field_names(value_type: type[Any]) -> tuple[str, ...]:
    """Resolve stable dataclass field names once per contract type."""

    return tuple(item.name for item in fields(value_type))


_FLAT_IMMUTABLE_TYPES = frozenset({type(None), str, bool, int, float, tuple})
_JSON_ATOMIC_TYPES = frozenset({type(None), str, bool, int, float})
_JSON_PASSTHROUGH_TYPES = frozenset({type(None), str, bool, int})


def _copy_uncommon_flat_container(value: Any) -> Any:
    """Normalize container subclasses without taxing the primitive hot path."""

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return tuple(value)
    return value


def copy_flat_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Copy a primitive mapping and its one-level mutable containers."""

    payload = value.copy()
    for name, field_value in value.items():
        value_type = type(field_value)
        if value_type in _FLAT_IMMUTABLE_TYPES:
            continue
        elif value_type is dict:
            payload[name] = field_value.copy()
        elif value_type is list:
            payload[name] = field_value.copy()
        else:
            copied = _copy_uncommon_flat_container(field_value)
            if copied is not field_value:
                payload[name] = copied
    return payload


def flat_dataclass_dict(value: Any) -> dict[str, Any]:
    """Copy a flat slots dataclass without recursive ``asdict``/``deepcopy``."""

    payload: dict[str, Any] = {}
    for name in _field_names(type(value)):
        field_value = getattr(value, name)
        value_type = type(field_value)
        if value_type in _FLAT_IMMUTABLE_TYPES:
            pass
        elif value_type is dict:
            field_value = field_value.copy()
        elif value_type is list:
            field_value = field_value.copy()
        else:
            field_value = _copy_uncommon_flat_container(field_value)
        payload[name] = field_value
    return payload


def clone_flat_dataclass(value: Any) -> Any:
    """Clone a flat snapshot while isolating its mutable map/list fields."""

    return type(value)(**flat_dataclass_dict(value))


def copy_json_payload(value: Any) -> Any:
    """Clone an already-normalized JSON payload without validating it again."""

    value_type = type(value)
    if value_type in _JSON_ATOMIC_TYPES:
        return value
    if value_type is dict:
        return {key: copy_json_payload(item) for key, item in value.items()}
    if value_type is list:
        return [copy_json_payload(item) for item in value]
    if value_type is tuple:
        return tuple(copy_json_payload(item) for item in value)
    return value


def json_safe(value: Any) -> Any:
    """Copy nested telemetry while normalizing numeric scalar types."""

    value_type = type(value)
    if value_type is float:
        return value if math.isfinite(value) else None
    if value_type in _JSON_PASSTHROUGH_TYPES:
        return value
    if value_type is dict:
        return {str(key): json_safe(item) for key, item in value.items()}
    if value_type is list or value_type is tuple:
        return [json_safe(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, Integral) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def json_safe_mapping(value: dict[Any, Any]) -> dict[str, Any]:
    """Normalize a mapping without recursively dispatching its atomic values."""

    payload: dict[str, Any] = {}
    for key, item in value.items():
        output_key = key if type(key) is str else str(key)
        item_type = type(item)
        if item_type is float:
            payload[output_key] = item if math.isfinite(item) else None
        elif item_type in _JSON_PASSTHROUGH_TYPES:
            payload[output_key] = item
        else:
            payload[output_key] = json_safe(item)
    return payload


def json_safe_dataclass(
    value: Any,
    *,
    overrides: dict[str, Any] | None = None,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Serialize known dataclass fields without re-walking primitive scalars."""

    replacements = overrides or {}
    payload: dict[str, Any] = {}
    for name in _field_names(type(value)):
        if name in exclude:
            continue
        if name in replacements:
            payload[name] = replacements[name]
            continue
        field_value = getattr(value, name)
        value_type = type(field_value)
        if (
            field_value is None
            or value_type is str
            or value_type is bool
            or value_type is int
        ):
            payload[name] = field_value
        elif value_type is float:
            payload[name] = field_value if math.isfinite(field_value) else None
        else:
            payload[name] = json_safe(field_value)
    return payload
