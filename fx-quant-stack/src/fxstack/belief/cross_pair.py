from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip01(value: Any) -> float:
    return max(0.0, min(1.0, _safe_float(value, 0.0)))


def _row_value(row: pd.Series | dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        if isinstance(row, pd.Series) and key in row.index:
            value = row.get(key)
        elif isinstance(row, dict):
            value = row.get(key)
        else:
            value = None
        if value not in (None, ""):
            return value
    return default


def _side_sign(value: Any) -> float:
    side = str(value or "").strip().lower()
    if side == "short":
        return -1.0
    if side == "long":
        return 1.0
    return 0.0


def _local_belief_score(row: pd.Series | dict[str, Any]) -> float:
    return _clip01(
        (
            0.28 * _safe_float(_row_value(row, "belief_primary_score", "primary_score"), 0.0)
            + 0.18 * _safe_float(_row_value(row, "belief_primary_rank_score", "primary_rank_score"), 0.0)
            + 0.14 * _safe_float(_row_value(row, "belief_primary_ev_above_hurdle_prob", "primary_ev_above_hurdle_prob"), 0.0)
            + 0.12 * _safe_float(_row_value(row, "belief_gap", "gap"), 0.0)
            + 0.10 * _safe_float(_row_value(row, "belief_horizon_alignment_score", "horizon_alignment_score"), 0.0)
            + 0.08 * _safe_float(_row_value(row, "belief_regime_fit_score", "regime_fit_score"), 0.0)
            + 0.10 * (1.0 - _clip01(_row_value(row, "belief_fragility_score", "fragility_score", default=0.0)))
        )
    )


def _basket_alignment_score(row: pd.Series | dict[str, Any]) -> float:
    raw = _safe_float(_row_value(row, "usd_strength_basket_ret_1", "cross_pair_bias", default=0.0), 0.0)
    side = _side_sign(_row_value(row, "belief_primary_side", "primary_side", "side", default=""))
    if side == 0.0:
        return _clip01(0.5 + (0.5 * math.tanh(raw * 5000.0)))
    return _clip01(0.5 + (0.5 * math.tanh(raw * 5000.0 * side)))


def _consensus_score(row: pd.Series | dict[str, Any]) -> float:
    dispersion = _safe_float(_row_value(row, "cross_pair_dispersion", "pair_dispersion", default=0.0), 0.0)
    return _clip01(1.0 - _clip01(dispersion))


@dataclass(slots=True)
class CrossPairInfluenceRecord:
    pair: str
    ts: str
    rank_position: int = 0
    influence_score: float = 0.0
    recommendation_strength: float = 0.0
    influenced_by_pairs: list[str] = field(default_factory=list)
    cross_pair_reason_codes: list[str] = field(default_factory=list)
    local_belief_score: float = 0.0
    basket_alignment_score: float = 0.0
    peer_confluence_score: float = 0.0
    consensus_score: float = 0.0
    primary_side: str = ""
    primary_scenario: str = ""
    belief_gap: float = 0.0
    model_version: str = ""
    source_mode: str = "disabled"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_cross_pair_influence_records(
    frame: pd.DataFrame | list[dict[str, Any]],
    *,
    pair_col: str = "pair",
    ts_col: str = "ts",
) -> list[CrossPairInfluenceRecord]:
    if isinstance(frame, list):
        frame = pd.DataFrame(frame)
    if frame is None or frame.empty:
        return []
    work = frame.copy().reset_index(drop=True)
    if pair_col not in work.columns:
        raise ValueError(f"cross-pair frame missing required column: {pair_col}")
    if ts_col not in work.columns:
        work[ts_col] = ""

    output: list[CrossPairInfluenceRecord] = []
    for ts_value, group in work.groupby(work[ts_col].astype(str), dropna=False, sort=True):
        group = group.reset_index(drop=True)
        local_scores = group.apply(_local_belief_score, axis=1)
        basket_scores = group.apply(_basket_alignment_score, axis=1)
        consensus_scores = group.apply(_consensus_score, axis=1)
        side_signs = group.apply(lambda row: _side_sign(_row_value(row, "belief_primary_side", "primary_side", "side", default="")), axis=1)

        peer_confluence_scores: list[float] = []
        influenced_by_pairs: list[list[str]] = []
        for idx, row in group.iterrows():
            peer_candidates: list[tuple[str, float]] = []
            direction = float(side_signs.iloc[idx])
            for peer_idx, peer in group.iterrows():
                if peer_idx == idx:
                    continue
                peer_pair = str(_row_value(peer, pair_col, default="")).upper()
                if not peer_pair:
                    continue
                peer_direction = float(side_signs.iloc[peer_idx])
                direction_alignment = 1.0 if direction == 0.0 or peer_direction == 0.0 or direction == peer_direction else 0.55
                peer_alignment = _clip01(
                    0.50 + (0.50 * math.tanh(_safe_float(local_scores.iloc[peer_idx], 0.0) + _safe_float(basket_scores.iloc[peer_idx], 0.0) - 0.5))
                )
                peer_candidate = _clip01(
                    (0.60 * _safe_float(local_scores.iloc[peer_idx], 0.0) + 0.25 * _safe_float(basket_scores.iloc[peer_idx], 0.0) + 0.15 * _safe_float(consensus_scores.iloc[peer_idx], 0.0))
                    * direction_alignment
                    * peer_alignment
                )
                peer_candidates.append((peer_pair, peer_candidate))

            peer_candidates.sort(key=lambda item: (-item[1], item[0]))
            top_peer_pairs = [pair for pair, score in peer_candidates[:3] if score > 0.0]
            peer_confluence = float(sum(score for _, score in peer_candidates[:3]) / max(1, min(3, len(peer_candidates))))
            peer_confluence_scores.append(_clip01(peer_confluence))
            influenced_by_pairs.append(top_peer_pairs)

        influence_scores: list[float] = []
        reason_codes: list[list[str]] = []
        for idx, row in group.iterrows():
            local = _safe_float(local_scores.iloc[idx], 0.0)
            basket = _safe_float(basket_scores.iloc[idx], 0.0)
            consensus = _safe_float(consensus_scores.iloc[idx], 0.0)
            peer = _safe_float(peer_confluence_scores[idx], 0.0)
            influence = _clip01((0.46 * local) + (0.22 * basket) + (0.18 * consensus) + (0.14 * peer))
            recommendation = _clip01((0.58 * influence) + (0.22 * local) + (0.10 * basket) + (0.10 * peer))
            influence_scores.append(influence)
            codes: list[str] = []
            if local >= 0.55:
                codes.append("local_edge")
            if basket >= 0.55:
                codes.append("basket_alignment")
            if consensus >= 0.55:
                codes.append("low_dispersion")
            if peer >= 0.45:
                codes.append("peer_confluence")
            if abs(_safe_float(_row_value(row, "usd_strength_basket_ret_1", default=0.0), 0.0)) > 0.0:
                codes.append("cross_pair_pressure")
            if not codes:
                codes.append("weak_cross_pair_signal")
            reason_codes.append(list(dict.fromkeys(codes)))

        order = sorted(
            range(len(group)),
            key=lambda idx: (-float(influence_scores[idx]), str(_row_value(group.iloc[idx], pair_col, default="")).upper()),
        )
        rank_positions = {idx: rank + 1 for rank, idx in enumerate(order)}

        for idx, row in group.iterrows():
            output.append(
                CrossPairInfluenceRecord(
                    pair=str(_row_value(row, pair_col, default="")).upper(),
                    ts=str(_row_value(row, ts_col, default=ts_value)),
                    rank_position=int(rank_positions[idx]),
                    influence_score=float(influence_scores[idx]),
                    recommendation_strength=float(recommendation),
                    influenced_by_pairs=list(influenced_by_pairs[idx]),
                    cross_pair_reason_codes=list(reason_codes[idx]),
                    local_belief_score=float(local_scores.iloc[idx]),
                    basket_alignment_score=float(basket_scores.iloc[idx]),
                    peer_confluence_score=float(peer_confluence_scores[idx]),
                    consensus_score=float(consensus_scores.iloc[idx]),
                    primary_side=str(_row_value(row, "belief_primary_side", "primary_side", "side", default="")),
                    primary_scenario=str(_row_value(row, "belief_primary_scenario", "primary_scenario", "scenario", default="")),
                    belief_gap=_safe_float(_row_value(row, "belief_gap", default=0.0), 0.0),
                    model_version=str(_row_value(row, "belief_model_version", "model_version", default="")),
                    source_mode=str(_row_value(row, "belief_source_mode", "source_mode", default="disabled")),
                )
            )
    return sorted(output, key=lambda rec: (str(rec.ts), rec.rank_position, rec.pair))


def build_cross_pair_influence_frame(
    frame: pd.DataFrame | list[dict[str, Any]],
    *,
    pair_col: str = "pair",
    ts_col: str = "ts",
) -> pd.DataFrame:
    records = build_cross_pair_influence_records(frame, pair_col=pair_col, ts_col=ts_col)
    if not records:
        return pd.DataFrame(
            columns=
            [
                "pair",
                "ts",
                "rank_position",
                "influence_score",
                "recommendation_strength",
                "influenced_by_pairs",
                "cross_pair_reason_codes",
                "local_belief_score",
                "basket_alignment_score",
                "peer_confluence_score",
                "consensus_score",
                "primary_side",
                "primary_scenario",
                "belief_gap",
                "model_version",
                "source_mode",
            ]
        )
    return pd.DataFrame([record.to_dict() for record in records])


def summarize_cross_pair_intelligence(
    frame: pd.DataFrame | list[dict[str, Any]],
    *,
    pair_col: str = "pair",
    ts_col: str = "ts",
) -> dict[str, Any]:
    ranking = build_cross_pair_influence_frame(frame, pair_col=pair_col, ts_col=ts_col)
    if ranking.empty:
        return {
            "model": "cross_pair_intelligence_v1",
            "rows": 0,
            "pairs": [],
            "rankings": [],
        }
    top_pairs = [str(item) for item in ranking.sort_values(["rank_position", "pair"]).head(5)["pair"].tolist()]
    return {
        "model": "cross_pair_intelligence_v1",
        "rows": int(len(ranking)),
        "pairs": top_pairs,
        "rankings": ranking.sort_values(["ts", "rank_position", "pair"]).to_dict(orient="records"),
    }
