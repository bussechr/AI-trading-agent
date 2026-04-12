"""# AGENT: ROLE: Build the cross-pair directional-belief v2 training dataset.
# AGENT: ENTRYPOINT: `build_directional_belief_dataset()`.
# AGENT: PRIMARY INPUTS: feature parquet root, pair universe, timeframe.
# AGENT: PRIMARY OUTPUTS: hypothesis candidate frame with realized outcome labels.
# AGENT: DEPENDS ON: parquet store, candidate builder, outcome labels.
# AGENT: CALLED BY: `fxstack/training/belief.py` and CLI dataset export.
# AGENT: STATE / SIDE EFFECTS: optional dataset export path only.
# AGENT: HANDSHAKES: shared query grouping contract for the global XGBRanker.
# AGENT: SEE: `fxstack/belief/candidate_builder.py` -> `fxstack/belief/outcome_labels.py` -> `src/trader/cli.py`"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.belief.candidate_builder import CROSS_PAIR_CONTEXT_PREFIXES, build_hypothesis_candidates
from fxstack.belief.outcome_labels import label_hypothesis_outcomes
from fxstack.feast.offline_builder import build_historical_feature_frame
from fxstack.settings import get_settings

REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS = ("usd_strength_basket_ret_1", "cross_pair_dispersion")


def _provider() -> str:
    return get_settings().normalized_data_provider


def _downsample_queries(frame: pd.DataFrame, *, max_queries_per_pair: int) -> pd.DataFrame:
    if frame.empty or max_queries_per_pair <= 0:
        return frame
    unique_queries = frame[["pair", "query_id"]].drop_duplicates().reset_index(drop=True)
    keep_frames: list[pd.DataFrame] = []
    for pair, pair_queries in unique_queries.groupby("pair"):
        if len(pair_queries) <= max_queries_per_pair:
            keep_ids = set(pair_queries["query_id"].astype(str))
        else:
            step = max(1, int(len(pair_queries) / max_queries_per_pair))
            keep_ids = set(pair_queries.iloc[::step].head(max_queries_per_pair)["query_id"].astype(str))
        keep_frames.append(frame.loc[(frame["pair"].astype(str) == str(pair)) & (frame["query_id"].astype(str).isin(keep_ids))])
    if not keep_frames:
        return frame.iloc[0:0].copy()
    return pd.concat(keep_frames, axis=0, ignore_index=True)


def _cross_pair_context_status(frame: pd.DataFrame, retrieval_meta: dict[str, Any]) -> dict[str, Any]:
    available_columns = [name for name in REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS if name in frame.columns]
    missing_columns = [name for name in REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS if name not in frame.columns]
    prefixed_columns = [name for name in frame.columns if any(str(name).startswith(prefix) for prefix in CROSS_PAIR_CONTEXT_PREFIXES)]
    return {
        "available": not missing_columns,
        "columns": available_columns,
        "missing_columns": missing_columns,
        "prefixed_columns": sorted(str(name) for name in prefixed_columns),
        "retrieval_source": str(retrieval_meta.get("source") or retrieval_meta.get("feature_retrieval") or ""),
    }


def build_directional_belief_dataset(
    *,
    feature_root: str,
    timeframe: str = "M5",
    pairs: list[str] | None = None,
    out_path: str | None = None,
    max_queries_per_pair: int = 20000,
    slippage_bps: float = 0.25,
    min_expected_edge_bps: float | None = None,
) -> pd.DataFrame:
    s = get_settings()
    pair_list = [str(p).upper() for p in (pairs or list(s.pairs))]
    frames: list[pd.DataFrame] = []
    retrievals: list[dict[str, Any]] = []
    for pair in pair_list:
        feats, retrieval_meta = build_historical_feature_frame(
            feature_root=feature_root,
            pair=pair,
            timeframe=str(timeframe).upper(),
            feature_service_name=f"fx_{pair.lower()}_directional_belief_{str(timeframe).lower()}",
            feature_view_names=["anchor_m5", "context_m15", "context_h1", "context_h4", "context_d", "cross_pair_context"],
        )
        retrieval_meta = dict(retrieval_meta or {})
        retrieval_meta["pair"] = str(pair).upper()
        cross_pair_status = _cross_pair_context_status(feats, retrieval_meta)
        retrieval_meta["cross_pair_context"] = dict(cross_pair_status)
        retrievals.append(retrieval_meta)
        if feats.empty:
            continue
        base = feats.sort_values("ts").reset_index(drop=True).copy()
        base["pair"] = str(pair)
        base["row_idx"] = range(len(base))
        candidates = build_hypothesis_candidates(base, settings=s, local_feasible_only=True)
        if candidates.empty:
            continue
        labeled = label_hypothesis_outcomes(
            candidates,
            base_frame=base,
            slippage_bps=float(slippage_bps),
            min_expected_edge_bps=float(min_expected_edge_bps if min_expected_edge_bps is not None else s.min_expected_edge_bps),
        )
        frames.append(labeled)
    if not frames:
        dataset = pd.DataFrame()
    else:
        dataset = pd.concat(frames, axis=0, ignore_index=True)
        dataset = _downsample_queries(dataset, max_queries_per_pair=max_queries_per_pair)
        dataset = dataset.sort_values(["pair", "ts", "scenario", "side"]).reset_index(drop=True)
        dataset["query_id"] = dataset["pair"].astype(str) + "|" + dataset["ts"].astype(str)
        retrieval_source = "feast_historical"
        if retrievals and any(str(item.get("source") or item.get("feature_retrieval") or "").strip() != "feast_historical" for item in retrievals):
            retrieval_source = "parquet_point_in_time_fallback"
        dataset.attrs["feature_retrieval"] = {
            "source": retrieval_source,
            "timeframe": str(timeframe).upper(),
            "pair_count": int(len(pair_list)),
            "pair_sources": sorted({str(p).upper() for p in pair_list}),
            "feature_service_names": sorted({str(item.get("feature_service_name") or "") for item in retrievals if str(item.get("feature_service_name") or "").strip()}),
            "retrievals": retrievals,
        }
        missing_context_pairs = sorted(
            str(item.get("pair") or "").upper()
            for item in retrievals
            if not bool(dict(item.get("cross_pair_context") or {}).get("available", False))
        )
        dataset.attrs["cross_pair_context"] = {
            "available": not missing_context_pairs,
            "required_columns": list(REQUIRED_CROSS_PAIR_CONTEXT_COLUMNS),
            "missing_pairs": [pair for pair in missing_context_pairs if pair],
            "retrievals": [dict(item.get("cross_pair_context") or {}) for item in retrievals],
        }
    if out_path and not dataset.empty:
        target = Path(out_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if str(target).endswith(".csv.gz"):
            with gzip.open(target, "wt", encoding="utf-8", newline="") as fh:
                dataset.to_csv(fh, index=False)
        else:
            dataset.to_csv(target, index=False)
    return dataset
