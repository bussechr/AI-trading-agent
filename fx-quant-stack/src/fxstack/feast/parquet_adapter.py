from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.feast.repository import default_feature_views
from fxstack.io.parquet_store import ParquetStore

_KEY_COLUMNS = ["pair", "timeframe", "ts", "date", "close_ts", "anchor_close_ts"]
_CROSS_PAIR_COLUMNS = {"cross_pair_dispersion"}
_DIAGNOSTIC_COLUMNS = {"spread_bps", "spread", "transport_mode", "ticks_fresh"}


@dataclass(slots=True)
class FeastParquetArtifact:
    pair: str
    view_name: str
    timeframe: str
    rows: int
    output_path: Path


def _base_frame_columns(frame: pd.DataFrame) -> list[str]:
    return [col for col in _KEY_COLUMNS if col in frame.columns]


def _anchor_columns(frame: pd.DataFrame) -> list[str]:
    blocked_prefixes = ("m15_", "h1_", "h4_", "d_", "usd_strength_", "cross_pair_", "feature_serving_", "runtime_", "tick_", "heartbeat_")
    out: list[str] = []
    for col in frame.columns:
        txt = str(col)
        if txt in _base_frame_columns(frame):
            continue
        if txt in _CROSS_PAIR_COLUMNS or txt in _DIAGNOSTIC_COLUMNS:
            continue
        if txt.startswith(blocked_prefixes):
            continue
        out.append(txt)
    return out


def _columns_for_view(frame: pd.DataFrame, view_name: str) -> list[str]:
    base = _base_frame_columns(frame)
    if view_name.startswith("anchor_"):
        return base + _anchor_columns(frame)
    if view_name == "cross_pair_context":
        cols = [col for col in frame.columns if str(col).startswith(("usd_strength_", "cross_pair_")) or str(col) in _CROSS_PAIR_COLUMNS]
        return base + cols
    if view_name == "live_diagnostics":
        cols = [
            col
            for col in frame.columns
            if str(col).startswith(("feature_serving_", "runtime_", "tick_", "heartbeat_")) or str(col) in _DIAGNOSTIC_COLUMNS
        ]
        return base + cols
    prefix = ""
    if view_name == "context_m15":
        prefix = "m15_"
    elif view_name == "context_h1":
        prefix = "h1_"
    elif view_name == "context_h4":
        prefix = "h4_"
    elif view_name == "context_d":
        prefix = "d_"
    cols = [col for col in frame.columns if str(col).startswith(prefix)]
    return base + cols


def split_feature_views(frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    if frame.empty:
        return {spec.name: frame.copy() for spec in default_feature_views()}
    out: dict[str, pd.DataFrame] = {}
    for spec in default_feature_views():
        cols = [col for col in _columns_for_view(frame, spec.name) if col in frame.columns]
        out[spec.name] = frame[cols].copy()
    return out


def compact_pair_feature_views(
    *,
    feature_root: str | Path,
    output_root: str | Path,
    provider: str,
    pair: str,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    source = ParquetStore(Path(feature_root))
    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    compacted: dict[str, Any] = {}
    for timeframe in [str(tf).upper() for tf in list(timeframes or ["M5", "H4", "D"])]:
        frame = source.read_pair_timeframe(provider=provider, pair=str(pair).upper(), timeframe=timeframe)
        if frame.empty:
            compacted[timeframe] = {"rows": 0, "views": {}}
            continue
        views = split_feature_views(frame)
        view_diag: dict[str, Any] = {}
        for view_name, view_frame in views.items():
            if view_frame.empty:
                view_diag[view_name] = {"rows": 0, "path": ""}
                continue
            store = ParquetStore(output / view_name)
            view_path = store.write_partitioned(view_frame, provider=provider, pair=str(pair).upper(), timeframe=timeframe)
            view_diag[view_name] = {"rows": int(len(view_frame)), "path": str(view_path)}
        compacted[timeframe] = {"rows": int(len(frame)), "views": view_diag}
    return {"pair": str(pair).upper(), "provider": str(provider), "timeframes": compacted}


def _minimal_view_frame(frame: pd.DataFrame, *, pair: str, timeframe: str) -> pd.DataFrame:
    if not frame.empty:
        return frame.copy()
    return pd.DataFrame(
        {
            "pair": [str(pair).upper()],
            "timeframe": [str(timeframe).upper()],
            "ts": [pd.Timestamp("1970-01-01T00:00:00Z")],
            "date": ["1970-01-01"],
        }
    )


def _aggregate_pair_views(
    *,
    source: ParquetStore,
    provider: str,
    pair: str,
) -> dict[str, pd.DataFrame]:
    pair_value = str(pair).upper()
    m5 = source.read_pair_timeframe(provider=provider, pair=pair_value, timeframe="M5")
    h4 = source.read_pair_timeframe(provider=provider, pair=pair_value, timeframe="H4")
    daily = source.read_pair_timeframe(provider=provider, pair=pair_value, timeframe="D")

    anchor = split_feature_views(m5).get("anchor_m5", pd.DataFrame()) if not m5.empty else pd.DataFrame()
    anchor = _minimal_view_frame(anchor, pair=pair_value, timeframe="M5")

    higher_frames: list[pd.DataFrame] = []
    for tf, frame in (("H4", h4), ("D", daily)):
        if frame.empty:
            continue
        item = frame.copy()
        item["source_timeframe"] = tf
        higher_frames.append(item)
    higher = pd.concat(higher_frames, ignore_index=True) if higher_frames else pd.DataFrame()
    higher = _minimal_view_frame(higher, pair=pair_value, timeframe="MULTI")

    cross = split_feature_views(m5).get("cross_pair_context", pd.DataFrame()) if not m5.empty else pd.DataFrame()
    cross = _minimal_view_frame(cross, pair=pair_value, timeframe="M5")

    live = split_feature_views(m5).get("live_diagnostics", pd.DataFrame()) if not m5.empty else pd.DataFrame()
    live = _minimal_view_frame(live, pair=pair_value, timeframe="M5")

    return {
        "anchor_lifecycle_m5": anchor,
        "higher_timeframe_context": higher,
        "cross_pair_context": cross,
        "live_diagnostics": live,
    }


def build_stable_feast_parquet_outputs(
    *,
    source_root: Path | str,
    output_root: Path | str,
    provider: str,
    pairs: list[str],
) -> list[FeastParquetArtifact]:
    source = ParquetStore(Path(source_root))
    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    artifacts: list[FeastParquetArtifact] = []
    for pair in [str(item).upper() for item in list(pairs or [])]:
        for view_name, frame in _aggregate_pair_views(source=source, provider=provider, pair=pair).items():
            timeframe = "M5" if view_name != "higher_timeframe_context" else "MULTI"
            target = ParquetStore(output / view_name)
            out_path = target.write_partitioned(
                frame,
                provider=provider,
                pair=pair,
                timeframe=timeframe,
            )
            artifacts.append(
                FeastParquetArtifact(
                    pair=pair,
                    view_name=view_name,
                    timeframe=timeframe,
                    rows=int(len(frame)),
                    output_path=Path(out_path),
                )
            )
    return artifacts
