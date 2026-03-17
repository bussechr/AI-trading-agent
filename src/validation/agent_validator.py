"""
Agent validation - ensures chaos/randomness strategy behavior, not just oscillation.
Run this before each session to verify all gates and checks are functioning.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
from typing import Any
import logging

logger = logging.getLogger(__name__)

class AgentValidator:
    """Validates that the agent properly models randomness and respects mini-only constraints."""
    
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.checks_passed = []
        self.checks_failed = []
    
    def validate_all(self) -> bool:
        """Run all validation checks. Returns True if all pass."""
        logger.info("=" * 60)
        logger.info("AGENT VALIDATION CHECKLIST")
        logger.info("=" * 60)
        
        checks = [
            self.check_mini_only_config(),
            self.check_risk_knobs_bounded(),
            self.check_el_parameters(),
            self.check_gate_thresholds(),
            self.check_target_ranges(),
            self.check_correlation_filter(),
            self.check_cost_gate_setup()
        ]
        
        all_passed = all(checks)
        
        logger.info("\n" + "=" * 60)
        logger.info(f"VALIDATION RESULT: {'✓ PASS' if all_passed else '✗ FAIL'}")
        logger.info(f"Passed: {len(self.checks_passed)}/{len(checks)}")
        if self.checks_failed:
            logger.error(f"Failed checks: {', '.join(self.checks_failed)}")
        logger.info("=" * 60)
        
        return all_passed
    
    def _pass(self, name: str, detail: str = "") -> bool:
        self.checks_passed.append(name)
        logger.info(f"✓ {name}" + (f" - {detail}" if detail else ""))
        return True
    
    def _fail(self, name: str, reason: str) -> bool:
        self.checks_failed.append(name)
        logger.error(f"✗ {name} - {reason}")
        return False
    
    def check_mini_only_config(self) -> bool:
        """A. Verify mini symbols only configuration."""
        logger.info("\nA. MINI SYMBOLS ONLY")
        
        mini_suffixes = self.cfg.get("mini_suffixes", [])
        roots = self.cfg.get("symbols_roots", [])
        active = self.cfg.get("active_symbols", []) or []
        
        if not roots:
            return self._fail("Mini config", "No symbols_roots defined")

        if active:
            roots_set = {str(r).upper() for r in roots}
            active_set = {str(a).upper() for a in active}
            missing = sorted(active_set - roots_set)
            if missing:
                return self._fail("Mini config", f"active_symbols not in symbols_roots: {missing[:5]}")
        
        # For IG: empty suffixes is valid (minis via lot size)
        if not mini_suffixes:
            logger.info("  IG mode: minis determined by 0.10 lot size, not suffix")

        active_detail = f", active={len(active)}" if active else ""
        return self._pass("Mini config", f"{len(roots)} roots{active_detail}, suffixes={mini_suffixes}")
    
    def check_risk_knobs_bounded(self) -> bool:
        """G. Risk parameters within safe ranges."""
        logger.info("\nG. RISK KNOBS BOUNDED")
        
        target_base = self.cfg.get("target_base_pct", 0.01)
        if not (0.005 <= target_base <= 0.03):
            return self._fail("target_base_pct", f"{target_base} not in [0.5%, 3%]")
        
        corr_max = self.cfg.get("corr_max", 0.70)
        if not (0.5 <= corr_max <= 0.9):
            return self._fail("corr_max", f"{corr_max} not in [0.5, 0.9]")
        
        score_threshold = self.cfg.get("score_threshold", 0.40)
        if not (0.15 <= score_threshold <= 0.8):
            return self._fail("score_threshold", f"{score_threshold} not in [0.15, 0.8]")
        
        max_concurrent = self.cfg.get("max_concurrent", 4)
        if not (1 <= max_concurrent <= 10):
            return self._fail("max_concurrent", f"{max_concurrent} not in [1, 10]")

        execution_mode = str(self.cfg.get("execution_mode", "full_live")).strip().lower()
        if execution_mode not in {"full_live", "close_only", "read_only"}:
            return self._fail("execution_mode", f"{execution_mode} invalid (full_live|close_only|read_only)")

        max_new_entries = int(self.cfg.get("max_new_entries_per_minute", 12))
        max_total_cmds = int(self.cfg.get("max_total_commands_per_minute", 60))
        if max_new_entries < 1:
            return self._fail("max_new_entries_per_minute", f"{max_new_entries} must be >= 1")
        if max_total_cmds < max_new_entries:
            return self._fail(
                "max_total_commands_per_minute",
                f"{max_total_cmds} must be >= max_new_entries_per_minute={max_new_entries}",
            )

        audit_sample_rate = float(self.cfg.get("audit_sample_rate", 1.0))
        if not (0.0 <= audit_sample_rate <= 1.0):
            return self._fail("audit_sample_rate", f"{audit_sample_rate} not in [0, 1]")
        audit_replay_mode = str(self.cfg.get("audit_replay_mode", "offline")).strip().lower()
        if audit_replay_mode not in {"offline", "live_like"}:
            return self._fail("audit_replay_mode", f"{audit_replay_mode} invalid (offline|live_like)")

        interop_sample_rate = float(self.cfg.get("interop_audit_sample_rate", 1.0))
        if not (0.0 <= interop_sample_rate <= 1.0):
            return self._fail("interop_audit_sample_rate", f"{interop_sample_rate} not in [0, 1]")
        interop_mode = str(self.cfg.get("interop_audit_mode", "live_shadow")).strip().lower()
        if interop_mode not in {"live_shadow", "replay_live_like", "replay_offline"}:
            return self._fail(
                "interop_audit_mode",
                f"{interop_mode} invalid (live_shadow|replay_live_like|replay_offline)",
            )
        startup_warmup_bars = int(self.cfg.get("startup_warmup_min_live_bars", 24))
        startup_warmup_hours = float(self.cfg.get("startup_warmup_min_tick_hours", 6))
        if startup_warmup_bars < 1:
            return self._fail("startup_warmup_min_live_bars", f"{startup_warmup_bars} must be >= 1")
        if startup_warmup_hours <= 0:
            return self._fail("startup_warmup_min_tick_hours", f"{startup_warmup_hours} must be > 0")

        starvation_window = int(self.cfg.get("starvation_window_cycles", 36))
        starvation_step = int(self.cfg.get("starvation_step_cycles", 12))
        starvation_share = float(self.cfg.get("starvation_reject_share_min", 0.60))
        if starvation_window < 1:
            return self._fail("starvation_window_cycles", f"{starvation_window} must be >= 1")
        if starvation_step < 1:
            return self._fail("starvation_step_cycles", f"{starvation_step} must be >= 1")
        if not (0.0 <= starvation_share <= 1.0):
            return self._fail("starvation_reject_share_min", f"{starvation_share} not in [0, 1]")

        breaker = float(self.cfg.get("daily_loss_breaker_pct", 0.03))
        if not (0.0 < breaker <= 0.20):
            return self._fail("daily_loss_breaker_pct", f"{breaker} not in (0, 0.20]")
        buckets = self.cfg.get("interop_latency_buckets_ms", [25, 50, 100])
        try:
            parsed = [int(float(v)) for v in list(buckets)]
            if not parsed or any(v <= 0 for v in parsed):
                return self._fail("interop_latency_buckets_ms", f"{buckets} must be positive integers")
        except Exception:
            return self._fail("interop_latency_buckets_ms", f"{buckets} invalid")
        
        return self._pass(
            "Risk knobs",
            f"All parameters within safe ranges (mode={execution_mode}, entries/min={max_new_entries}, cmds/min={max_total_cmds})",
        )
    
    def check_el_parameters(self) -> bool:
        """B. EL momentum parameters well-formed."""
        logger.info("\nB. EL MOMENTUM PARAMETERS")
        
        el_window = self.cfg.get("el_window", 48)
        if el_window < 20:
            return self._fail("el_window", f"{el_window} too small, min 20")
        
        el_ema_span = self.cfg.get("el_ema_span", 10)
        if el_ema_span < 3:
            return self._fail("el_ema_span", f"{el_ema_span} too small, min 3")
        
        lookback = self.cfg.get("lookback_bars", 400)
        if lookback < el_window * 3:
            return self._fail("lookback_bars", f"{lookback} < 3× el_window")
        
        return self._pass("EL parameters", f"window={el_window}, ema={el_ema_span}, lookback={lookback}")
    
    def check_gate_thresholds(self) -> bool:
        """C. Cost and edge gates configured."""
        logger.info("\nC. GATES CONFIGURED")
        
        avg_spread = self.cfg.get("avg_spread_pips", 0.8)
        if avg_spread <= 0 or avg_spread > 5.0:
            return self._fail("avg_spread_pips", f"{avg_spread} unrealistic")
        
        pip_value = self.cfg.get("pip_value_per_lot", 1.0)
        if pip_value <= 0:
            return self._fail("pip_value_per_lot", f"{pip_value} invalid")
        
        return self._pass("Gate thresholds", f"spread={avg_spread}pips, pip_val={pip_value}")
    
    def check_target_ranges(self) -> bool:
        """D. Target sizing parameters."""
        logger.info("\nD. TARGET PARAMETERS")
        
        vol_ref = self.cfg.get("vol_ref", 0.010)
        if not (0.005 <= vol_ref <= 0.05):
            return self._fail("vol_ref", f"{vol_ref} not in [0.5%, 5%]")
        
        use_dynamic = self.cfg.get("use_dynamic_target", True)
        logger.info(f"  Dynamic target scaling: {use_dynamic}")
        
        return self._pass("Target ranges", f"vol_ref={vol_ref*100:.1f}%, dynamic={use_dynamic}")
    
    def check_correlation_filter(self) -> bool:
        """C. Correlation filter configured."""
        logger.info("\nC. CORRELATION FILTER")
        
        max_concurrent = self.cfg.get("max_concurrent", 4)
        corr_max = self.cfg.get("corr_max", 0.70)
        
        logger.info(f"  Max concurrent: {max_concurrent}")
        logger.info(f"  Max correlation: {corr_max}")
        
        return self._pass("Correlation filter", f"k={max_concurrent}, ρ_max={corr_max}")
    
    def check_cost_gate_setup(self) -> bool:
        """C. Cost gate properly configured."""
        logger.info("\nC. COST GATE")
        
        avg_spread = self.cfg.get("avg_spread_pips", 0.8)
        pip_value = self.cfg.get("pip_value_per_lot", 1.0)
        ig_mini_lot = self.cfg.get("ig_mini_lot_size", 0.10)
        
        # Estimate cost at 10k equity for sanity
        test_equity = 10000.0
        cost_fraction = (avg_spread * pip_value * ig_mini_lot) / test_equity
        
        logger.info(f"  Spread: {avg_spread} pips")
        logger.info(f"  Pip value: ${pip_value}/lot")
        logger.info(f"  Mini lot: {ig_mini_lot}")
        logger.info(f"  Est. cost @ $10k: {cost_fraction*100:.3f}% ({cost_fraction*100*3:.3f}% threshold)")
        
        if cost_fraction > 0.01:  # More than 1% cost is suspicious
            return self._fail("Cost gate", f"Cost {cost_fraction*100:.2f}% too high")
        
        return self._pass("Cost gate", f"Cost {cost_fraction*100:.4f}% reasonable")


class RuntimeValidator:
    """Runtime validation during trading - checks signal quality."""
    
    def __init__(self):
        self.pz_history = []
        self.tilt_history = []
        self.score_history = []
        self.decision_count = 0
        self.rejection_reasons = {}
    
    def validate_el_momentum(self, pz: float, symbol: str) -> tuple[bool, str]:
        """B. Check EL momentum is well-formed."""
        if not np.isfinite(pz):
            return False, f"pz NaN/Inf for {symbol}"
        
        self.pz_history.append(pz)
        if len(self.pz_history) > 100:
            self.pz_history.pop(0)
        
        # Check for flatline (stuck indicator)
        if len(self.pz_history) >= 20:
            recent = self.pz_history[-20:]
            if np.std(recent) < 1e-6:
                return False, f"pz flatlined at {pz:.4f} for {symbol}"
        
        return True, ""
    
    def validate_regime_tilt(self, tilt: float, symbol: str) -> tuple[bool, str]:
        """B. Check regime tilt is centered."""
        if not np.isfinite(tilt):
            return False, f"tilt NaN/Inf for {symbol}"
        
        if not (-1.0 <= tilt <= 1.0):
            return False, f"tilt {tilt} out of [-1, 1] for {symbol}"
        
        self.tilt_history.append(tilt)
        if len(self.tilt_history) > 100:
            self.tilt_history.pop(0)
        
        # Check not stuck at extremes
        if len(self.tilt_history) >= 20:
            recent = self.tilt_history[-20:]
            if all(abs(t) > 0.95 for t in recent):
                return False, f"tilt stuck at extremes for {symbol}"
        
        return True, ""
    
    def validate_score(self, score: float, pz: float, tilt: float, symbol: str) -> tuple[bool, str]:
        """B. Check score formula applied correctly."""
        expected_score = pz * tilt
        if abs(score - expected_score) > 1e-6:
            return False, f"score mismatch: {score:.4f} != pz*tilt={expected_score:.4f}"
        
        self.score_history.append(score)
        if len(self.score_history) > 100:
            self.score_history.pop(0)
        
        return True, ""
    
    def log_rejection(self, reason: str):
        """Track rejection reasons for diagnostics."""
        self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
    
    def get_diagnostics(self) -> dict:
        """Get diagnostic statistics."""
        return {
            "pz_median": float(np.median(self.pz_history)) if self.pz_history else 0.0,
            "pz_std": float(np.std(self.pz_history)) if self.pz_history else 0.0,
            "tilt_median": float(np.median(self.tilt_history)) if self.tilt_history else 0.0,
            "tilt_std": float(np.std(self.tilt_history)) if self.tilt_history else 0.0,
            "score_median": float(np.median(self.score_history)) if self.score_history else 0.0,
            "decisions": self.decision_count,
            "rejections": dict(self.rejection_reasons)
        }
