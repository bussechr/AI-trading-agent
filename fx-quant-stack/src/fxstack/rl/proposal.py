from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.risk.contracts import PortfolioState
from fxstack.rl.contracts import RLPortfolioAction, RLPortfolioObservation, RLTradeAction
from fxstack.rl.trainer import RLLinearCheckpoint, load_replay_checkpoint


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _sign_for_side(side: str) -> float:
    txt = str(side or "").strip().upper()
    if txt == "SELL":
        return -1.0
    return 1.0


def _proposal_strength(*, score: float, fallback: float) -> float:
    if np.isfinite(score):
        if abs(score) > 1.5:
            score = float(1.0 / (1.0 + np.exp(-score)))
        return _clip01(score)
    return _clip01(fallback)


def _build_market_snapshot(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "spread_bps": _safe_float(meta.get("spread_bps", meta.get("max_spread_bps", 0.0)), 0.0),
        "freshness_secs": _safe_float(meta.get("freshness_secs", 0.0), 0.0),
        "volatility": _safe_float(meta.get("vol_20", meta.get("volatility", 0.0)), 0.0),
        "liquidity_score": _safe_float(meta.get("liquidity_score", 0.0), 0.0),
        "regime": str(meta.get("regime_bucket") or meta.get("regime") or ""),
        "session_bucket": str(meta.get("session_bucket") or ""),
        "market_open": bool(meta.get("market_open", True)),
        "data_fresh": bool(meta.get("data_fresh", True)),
    }


def _build_feature_snapshot(meta: dict[str, Any]) -> dict[str, float]:
    keys = [
        "expected_edge_bps",
        "expected_net_ev_bps",
        "calibrated_ev_bps_shadow",
        "trade_prob",
        "entry_prob",
        "conviction_score",
        "allocator_score",
        "portfolio_risk_pressure",
        "portfolio_pair_pressure",
        "portfolio_session_pressure",
        "portfolio_sleeve_pressure",
        "portfolio_correlation_pressure",
        "replacement_urgency",
        "spread_bps",
        "freshness_secs",
        "liquidity_score",
        "cross_pair_rank_position",
        "cross_pair_influence_score",
        "cross_pair_recommendation_strength",
    ]
    return {key: _safe_float(meta.get(key, 0.0), 0.0) for key in keys}


def _portfolio_state_from_payload(portfolio: dict[str, Any] | PortfolioState | None) -> PortfolioState:
    if isinstance(portfolio, PortfolioState):
        return portfolio
    payload = dict(portfolio or {})
    metadata: dict[str, Any] = {}
    for key in ("concentration", "correlation", "budget", "stress", "governance", "rl_portfolio_proposal"):
        value = payload.get(key)
        if value is not None:
            metadata[key] = value
    if "metadata" in payload and isinstance(payload.get("metadata"), dict):
        metadata.update(dict(payload.get("metadata") or {}))
    # The runtime often passes portfolio telemetry instead of a raw PortfolioState.
    # Preserve the live book signals in the structured portfolio contract so the
    # RL observation still reflects concentration and exposure context.
    return PortfolioState(
        equity=_safe_float(payload.get("equity", 0.0), 0.0),
        balance=_safe_float(payload.get("balance", 0.0), 0.0),
        peak_equity=_safe_float(payload.get("peak_equity", 0.0), 0.0),
        drawdown_pct=_safe_float(payload.get("drawdown_pct", 0.0), 0.0),
        open_position_count=int(payload.get("open_position_count", payload.get("positions", 0)) or 0),
        pair_position_count=int(payload.get("pair_position_count", len(dict(payload.get("per_symbol_exposure") or {}))) or 0),
        max_total_positions=int(payload.get("max_total_positions", 0) or 0),
        max_pair_positions=int(payload.get("max_pair_positions", 0) or 0),
        gross_exposure=_safe_float(payload.get("gross_exposure", 0.0), 0.0),
        net_exposure=_safe_float(payload.get("net_exposure", 0.0), 0.0),
        capital_at_risk_pct=_safe_float(payload.get("capital_at_risk_pct", payload.get("budget", {}).get("risk_budget_pct", 0.0) if isinstance(payload.get("budget"), dict) else 0.0), 0.0),
        sleeve=str(payload.get("sleeve") or payload.get("governance", {}).get("current_sleeve", "") if isinstance(payload.get("governance"), dict) else ""),
        replacement_pressure=_safe_float(payload.get("replacement_pressure", payload.get("stress", {}).get("replacement_pressure", 0.0) if isinstance(payload.get("stress"), dict) else 0.0), 0.0),
        metadata=metadata,
    )


