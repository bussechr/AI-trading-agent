from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from src.trader.domain.plugins import default_plugin_registry
from src.trader.interfaces.dto import DecisionOutcome


@dataclass(slots=True)
class StageTrace:
    stage: str
    accepted: bool
    rejection_reason: str
    attrs: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": str(self.stage),
            "accepted": bool(self.accepted),
            "rejection_reason": str(self.rejection_reason),
            "attrs": dict(self.attrs or {}),
        }


@dataclass(slots=True)
class PipelineResult:
    outcome: DecisionOutcome
    traces: list[StageTrace]
    plugin_errors: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome.to_dict(),
            "traces": [t.to_dict() for t in self.traces],
            "plugin_errors": [dict(x) for x in self.plugin_errors],
        }


@dataclass(slots=True)
class PipelineBatchResult:
    rows: list[PipelineResult]
    rejection_taxonomy: dict[str, int]
    plugin_errors: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows": [r.to_dict() for r in self.rows],
            "rejection_taxonomy": dict(self.rejection_taxonomy),
            "plugin_errors": [dict(x) for x in self.plugin_errors],
        }


class DecisionPipeline:
    """Composable decision pipeline with plugin fault isolation."""

    def __init__(self) -> None:
        self._plugin_registry = default_plugin_registry()
        self._plugin_handlers: dict[str, Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any] | None]] = {
            "hawkes": self._apply_hawkes,
            "lppls": self._apply_lppls,
            "heston": self._apply_heston,
            "ai_indicator": self._apply_ai_indicator,
        }

    @staticmethod
    def _clip(value: float, lo: float, hi: float) -> float:
        return float(max(lo, min(hi, value)))

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return float(default)

    def _apply_hawkes(self, stage: str, candidate: dict[str, Any], diag: dict[str, Any]) -> dict[str, Any] | None:
        if stage != "model_scoring":
            return None
        hawkes_n = self._safe_float(diag.get("hawkes_n", 1.0), 1.0)
        factor = self._clip(hawkes_n, 0.35, 1.60)
        return {
            "model_score": float(candidate.get("model_score", candidate.get("score_effective", 0.0))) * factor,
            "hawkes_factor": float(factor),
        }

    def _apply_lppls(self, stage: str, candidate: dict[str, Any], diag: dict[str, Any]) -> dict[str, Any] | None:
        if stage != "gating":
            return None
        side = str(candidate.get("side", "")).upper()
        hazard = self._safe_float(diag.get("lppls_hazard", 0.0), 0.0)
        if side == "BUY" and hazard >= 0.85:
            return {"blocked_by": "lppls_hazard", "lppls_hazard": float(hazard)}
        return {"lppls_hazard": float(hazard)}

    def _apply_heston(self, stage: str, candidate: dict[str, Any], diag: dict[str, Any]) -> dict[str, Any] | None:
        if stage != "model_scoring":
            return None
        scale = self._clip(self._safe_float(diag.get("heston_scale", 1.0), 1.0), 0.20, 2.50)
        return {
            "model_score": float(candidate.get("model_score", candidate.get("score_effective", 0.0))) * scale,
            "heston_scale": float(scale),
        }

    def _apply_ai_indicator(self, stage: str, candidate: dict[str, Any], diag: dict[str, Any]) -> dict[str, Any] | None:
        if stage != "readiness":
            return None
        hit = self._clip(self._safe_float(diag.get("direction_hit_rate", 0.5), 0.5), 0.0, 1.0)
        conf = self._safe_float(candidate.get("confidence", 0.0), 0.0)
        conf_adj = conf * (0.80 + 0.40 * hit)
        return {"confidence": float(self._clip(conf_adj, 0.0, 100.0)), "ai_hit_rate": float(hit)}

    def _run_plugins(
        self,
        *,
        stage: str,
        candidate: dict[str, Any],
        diag: dict[str, Any],
        plugin_cfg: dict[str, Any],
        plugin_errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        out = dict(candidate)
        enabled_plugins = self._plugin_registry.enabled(plugin_cfg)
        for plugin_name in enabled_plugins:
            handler = self._plugin_handlers.get(str(plugin_name))
            if handler is None:
                continue
            try:
                patch = handler(str(stage), out, diag)
                if isinstance(patch, dict) and patch:
                    out.update(dict(patch))
            except Exception as exc:
                plugin_errors.append(
                    {
                        "plugin": str(plugin_name),
                        "stage": str(stage),
                        "error": str(exc),
                    }
                )
        return out

    def run_candidate(
        self,
        *,
        decision: dict[str, Any],
        diagnostics: dict[str, Any],
        plugin_cfg: dict[str, Any],
        sizing_cfg: dict[str, Any] | None = None,
    ) -> PipelineResult:
        d = dict(decision or {})
        diag = dict((diagnostics or {}).get("last_diag", {}) or {})
        governance = dict((diagnostics or {}).get("governance", {}) or {})

        cfg = dict(sizing_cfg or {})
        min_conf = self._safe_float(cfg.get("min_confidence", 35.0), 35.0)
        base_lot = self._safe_float(cfg.get("base_lot", 0.03), 0.03)
        min_lot = self._safe_float(cfg.get("min_lot", 0.01), 0.01)
        max_lot = self._safe_float(cfg.get("max_lot", 2.00), 2.00)

        symbol = str(d.get("symbol", "")).strip()
        side = str(d.get("side", "")).strip().upper() or "NONE"
        score = self._safe_float(d.get("score", 0.0), 0.0)
        confidence = self._safe_float(d.get("confidence", 0.0), 0.0)

        candidate: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "score": float(score),
            "score_effective": float(
                self._safe_float(d.get("score_effective", diag.get("score_effective", score)), score)
            ),
            "confidence": float(confidence),
            "blocked_by": str(d.get("blocked_by", "none") or "none"),
            "execution_ready": bool(d.get("execution_ready", True)),
            "risk_scale": self._clip(self._safe_float(governance.get("risk_scale", 1.0), 1.0), 0.0, 1.0),
            "governance_paused": bool(governance.get("paused", False)),
            "intent": "HOLD",
            "lots": 0.0,
        }

        traces: list[StageTrace] = []
        plugin_errors: list[dict[str, Any]] = []

        # Stage 1: feature extraction
        p_trend = self._clip(self._safe_float(diag.get("p_trend", 0.5), 0.5), 0.0, 1.0)
        vol = self._safe_float(diag.get("vol", 0.0), 0.0)
        candidate.update(
            {
                "p_trend": float(p_trend),
                "vol": float(vol),
                "regime_bucket": str(diag.get("regime_bucket", "unknown")),
            }
        )
        candidate = self._run_plugins(
            stage="feature_extraction",
            candidate=candidate,
            diag=diag,
            plugin_cfg=plugin_cfg,
            plugin_errors=plugin_errors,
        )
        traces.append(
            StageTrace(
                stage="feature_extraction",
                accepted=True,
                rejection_reason="none",
                attrs={
                    "symbol": symbol,
                    "side": side,
                    "p_trend": float(p_trend),
                    "vol": float(vol),
                    "regime_bucket": str(candidate.get("regime_bucket", "unknown")),
                },
            )
        )

        # Stage 2: model scoring
        gate_penalty = self._clip(self._safe_float(diag.get("gate_penalty", 1.0), 1.0), 0.05, 2.0)
        model_score = float(candidate.get("score_effective", score)) * gate_penalty
        candidate["model_score"] = float(model_score)
        candidate = self._run_plugins(
            stage="model_scoring",
            candidate=candidate,
            diag=diag,
            plugin_cfg=plugin_cfg,
            plugin_errors=plugin_errors,
        )
        traces.append(
            StageTrace(
                stage="model_scoring",
                accepted=True,
                rejection_reason="none",
                attrs={
                    "score": float(score),
                    "gate_penalty": float(gate_penalty),
                    "model_score": self._safe_float(candidate.get("model_score", model_score), model_score),
                },
            )
        )

        # Stage 3: gating
        candidate = self._run_plugins(
            stage="gating",
            candidate=candidate,
            diag=diag,
            plugin_cfg=plugin_cfg,
            plugin_errors=plugin_errors,
        )
        blocked_by = str(candidate.get("blocked_by", d.get("blocked_by", "none")) or "none")
        gate_ok = blocked_by in {"", "none"}
        traces.append(
            StageTrace(
                stage="gating",
                accepted=bool(gate_ok),
                rejection_reason=("none" if gate_ok else str(blocked_by)),
                attrs={"blocked_by": str(blocked_by)},
            )
        )

        # Stage 4: confidence/readiness
        candidate = self._run_plugins(
            stage="readiness",
            candidate=candidate,
            diag=diag,
            plugin_cfg=plugin_cfg,
            plugin_errors=plugin_errors,
        )
        conf_now = self._safe_float(candidate.get("confidence", confidence), confidence)
        gov_pause = bool(candidate.get("governance_paused", False))
        ready = bool(gate_ok and (not gov_pause) and conf_now >= float(min_conf) and side in {"BUY", "SELL"})
        readiness_reason = "none"
        if not gate_ok:
            readiness_reason = str(blocked_by)
        elif gov_pause:
            readiness_reason = "governance_pause"
        elif conf_now < float(min_conf):
            readiness_reason = "low_confidence"
        elif side not in {"BUY", "SELL"}:
            readiness_reason = "no_action"
        candidate["execution_ready"] = bool(ready)
        traces.append(
            StageTrace(
                stage="readiness",
                accepted=bool(ready),
                rejection_reason=str(readiness_reason),
                attrs={
                    "confidence": float(conf_now),
                    "min_confidence": float(min_conf),
                    "governance_paused": bool(gov_pause),
                },
            )
        )

        # Stage 5: sizing
        if ready:
            conf_scale = self._clip(conf_now / 100.0, 0.10, 1.0)
            lot = self._clip(base_lot * conf_scale * float(candidate.get("risk_scale", 1.0)), min_lot, max_lot)
        else:
            lot = 0.0
        candidate["lots"] = float(lot)
        traces.append(
            StageTrace(
                stage="sizing",
                accepted=bool(ready and lot > 0),
                rejection_reason=("none" if ready and lot > 0 else str(readiness_reason)),
                attrs={
                    "lots": float(lot),
                    "base_lot": float(base_lot),
                    "risk_scale": float(candidate.get("risk_scale", 1.0)),
                    "min_lot": float(min_lot),
                    "max_lot": float(max_lot),
                },
            )
        )

        # Stage 6: dispatch intent
        if ready and lot > 0:
            intent = "ENTRY"
            dispatch_reason = "none"
            reasons: list[str] = []
        else:
            intent = "HOLD"
            dispatch_reason = str(readiness_reason)
            reasons = [str(readiness_reason)] if readiness_reason not in {"", "none"} else []
        candidate["intent"] = str(intent)
        traces.append(
            StageTrace(
                stage="dispatch_intent",
                accepted=bool(intent == "ENTRY"),
                rejection_reason=str(dispatch_reason),
                attrs={"intent": str(intent), "lots": float(lot)},
            )
        )

        outcome = DecisionOutcome(
            symbol=str(symbol),
            side=str(side),
            score=float(self._safe_float(candidate.get("model_score", score), score)),
            confidence=float(conf_now),
            execution_ready=bool(intent == "ENTRY"),
            reasons=list(reasons),
            metadata={
                "intent": str(intent),
                "lots": float(lot),
                "blocked_by": str(blocked_by),
                "plugin_errors": [dict(x) for x in plugin_errors],
            },
        )

        return PipelineResult(outcome=outcome, traces=traces, plugin_errors=plugin_errors)

    def run_many(
        self,
        *,
        decisions: list[dict[str, Any]],
        diagnostics: dict[str, Any],
        plugin_cfg: dict[str, Any],
        sizing_cfg: dict[str, Any] | None = None,
    ) -> PipelineBatchResult:
        rows: list[PipelineResult] = []
        taxonomy: dict[str, int] = {}
        all_errors: list[dict[str, Any]] = []

        for decision in list(decisions or []):
            res = self.run_candidate(
                decision=dict(decision or {}),
                diagnostics=dict(diagnostics or {}),
                plugin_cfg=dict(plugin_cfg or {}),
                sizing_cfg=dict(sizing_cfg or {}),
            )
            rows.append(res)
            all_errors.extend(list(res.plugin_errors))
            final = res.traces[-1] if res.traces else None
            reason = "none"
            if final is not None and not bool(final.accepted):
                reason = str(final.rejection_reason or "none")
            taxonomy[reason] = int(taxonomy.get(reason, 0)) + 1

        return PipelineBatchResult(rows=rows, rejection_taxonomy=taxonomy, plugin_errors=all_errors)
