from __future__ import annotations

try:
    from feast import Entity  # type: ignore
    from feast.value_type import ValueType  # type: ignore
except Exception:  # pragma: no cover - exercised only when Feast is installed
    Entity = None  # type: ignore
    ValueType = None  # type: ignore


pair = (
    Entity(
        name="pair",
        join_keys=["pair"],
        value_type=ValueType.STRING,
        description="FX pair entity key.",
    )
    if Entity is not None and ValueType is not None
    else None
)