def _checkpoint_summary(checkpoint: RLLinearCheckpoint | None) -> dict[str, Any]:
    if checkpoint is None:
        return {}
    return {
        "schema_version": str(getattr(checkpoint, "schema_version", "") or ""),
        "target_name": str(getattr(checkpoint, "target_name", "") or ""),
        "feature_count": int(len(getattr(checkpoint, "feature_names", []) or [])),
        "train_rows": int(getattr(checkpoint, "train_rows", 0) or 0),
        "val_rows": int(getattr(checkpoint, "val_rows", 0) or 0),
        "metrics": dict(getattr(checkpoint, "metrics", {}) or {}),
        "metadata": dict(getattr(checkpoint, "metadata", {}) or {}),
    }


def _load_policy_manifest(policy_manifest_path: Path | None) -> dict[str, Any]:
    if policy_manifest_path is None:
        return {}
    path = Path(policy_manifest_path)
    if not path.exists():
        return {}
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")) or {})
    except Exception:
        return {}


def _resolve_checkpoint_path_from_manifest(policy_manifest: dict[str, Any]) -> Path | None:
    raw_path = str(policy_manifest.get("checkpoint_path") or policy_manifest.get("artifact_paths", {}).get("checkpoint_path") or "").strip()
    if not raw_path:
        return None
    return Path(raw_path)


@dataclass(slots=True)
class RLPortfolioProposal:
    pair: str
    action: RLTradeAction
    source: str
    score: float = 0.0
    confidence: float = 0.0
    supervised_fallback_used: bool = True
    fallback_reason: str = ""
    risk_review: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["action"] = self.action.to_dict()
        return payload


@dataclass(slots=True)
class RLPortfolioProposalBundle:
    ts: str
    pair_universe: list[str]
    observation: dict[str, Any]
    proposals_by_pair: dict[str, RLPortfolioProposal]
    source: str
    supervised_fallback_used: bool
    fallback_reason: str
    checkpoint_path: str = ""
    checkpoint_loaded: bool = False
    checkpoint_summary: dict[str, Any] = field(default_factory=dict)
    policy_manifest_path: str = ""
    policy_manifest: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": str(self.ts),
            "pair_universe": [str(pair).upper() for pair in self.pair_universe],
            "observation": dict(self.observation or {}),
            "proposals_by_pair": {pair: proposal.to_dict() for pair, proposal in sorted(self.proposals_by_pair.items())},
            "source": str(self.source),
            "supervised_fallback_used": bool(self.supervised_fallback_used),
            "fallback_reason": str(self.fallback_reason),
            "checkpoint_path": str(self.checkpoint_path or ""),
            "checkpoint_loaded": bool(self.checkpoint_loaded),
            "checkpoint_summary": dict(self.checkpoint_summary or {}),
            "policy_manifest_path": str(self.policy_manifest_path or ""),
            "policy_manifest": dict(self.policy_manifest or {}),
            "diagnostics": dict(self.diagnostics or {}),
        }


def _load_checkpoint(checkpoint_path: Path | None) -> RLLinearCheckpoint | None:
    if checkpoint_path is None:
        return None
    return _load_checkpoint_cached(str(Path(checkpoint_path)))


@lru_cache(maxsize=16)
def _load_checkpoint_cached(checkpoint_path: str) -> RLLinearCheckpoint | None:
    path = Path(str(checkpoint_path or ""))
    if not path.exists():
        return None
    try:
        return load_replay_checkpoint(path)
    except Exception:
        return None


