from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

_MIN_GATING_UNIVERSE_SIZE = 3
_TELEMETRY_ONLY_RECOMMENDATION_FLOOR = 0.35
_MIN_SIGNAL_COVERAGE = 0.42
_INELIGIBLE_SOURCE_MODES = {"", "disabled", "artifact_missing"}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _clip01(value: Any) -> float:
    return max(0.0, min(1.0, _safe_float(value, 0.0)))


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return float(sum(float(value) for value in values) / len(values))


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


def _row_has_value(row: pd.Series | dict[str, Any], key: str) -> bool:
    value: Any = None
    if isinstance(row, pd.Series):
        if key not in row.index:
            return False
        value = row.get(key)
    elif isinstance(row, dict):
        value = row.get(key)
    else:
        return False
    if value in (None, ""):
        return False
    try:
        return not bool(pd.isna(value))
    except Exception:
        return True


def _row_float_presence(row: pd.Series | dict[str, Any], *keys: str) -> tuple[float, bool]:
    for key in keys:
        if _row_has_value(row, key):
            return _safe_float(_row_value(row, key, default=0.0), 0.0), True
    return 0.0, False


def _belief_source_mode(row: pd.Series | dict[str, Any]) -> tuple[str, bool]:
    for key in ("belief_source_mode", "source_mode"):
        if _row_has_value(row, key):
            return str(_row_value(row, key, default="")).strip().lower(), True
    return "", False


def _cross_pair_input_eligible(row: pd.Series | dict[str, Any]) -> bool:
    source_mode, present = _belief_source_mode(row)
    return (not present) or source_mode not in _INELIGIBLE_SOURCE_MODES


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
    raw, present = _row_float_presence(row, "usd_strength_basket_ret_1", "cross_pair_bias")
    if not present:
        return 0.5
    side = _side_sign(_row_value(row, "belief_primary_side", "primary_side", "side", default=""))
    if side == 0.0:
        return _clip01(0.5 + (0.5 * math.tanh(raw * 5000.0)))
    return _clip01(0.5 + (0.5 * math.tanh(raw * 5000.0 * side)))


def _consensus_score(row: pd.Series | dict[str, Any]) -> float:
    dispersion, present = _row_float_presence(row, "cross_pair_dispersion", "pair_dispersion")
    if not present:
        return 0.5
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
        eligible_mask = group.apply(_cross_pair_input_eligible, axis=1).astype(bool)
        source_mode_pairs = [_belief_source_mode(group.iloc[idx]) for idx in range(len(group))]
        side_signs = group.apply(lambda row: _side_sign(_row_value(row, "belief_primary_side", "primary_side", "side", default="")), axis=1)

        peer_confluence_scores: list[float] = []
        influenced_by_pairs: list[list[str]] = []
        for idx, row in group.iterrows():
            if not bool(eligible_mask.iloc[idx]):
                peer_confluence_scores.append(0.0)
                influenced_by_pairs.append([])
                continue
            peer_candidates: list[tuple[str, float]] = []
            direction = float(side_signs.iloc[idx])
            for peer_idx, peer in group.iterrows():
                if peer_idx == idx or not bool(eligible_mask.iloc[peer_idx]):
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

        eligible_local = [_safe_float(local_scores.iloc[idx], 0.0) for idx in range(len(group)) if bool(eligible_mask.iloc[idx])]
        eligible_basket = [_safe_float(basket_scores.iloc[idx], 0.0) for idx in range(len(group)) if bool(eligible_mask.iloc[idx])]
        eligible_consensus = [_safe_float(consensus_scores.iloc[idx], 0.0) for idx in range(len(group)) if bool(eligible_mask.iloc[idx])]
        eligible_peer = [_safe_float(peer_confluence_scores[idx], 0.0) for idx in range(len(group)) if bool(eligible_mask.iloc[idx])]
        avg_local = _mean(eligible_local)
        avg_basket = _mean(eligible_basket)
        avg_consensus = _mean(eligible_consensus)
        avg_peer = _mean(eligible_peer)
        signal_coverage = (0.38 * avg_local) + (0.24 * avg_basket) + (0.20 * avg_consensus) + (0.18 * avg_peer)
        eligible_count = int(eligible_mask.sum())
        telemetry_only = eligible_count < _MIN_GATING_UNIVERSE_SIZE or float(signal_coverage) < _MIN_SIGNAL_COVERAGE

        influence_scores: list[float] = []
        recommendation_scores: list[float] = []
        reason_codes: list[list[str]] = []
        for idx, row in group.iterrows():
            if not bool(eligible_mask.iloc[idx]):
                influence_scores.append(0.0)
                recommendation_scores.append(0.5)
                reason_codes.append(["ineligible_belief_source_mode"])
                continue
            local = _safe_float(local_scores.iloc[idx], 0.0)
            basket = _safe_float(basket_scores.iloc[idx], 0.0)
            consensus = _safe_float(consensus_scores.iloc[idx], 0.0)
            peer = _safe_float(peer_confluence_scores[idx], 0.0)
            influence = _clip01((0.46 * local) + (0.22 * basket) + (0.18 * consensus) + (0.14 * peer))
            recommendation = _clip01((0.58 * influence) + (0.22 * local) + (0.10 * basket) + (0.10 * peer))
            if telemetry_only:
                recommendation = max(float(recommendation), float(_TELEMETRY_ONLY_RECOMMENDATION_FLOOR))
            influence_scores.append(influence)
            recommendation_scores.append(float(recommendation))
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
            if telemetry_only:
                codes.append("telemetry_only")
                if eligible_count < _MIN_GATING_UNIVERSE_SIZE:
                    codes.append("insufficient_universe_coverage")
                else:
                    codes.append("low_signal_quality")
            if not codes:
                codes.append("weak_cross_pair_signal")
            reason_codes.append(list(dict.fromkeys(codes)))

        order = sorted(
            range(len(group)),
            key=lambda idx: (
                not bool(eligible_mask.iloc[idx]),
                -float(influence_scores[idx]),
                str(_row_value(group.iloc[idx], pair_col, default="")).upper(),
            ),
        )
        rank_positions = {idx: rank + 1 for rank, idx in enumerate(order)}

        for idx, row in group.iterrows():
            explicit_source_mode, explicit_source_present = source_mode_pairs[idx]
            output.append(
                CrossPairInfluenceRecord(
                    pair=str(_row_value(row, pair_col, default="")).upper(),
                    ts=str(_row_value(row, ts_col, default=ts_value)),
                    rank_position=int(rank_positions[idx]),
                    influence_score=float(influence_scores[idx]),
                    recommendation_strength=float(recommendation_scores[idx]),
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
                    source_mode=(
                        "telemetry_only"
                        if bool(eligible_mask.iloc[idx]) and telemetry_only
                        else (
                            explicit_source_mode
                            if explicit_source_present
                            else ("artifact" if bool(eligible_mask.iloc[idx]) else "disabled")
                        )
                    ),
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
    eligible_ranking = ranking.loc[~ranking["cross_pair_reason_codes"].apply(lambda codes: "ineligible_belief_source_mode" in list(codes or []))].copy()
    top_source = eligible_ranking if not eligible_ranking.empty else ranking
    top_pairs = [str(item) for item in top_source.sort_values(["rank_position", "pair"]).head(5)["pair"].tolist()]
    return {
        "model": "cross_pair_intelligence_v1",
        "rows": int(len(ranking)),
        "pairs": top_pairs,
        "rankings": ranking.sort_values(["ts", "rank_position", "pair"]).to_dict(orient="records"),
    }
