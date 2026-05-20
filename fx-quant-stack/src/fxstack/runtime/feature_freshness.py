"""Feature-bar freshness helpers extracted from ``fxstack.runtime.runner``.

Continues the pattern of carving self-contained chunks out of the 9k-line
runner module. These four functions answer two questions:

* How old is a feature bar relative to the current loop timestamp?
* Has the latest persisted partition for a (provider, pair, timeframe)
  fallen behind the loop?

All four are pure functions of their inputs (plus, for ``latest_partition_ts``,
a ``ParquetStore`` for the read). No global state, no clock reads — the caller
passes ``loop_ts`` explicitly so freshness logic is deterministic.

The bar-to-DataFrame conversion (``_bars_to_raw_frame`` in runner.py)
deliberately stays in runner.py for now because it depends on the
runner-local ``_safe_float`` helper. Pulling utilities first will make
that move straightforward.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from fxstack.io.parquet_store import ParquetStore


def timeframe_to_seconds(timeframe: str) -> int:
    """Convert an MT4-style timeframe code (``M5``, ``H1``, ``D``, ``W``) to seconds.

    Returns 0 for empty input or anything unparseable. The unit letter
    (S/M/H/D) is multiplied by the leading integer (``M5`` → 5×60 = 300).
    ``D``, ``W``, and ``MN``/``MN1`` are handled as fixed-length specials.
    """
    txt = str(timeframe or "").strip().upper()
    if not txt:
        return 0
    if txt == "D":
        return 86_400
    if txt == "W":
        return 604_800
    if txt in {"MN", "MN1"}:
        return 2_592_000
    unit = txt[:1]
    magnitude = txt[1:] or "1"
    try:
        value = int(magnitude)
    except Exception:
        return 0
    scale = {
        "S": 1,
        "M": 60,
        "H": 3_600,
        "D": 86_400,
    }.get(unit, 0)
    return int(value * scale) if scale > 0 else 0


def feature_bar_freshness(*, ts_value: Any, loop_ts: float, timeframe: str) -> dict[str, Any]:
    """Decide whether a feature bar at ``ts_value`` is fresh for ``loop_ts``.

    Returns a dict shaped for the runtime's state telemetry — ``age_secs``,
    ``stale`` (bool), ``stale_after_secs``, ``reason``. Bars older than 2×
    the timeframe (with a 10-minute floor for short timeframes) are
    considered stale; that floor lives here rather than in ``settings``
    because it's the wire-protocol-side definition the runtime publishes
    to dashboards.
    """
    parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    timeframe_secs = max(0, timeframe_to_seconds(timeframe))
    stale_after_secs = max(float(timeframe_secs * 2), 600.0)
    if pd.isna(parsed):
        return {
            "ts": str(ts_value or ""),
            "age_secs": None,
            "stale": True,
            "stale_after_secs": stale_after_secs,
            "reason": "missing_feature_ts",
        }
    age_secs = max(0.0, float(loop_ts) - float(parsed.timestamp()))
    return {
        "ts": str(parsed),
        "age_secs": float(age_secs),
        "stale": bool(age_secs > stale_after_secs),
        "stale_after_secs": float(stale_after_secs),
        "reason": "ok" if age_secs <= stale_after_secs else "stale_feature_bar",
    }


def feature_row_is_stale(*, row: pd.DataFrame, loop_ts: float, timeframe: str) -> bool:
    """Return True when a feature row's first-record ``ts`` is past the freshness floor."""
    if row is None or row.empty:
        return True
    return bool(
        feature_bar_freshness(
            ts_value=row.iloc[0].get("ts"),
            loop_ts=float(loop_ts),
            timeframe=str(timeframe),
        ).get("stale", False)
    )


def latest_partition_ts(
    *,
    store: ParquetStore,
    provider: str,
    pair: str,
    timeframe: str,
) -> pd.Timestamp | None:
    """Read the latest persisted bar's timestamp from the ParquetStore.

    Returns ``None`` if there are no rows or if the ``ts`` column does not
    parse — callers treat that as "no recent partition" and fall back to
    bootstrap data.
    """
    row = store.read_latest_row(
        provider=str(provider),
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        tail_files=3,
    )
    if row.empty:
        return None
    ts = pd.to_datetime(row.iloc[-1].get("ts"), utc=True, errors="coerce")
    if pd.isna(ts):
        return None
    return pd.Timestamp(ts)


__all__ = [
    "feature_bar_freshness",
    "feature_row_is_stale",
    "latest_partition_ts",
    "timeframe_to_seconds",
]