def _decision_to_row(
    *,
    decision: dict[str, Any],
    candidate: dict[str, Any] | None = None,
    checkpoint: RLLinearCheckpoint | None = None,
) -> dict[str, Any]:
    meta = dict(decision.get("metadata") or {})
    row = {
        "episode_id": str(decision.get("episode_id") or meta.get("episode_id") or "runtime"),
        "pair": str(decision.get("symbol") or decision.get("pair") or meta.get("pair") or "").upper(),
        "ts": str(decision.get("ts") or meta.get("ts") or ""),
        "side": str(decision.get("side") or meta.get("position_side") or "BUY").upper(),
        "allocator_score": _safe_float(meta.get("allocator_score", 0.0), 0.0),
        "conviction_score": _safe_float(meta.get("conviction_score", 0.0), 0.0),
        "trade_prob": _safe_float(meta.get("trade_prob", decision.get("trade_prob", 0.0)), 0.0),
        "entry_ready": bool(meta.get("entry_ready", False)),
        "strict_entry_ready": bool(meta.get("strict_entry_ready", meta.get("entry_ready", False))),
        "adaptive_shadow_would_trade": bool(meta.get("adaptive_shadow_would_trade", False)),
        "has_open_position": bool(meta.get("has_open_position", False)),
        "position_open": bool(meta.get("adaptive_shadow_live_divergence") == "open_position"),
        "lifecycle_action": str(meta.get("lifecycle_action") or ""),
        "lifecycle_reason": str(meta.get("lifecycle_reason") or ""),
        "portfolio_risk_pressure": _safe_float(meta.get("portfolio_risk_pressure", 0.0), 0.0),
        "portfolio_pair_pressure": _safe_float(meta.get("portfolio_pair_pressure", 0.0), 0.0),
        "portfolio_session_pressure": _safe_float(meta.get("portfolio_session_pressure", 0.0), 0.0),
        "portfolio_sleeve_pressure": _safe_float(meta.get("portfolio_sleeve_pressure", 0.0), 0.0),
        "portfolio_correlation_pressure": _safe_float(meta.get("portfolio_correlation_pressure", 0.0), 0.0),
        "replacement_urgency": _safe_float(meta.get("replacement_urgency", 0.0), 0.0),
        "spread_bps": _safe_float(meta.get("spread_bps", 0.0), 0.0),
        "freshness_secs": _safe_float(meta.get("freshness_secs", 0.0), 0.0),
        "liquidity_score": _safe_float(meta.get("liquidity_score", 0.0), 0.0),
        "vol_20": _safe_float(meta.get("vol_20", 0.0), 0.0),
        "candidate_selected": bool(meta.get("allocator_selected", False)),
        "candidate_rank": int(meta.get("allocator_rank", 0) or 0),
        "candidate_rejection_reason": str(meta.get("allocator_rejection_reason") or ""),
        "candidate_score": _safe_float(meta.get("allocator_score", 0.0), 0.0),
        "candidate_replacement_target_pair": str(meta.get("replacement_target_pair") or ""),
        "cross_pair_rank_position": int(_safe_float(meta.get("cross_pair_rank_position", 0), 0.0)),
        "cross_pair_influence_score": _safe_float(meta.get("cross_pair_influence_score", 0.5), 0.5),
        "cross_pair_recommendation_strength": _safe_float(meta.get("cross_pair_recommendation_strength", 0.5), 0.5),
        "cross_pair_influenced_by_pairs": list(meta.get("cross_pair_influenced_by_pairs", []) or []),
        "cross_pair_reason_codes": list(meta.get("cross_pair_reason_codes", []) or []),
        "cross_pair_soft_block": bool(meta.get("cross_pair_soft_block", False)),
        "cross_pair_hard_block": bool(meta.get("cross_pair_hard_block", False)),
    }
    row.update(_build_feature_snapshot(meta))
    row.update(_build_market_snapshot(meta))
    if candidate is not None:
        row.update(
            {
                "candidate_selected": bool(candidate.get("allocator_selected", False)),
                "candidate_rank": int(candidate.get("allocator_rank", 0) or 0),
                "candidate_rejection_reason": str(candidate.get("allocator_rejection_reason") or ""),
                "candidate_score": _safe_float(candidate.get("allocator_score", 0.0), 0.0),
                "candidate_replacement_target_pair": str(candidate.get("replacement_target_pair") or ""),
            }
        )
    if checkpoint is not None:
        row["checkpoint_score"] = float(checkpoint.predict_frame(pd.DataFrame([row]))[0])
    return row


