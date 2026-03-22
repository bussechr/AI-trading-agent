"""Tick, report, and decision recording operations."""

from __future__ import annotations

import sqlite3
from typing import Any

from src.trader.domain.decision_pipeline import DecisionPipeline
from src.trader.domain.risk_envelope import compute_adaptive_risk_envelope
from src.trader.utils import safe_float as _safe_float

from ._helpers import _jdump, _now


class TickStoreMixin:
    """Mixin for tick/report/decision persistence on ``RuntimeStore``."""

    _conn: sqlite3.Connection
    _lock: Any  # threading.RLock
    _decision_pipeline: DecisionPipeline
    soft_band: tuple[float, float]
    hard_band: tuple[float, float]
    daily_band: tuple[float, float]
    sizing_band: tuple[float, float, float]

    def record_tick(self, payload: dict[str, Any]) -> None:
        sym = str(payload.get("symbol", "")).strip()
        if not sym:
            return

        with self._lock:
            now_ts = _now()
            ts = payload.get("ts", payload.get("time", now_ts))
            try:
                ts_f = float(ts)
            except Exception:
                ts_f = now_ts

            bid = float(payload.get("bid", 0.0) or 0.0)
            ask = float(payload.get("ask", 0.0) or 0.0)
            spread = float(payload.get("spread", 0.0) or 0.0)
            self._conn.execute(
                "INSERT INTO market_ticks(symbol, bid, ask, spread, ts, raw_json) VALUES(?, ?, ?, ?, ?, ?)",
                (sym, bid, ask, spread, ts_f, _jdump(dict(payload or {}))),
            )
            self._conn.commit()

    def _insert_account_snapshot_locked(self, *, now_ts: float, source: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO account_snapshots(ts, equity, margin, freemargin, leverage, source, raw_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(now_ts),
                _safe_float(payload.get("equity", 0.0), 0.0),
                _safe_float(payload.get("margin", 0.0), 0.0),
                _safe_float(payload.get("freemargin", 0.0), 0.0),
                _safe_float(payload.get("leverage", 0.0), 0.0),
                str(source or "unknown"),
                _jdump(dict(payload or {})),
            ),
        )

    def _insert_position_snapshot_locked(self, *, now_ts: float, source: str, positions: list[dict[str, Any]]) -> None:
        self._conn.execute(
            "INSERT INTO position_snapshots(ts, source, positions_json) VALUES(?, ?, ?)",
            (
                float(now_ts),
                str(source or "unknown"),
                _jdump(list(positions or [])),
            ),
        )

    @staticmethod
    def _governance_view(gov: dict[str, Any]) -> dict[str, Any]:
        reasons = [str(x) for x in list(gov.get("reasons", []) or [])]
        return {
            "paused": bool(gov.get("paused", False)),
            "risk_scale": _safe_float(gov.get("risk_scale", 1.0), 1.0),
            "reasons": reasons,
            "drawdown_pct": _safe_float(gov.get("drawdown_pct", 0.0), 0.0),
            "daily_loss_pct": _safe_float(gov.get("daily_loss_pct", 0.0), 0.0),
        }

    @staticmethod
    def _plugin_cfg_from_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
        plugin_flags = dict((diagnostics or {}).get("plugin_flags", {}) or {})
        if plugin_flags:
            return plugin_flags
        last_diag = dict((diagnostics or {}).get("last_diag", {}) or {})
        return {
            "use_hawkes": bool(last_diag.get("hawkes_n", 0.0)),
            "use_lppls": bool(last_diag.get("lppls_hazard", 0.0)),
            "use_heston_guard": bool(abs(_safe_float(last_diag.get("heston_scale", 1.0), 1.0) - 1.0) > 1e-9),
            "use_ai_indicator_model": (
                bool(last_diag.get("ai_enabled", False))
                or bool(last_diag.get("direction_samples", 0))
                or bool(last_diag.get("direction_side_samples", 0))
            ),
        }

    @staticmethod
    def _stage_attribution(diagnostics: dict[str, Any]) -> dict[str, Any]:
        last_diag = dict((diagnostics or {}).get("last_diag", {}) or {})
        return {
            "feature_extraction": {
                "vol": _safe_float(last_diag.get("vol", 0.0), 0.0),
                "p_trend": _safe_float(last_diag.get("p_trend", 0.5), 0.5),
                "regime_bucket": str(last_diag.get("regime_bucket", "unknown")),
            },
            "model_scoring": {
                "score": _safe_float(last_diag.get("score", 0.0), 0.0),
                "score_effective": _safe_float(last_diag.get("score_effective", last_diag.get("score", 0.0)), 0.0),
                "raw_signal": _safe_float(last_diag.get("raw_signal", 0.0), 0.0),
                "momentum_component": _safe_float(last_diag.get("momentum_component", 0.0), 0.0),
                "micro_component": _safe_float(last_diag.get("micro_component", 0.0), 0.0),
                "gate_penalty": _safe_float(last_diag.get("gate_penalty", 1.0), 1.0),
            },
            "gating": {
                "entry_gate_mode": str((diagnostics or {}).get("entry_gate_mode", "unknown")),
                "execution_gate_mode": str((diagnostics or {}).get("execution_gate_mode", "unknown")),
                "utility_gate_mode": str((diagnostics or {}).get("utility_gate_mode", "off")),
            },
            "confidence_execution_readiness": {
                "predictive_sharpe": _safe_float(last_diag.get("predictive_sharpe", 0.0), 0.0),
                "predictive_sharpe_aligned": _safe_float(last_diag.get("predictive_sharpe_aligned", 0.0), 0.0),
                "horizon_confidence": _safe_float(last_diag.get("horizon_confidence", 0.0), 0.0),
            },
            "sizing_dispatch": {
                "portfolio_risk": dict((diagnostics or {}).get("portfolio_risk", {}) or {}),
                "governance": dict((diagnostics or {}).get("governance", {}) or {}),
                "risk_envelope": dict((diagnostics or {}).get("risk_envelope", {}) or {}),
            },
        }

    def record_report(self, report_text: str, report_json: dict[str, Any] | None = None) -> None:
        with self._lock:
            now_ts = _now()
            self._conn.execute(
                "INSERT INTO reports(ts, report_text, report_json) VALUES(?, ?, ?)",
                (now_ts, str(report_text or ""), _jdump(report_json) if report_json else ""),
            )

            state = self._get_state_locked()
            if report_json and isinstance(report_json, dict):
                typ = str(report_json.get("type", "")).upper().strip()
                if typ == "HEARTBEAT":
                    state["last_heartbeat"] = now_ts
                    state["system_status"] = "connected"
                    state["equity"] = float(report_json.get("equity", 0.0) or 0.0)
                    state["margin"] = float(report_json.get("margin", 0.0) or 0.0)
                    state["freemargin"] = float(report_json.get("freemargin", 0.0) or 0.0)
                    state["leverage"] = float(report_json.get("leverage", 0.0) or 0.0)
                    self._insert_account_snapshot_locked(now_ts=now_ts, source="heartbeat", payload=report_json)
                elif typ == "POSITIONS":
                    positions = list(report_json.get("positions", []) or [])
                    state["positions"] = positions
                    state["last_pos_update"] = now_ts
                    self._insert_position_snapshot_locked(now_ts=now_ts, source="positions", positions=positions)

            state["last_update"] = now_ts
            self._put_state_locked(state, now_ts)
            self._conn.commit()

    def store_decisions(self, *, decisions: list[dict[str, Any]], vol: float, diagnostics: dict[str, Any]) -> None:
        with self._lock:
            now_ts = _now()
            state = self._get_state_locked()

            plugin_cfg = self._plugin_cfg_from_diagnostics(diagnostics)
            execution_quality = dict((diagnostics or {}).get("execution_quality", {}) or {})
            batch = self._decision_pipeline.run_many(
                decisions=list(decisions or []),
                diagnostics=dict(diagnostics or {}),
                plugin_cfg=plugin_cfg,
                sizing_cfg={
                    "min_confidence": _safe_float(execution_quality.get("min_confidence", 35.0), 35.0),
                    "base_lot": float(self.sizing_band[0]),
                    "min_lot": float(self.sizing_band[1]),
                    "max_lot": float(self.sizing_band[2]),
                },
            )

            rejection = dict(batch.rejection_taxonomy or {})
            if not rejection:
                rejection = dict((diagnostics or {}).get("rejection_stats", {}) or {})
            attribution = {
                "pipeline_rows": [row.to_dict() for row in batch.rows],
                "plugin_errors": [dict(err) for err in batch.plugin_errors],
                "diagnostics_stage_attribution": self._stage_attribution(diagnostics),
            }
            self._conn.execute(
                """
                INSERT INTO decision_snapshots(ts, vol, decisions_json, diagnostics_json, rejection_json, attribution_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    float(now_ts),
                    float(vol or 0.0),
                    _jdump(list(decisions or [])),
                    _jdump(dict(diagnostics or {})),
                    _jdump(rejection),
                    _jdump(attribution),
                ),
            )

            gov = dict((diagnostics or {}).get("governance", {}) or {})
            prev_gov = dict(state.get("governance", {}) or {})
            prev_fp = str(state.get("_governance_fp", ""))
            next_view = self._governance_view(gov) if gov else {}
            next_fp = _jdump(next_view) if next_view else ""
            if gov and next_fp != prev_fp:
                prev_paused = bool(prev_gov.get("paused", False))
                next_paused = bool(gov.get("paused", False))
                if next_paused and not prev_paused:
                    event_type = "pause_on"
                elif prev_paused and not next_paused:
                    event_type = "pause_off"
                else:
                    event_type = "state_update"
                reasons = [str(x) for x in list(gov.get("reasons", []) or [])]
                reason = ",".join(reasons[:3]) if reasons else "governance_update"
                event_payload = {
                    "governance": dict(gov),
                    "vol": float(vol or 0.0),
                    "p_trend": _safe_float((diagnostics or {}).get("last_diag", {}).get("p_trend", 0.5), 0.5),
                }
                self._conn.execute(
                    "INSERT INTO governance_events(ts, event_type, reason, payload_json) VALUES(?, ?, ?, ?)",
                    (float(now_ts), str(event_type), str(reason), _jdump(event_payload)),
                )
                state["governance_last_event"] = {
                    "time": float(now_ts),
                    "event_type": str(event_type),
                    "reason": str(reason),
                }
                state["_governance_fp"] = str(next_fp)

            p_trend = _safe_float((diagnostics or {}).get("last_diag", {}).get("p_trend", 0.5), 0.5)
            env = compute_adaptive_risk_envelope(
                volatility=float(vol or 0.0),
                trend_prob=float(p_trend),
                soft_band=self.soft_band,
                hard_band=self.hard_band,
                daily_band=self.daily_band,
                now_ts=now_ts,
            )

            state["agent_decisions"] = list(decisions or [])
            state["agent_diagnostics"] = dict(diagnostics or {})
            state["monitor"] = dict((diagnostics or {}).get("monitor", {}) or {})
            state["vol"] = float(vol or 0.0)
            state["decision_pipeline_plugin_errors"] = [dict(err) for err in batch.plugin_errors[:20]]
            if gov:
                state["governance"] = dict(gov)
            state["risk_envelope"] = env.to_dict()
            state["last_update"] = now_ts

            self._put_state_locked(state, now_ts)
            self._conn.commit()

    def update_state_patch(self, patch: dict[str, Any]) -> None:
        if not patch:
            return
        with self._lock:
            now_ts = _now()
            state = self._get_state_locked()
            state.update(dict(patch))
            state["last_update"] = now_ts
            self._put_state_locked(state, now_ts)
            self._conn.commit()