def build_portfolio_rl_proposal_bundle(
    *,
    ts: str,
    decisions: list[dict[str, Any]],
    ranked_candidates: list[Any] | None = None,
    portfolio: dict[str, Any] | None = None,
    policy_context: dict[str, Any] | None = None,
    policy_manifest_path: Path | None = None,
    checkpoint_path: Path | None = None,
    supervised_fallback_required: bool = True,
) -> RLPortfolioProposalBundle:
    policy_context = dict(policy_context or {})
    manifest_path = policy_manifest_path
    if manifest_path is None:
        context_manifest = str(policy_context.get("policy_manifest_path") or policy_context.get("artifact_policy_manifest_path") or policy_context.get("artifact_manifest_path") or "").strip()
        if context_manifest:
            manifest_path = Path(context_manifest)
    policy_manifest = _load_policy_manifest(manifest_path)
    decision_map: dict[str, dict[str, Any]] = {}
    for item in list(decisions or []):
        pair = str(item.get("symbol") or item.get("pair") or "").upper()
        if pair:
            decision_map[pair] = dict(item or {})
    candidate_map: dict[str, dict[str, Any]] = {}
    for item in list(ranked_candidates or []):
        payload = dict(item.to_dict() if hasattr(item, "to_dict") else item)
        pair = str(payload.get("pair") or "").upper()
        if pair:
            candidate_map[pair] = payload

    effective_checkpoint_path = checkpoint_path
    if effective_checkpoint_path is None:
        policy_checkpoint_path = str(policy_context.get("checkpoint_path") or policy_context.get("artifact_checkpoint_path") or "").strip()
        if policy_checkpoint_path:
            effective_checkpoint_path = Path(policy_checkpoint_path)
    if effective_checkpoint_path is None and policy_manifest:
        effective_checkpoint_path = _resolve_checkpoint_path_from_manifest(policy_manifest)
    checkpoint = _load_checkpoint(effective_checkpoint_path)
    fallback_reason = ""
    source = "rl_checkpoint" if checkpoint is not None else "supervised_fallback"
    if checkpoint is None:
        fallback_reason = "checkpoint_unavailable"
    elif not getattr(checkpoint, "feature_names", None):
        fallback_reason = "checkpoint_featureless"
        source = "supervised_fallback"
        checkpoint = None

    pair_universe = [pair for pair in sorted(decision_map) if pair]
    market_by_pair: dict[str, dict[str, Any]] = {}
    features_by_pair: dict[str, dict[str, Any]] = {}
    proposals_by_pair: dict[str, RLPortfolioProposal] = {}
    for pair in pair_universe:
        decision = dict(decision_map.get(pair) or {})
        candidate = candidate_map.get(pair)
        row = _decision_to_row(decision=decision, candidate=candidate, checkpoint=checkpoint)
        if checkpoint is not None:
            strength = _proposal_strength(score=float(row.get("checkpoint_score", 0.0)), fallback=float(row.get("candidate_score", row.get("allocator_score", 0.0))))
            proposal_source = "rl_checkpoint"
            fallback_used = False
            proposal_reason = ""
        else:
            strength = _proposal_strength(score=float(row.get("candidate_score", row.get("allocator_score", row.get("conviction_score", 0.0)))), fallback=float(row.get("trade_prob", 0.0)))
            proposal_source = "supervised_fallback"
            fallback_used = True
            proposal_reason = fallback_reason or str(row.get("candidate_rejection_reason") or row.get("lifecycle_reason") or "supervised_allocator")
        market_by_pair[pair] = {k: row[k] for k in ("spread_bps", "freshness_secs", "volatility", "liquidity_score", "regime", "session_bucket", "market_open", "data_fresh")}
        features_by_pair[pair] = {k: float(v) for k, v in row.items() if k not in {"pair", "ts", "side"} and isinstance(v, (int, float, np.integer, np.floating, bool))}
        open_position = bool(row.get("has_open_position") or row.get("position_open"))
        ready_for_entry = bool(row.get("adaptive_shadow_would_trade") or row.get("strict_entry_ready") or row.get("entry_ready"))
        close_position = bool(open_position and str(row.get("lifecycle_action") or "").lower() in {"exit", "partial_tp"})
        tighten_stop = bool(open_position and str(row.get("lifecycle_action") or "").lower() in {"tighten_stop", "modify_sl"})
        entry_supported = bool(
            ready_for_entry
            and not open_position
            and not close_position
            and not tighten_stop
            and float(abs(strength)) >= 0.05
        )
        if open_position and not close_position:
            target_position = 0.0
        elif ready_for_entry:
            target_position = float(_sign_for_side(str(row.get("side") or decision.get("side") or "BUY")) * strength)
        else:
            target_position = 0.0
        action = RLTradeAction(
            target_position=float(target_position),
            close_position=bool(close_position),
            tighten_stop=bool(tighten_stop),
            metadata={
                "proposal_source": proposal_source,
                "proposal_strength": float(strength),
                "supervised_fallback_required": bool(supervised_fallback_required),
                "candidate_rank": int(row.get("candidate_rank", 0) or 0),
                "allocator_selected": bool(row.get("candidate_selected", False)),
                "cross_pair_rank_position": int(row.get("cross_pair_rank_position", 0) or 0),
                "cross_pair_influence_score": float(row.get("cross_pair_influence_score", 0.5)),
                "cross_pair_recommendation_strength": float(row.get("cross_pair_recommendation_strength", 0.5)),
                "cross_pair_soft_block": bool(row.get("cross_pair_soft_block", False)),
                "cross_pair_hard_block": bool(row.get("cross_pair_hard_block", False)),
                "cross_pair_influenced_by_pairs": list(row.get("cross_pair_influenced_by_pairs", []) or []),
                "cross_pair_reason_codes": list(row.get("cross_pair_reason_codes", []) or []),
                "entry_supported": bool(entry_supported),
            },
        )
        proposals_by_pair[pair] = RLPortfolioProposal(
            pair=pair,
            action=action,
            source=str(proposal_source),
            score=float(strength),
            confidence=float(row.get("trade_prob", row.get("conviction_score", 0.0))),
            supervised_fallback_used=bool(fallback_used),
            fallback_reason=str(proposal_reason),
            risk_review={
                "risk_verdict": str((decision.get("risk") or {}).get("verdict") or decision.get("verdict") or decision.get("metadata", {}).get("risk_verdict") or ""),
                "risk_reason": str((decision.get("risk") or {}).get("reason") or decision.get("reason") or decision.get("metadata", {}).get("risk_reason") or ""),
                "entry_ready": bool(decision.get("metadata", {}).get("entry_ready", False)),
                "strict_entry_ready": bool(decision.get("metadata", {}).get("strict_entry_ready", False)),
            },
            metadata={
                "pair": pair,
                "open_position": bool(open_position),
                "ready_for_entry": bool(ready_for_entry),
                "candidate_rejection_reason": str(row.get("candidate_rejection_reason") or ""),
                "candidate_rank": int(row.get("candidate_rank", 0) or 0),
                "candidate_score": float(row.get("candidate_score", row.get("allocator_score", 0.0))),
                "checkpoint_score": float(row.get("checkpoint_score", 0.0)),
                "policy_source": str(proposal_source),
                "cross_pair_rank_position": int(row.get("cross_pair_rank_position", 0) or 0),
                "cross_pair_influence_score": float(row.get("cross_pair_influence_score", 0.5)),
                "cross_pair_recommendation_strength": float(row.get("cross_pair_recommendation_strength", 0.5)),
                "cross_pair_influenced_by_pairs": list(row.get("cross_pair_influenced_by_pairs", []) or []),
                "cross_pair_reason_codes": list(row.get("cross_pair_reason_codes", []) or []),
                "cross_pair_soft_block": bool(row.get("cross_pair_soft_block", False)),
                "cross_pair_hard_block": bool(row.get("cross_pair_hard_block", False)),
                "entry_supported": bool(entry_supported),
            },
        )

    portfolio_state = _portfolio_state_from_payload(portfolio)
    observation = RLPortfolioObservation(
        ts=str(ts or ""),
        pair_universe=list(pair_universe),
        market_by_pair=market_by_pair,
        features_by_pair={pair: {key: float(value) for key, value in values.items()} for pair, values in features_by_pair.items()},
        portfolio=portfolio_state,
        policy_context=policy_context,
        action_mask={
            pair: {
                "can_open": bool(not proposals_by_pair[pair].metadata.get("open_position", False)),
                "can_close": bool(proposals_by_pair[pair].metadata.get("open_position", False)),
                "can_tighten_stop": bool(proposals_by_pair[pair].metadata.get("open_position", False)),
                "max_position_abs": 1.0,
            }
            for pair in pair_universe
        },
        metadata={
            "supervised_fallback_required": bool(supervised_fallback_required),
            "checkpoint_path": str(effective_checkpoint_path or ""),
            "policy_manifest_path": str(manifest_path or ""),
        },
    ).to_dict()

    return RLPortfolioProposalBundle(
        ts=str(ts or ""),
        pair_universe=list(pair_universe),
        observation=observation,
        proposals_by_pair=proposals_by_pair,
        source=str(source),
        supervised_fallback_used=bool(checkpoint is None),
        fallback_reason=str(fallback_reason),
        checkpoint_path=str(effective_checkpoint_path or ""),
        checkpoint_loaded=bool(checkpoint is not None),
        checkpoint_summary=_checkpoint_summary(checkpoint),
        policy_manifest_path=str(manifest_path or ""),
        policy_manifest=policy_manifest,
        diagnostics={
            "decision_count": int(len(decision_map)),
            "candidate_count": int(len(candidate_map)),
            "checkpoint_loaded": bool(checkpoint is not None),
            "checkpoint_summary": _checkpoint_summary(checkpoint),
            "supervised_fallback_required": bool(supervised_fallback_required),
            "artifact_discovery": {
                "checkpoint_path": str(effective_checkpoint_path or ""),
                "policy_manifest_path": str(manifest_path or ""),
                "checkpoint_loaded": bool(checkpoint is not None),
                "fallback_reason": str(fallback_reason),
                "primary_policy": bool(policy_manifest.get("primary_policy", False)) if policy_manifest else bool(checkpoint is not None),
            },
            "execution_authority": "risk_kernel",
        },
    )
