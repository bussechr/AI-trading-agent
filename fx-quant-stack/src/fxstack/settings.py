from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_provider: str = Field(default="dukascopy", alias="FXSTACK_DATA_PROVIDER")
    dukascopy_source_root: str = Field(default="fx-quant-stack/data/dukascopy", alias="FXSTACK_DUKASCOPY_SOURCE_ROOT")
    dukascopy_file_pattern: str = Field(default="{pair}_{granularity}.csv", alias="FXSTACK_DUKASCOPY_FILE_PATTERN")

    database_url: str = Field(
        default="postgresql+psycopg://fx:fx@localhost:5432/fxstack",
        alias="FXSTACK_DATABASE_URL",
    )
    mt4_bridge_url: str = Field(default="http://127.0.0.1:58710", alias="MT4_BRIDGE_URL")
    bridge_stale_heartbeat_secs: float = Field(default=30.0, alias="FXSTACK_BRIDGE_STALE_HEARTBEAT_SECS")
    bridge_stale_tick_secs: float = Field(default=30.0, alias="FXSTACK_BRIDGE_STALE_TICK_SECS")

    command_ttl_secs: float = Field(default=120.0, alias="FXSTACK_COMMAND_TTL_SECS")
    default_session_id: str = Field(default="default", alias="FXSTACK_DEFAULT_SESSION_ID")
    pg_service_name: str = Field(default="", alias="FXSTACK_PG_SERVICE_NAME")
    start_profile: str = Field(default="staged_safe", alias="FXSTACK_START_PROFILE")
    run_fast_gate: bool = Field(default=False, alias="FXSTACK_RUN_FAST_GATE")
    run_shadow_24h: bool = Field(default=False, alias="FXSTACK_RUN_SHADOW_24H")
    allow_sqlite: bool = Field(default=False, alias="FXSTACK_ALLOW_SQLITE")
    require_active_models: bool = Field(default=True, alias="FXSTACK_REQUIRE_ACTIVE_MODELS")
    pairs_csv: str = Field(
        default=(
            "EURUSD,USDJPY,GBPUSD,AUDUSD,USDCHF,USDCAD,NZDUSD,"
            "EURJPY,EURGBP,GBPJPY,EURCHF,AUDJPY,EURAUD,"
            "CADJPY,CHFJPY,GBPCHF,EURCAD,GBPCAD"
        ),
        alias="FXSTACK_PAIRS",
    )
    intraday_timeframe: str = Field(default="M5", alias="FXSTACK_INTRADAY_TIMEFRAME")
    swing_timeframe: str = Field(default="D", alias="FXSTACK_SWING_TIMEFRAME")
    regime_timeframe: str = Field(default="H4", alias="FXSTACK_REGIME_TIMEFRAME")
    max_pair_positions: int = Field(default=1, alias="FXSTACK_MAX_PAIR_POSITIONS")
    max_total_positions: int = Field(default=6, alias="FXSTACK_MAX_TOTAL_POSITIONS")
    default_order_lots: float = Field(default=0.1, alias="FXSTACK_DEFAULT_ORDER_LOTS")
    equity_lots_per_usd: float = Field(default=0.00004, alias="FXSTACK_EQUITY_LOTS_PER_USD")
    min_order_lots: float = Field(default=0.01, alias="FXSTACK_MIN_ORDER_LOTS")
    order_lot_step: float = Field(default=0.01, alias="FXSTACK_ORDER_LOT_STEP")
    max_order_lots: float = Field(default=0.0, alias="FXSTACK_MAX_ORDER_LOTS")
    min_swing_prob: float = Field(default=0.58, alias="FXSTACK_MIN_SWING_PROB")
    min_entry_prob: float = Field(default=0.62, alias="FXSTACK_MIN_ENTRY_PROB")
    min_trade_prob: float = Field(default=0.60, alias="FXSTACK_MIN_TRADE_PROB")
    max_allowed_spread_bps: float = Field(default=2.5, alias="FXSTACK_MAX_ALLOWED_SPREAD_BPS")
    min_expected_edge_bps: float = Field(default=3.0, alias="FXSTACK_MIN_EXPECTED_EDGE_BPS")
    policy_version: str = Field(default="fxstack_policy_v1", alias="FXSTACK_POLICY_VERSION")
    frame_profile: str = Field(default="baseline_v2", alias="FXSTACK_FRAME_PROFILE")
    swing_primary_timeframe: str = Field(default="D", alias="FXSTACK_SWING_PRIMARY_TIMEFRAME")
    enable_lifecycle_actions: bool = Field(default=True, alias="FXSTACK_ENABLE_LIFECYCLE_ACTIONS")
    enable_adjust_actions: bool = Field(default=False, alias="FXSTACK_ENABLE_ADJUST_ACTIONS")
    hard_time_stop_secs: float = Field(default=0.0, alias="FXSTACK_HARD_TIME_STOP_SECS")
    adjust_stop_buffer_pips: float = Field(default=0.0, alias="FXSTACK_ADJUST_STOP_BUFFER_PIPS")
    partial_close_fraction: float = Field(default=0.5, alias="FXSTACK_PARTIAL_CLOSE_FRACTION")
    partial_close_cooldown_secs: float = Field(default=1800.0, alias="FXSTACK_PARTIAL_CLOSE_COOLDOWN_SECS")
    max_partial_closes_per_position: int = Field(default=2, alias="FXSTACK_MAX_PARTIAL_CLOSES_PER_POSITION")
    lifecycle_model_action_min_prob: float = Field(default=0.50, alias="FXSTACK_LIFECYCLE_MODEL_ACTION_MIN_PROB")
    reversal_failure_min_prob: float = Field(default=0.50, alias="FXSTACK_REVERSAL_FAILURE_MIN_PROB")
    reversal_opportunity_min_prob: float = Field(default=0.50, alias="FXSTACK_REVERSAL_OPPORTUNITY_MIN_PROB")
    runtime_state_prune_stale_keys: bool = Field(default=True, alias="FXSTACK_RUNTIME_STATE_PRUNE_STALE_KEYS")
    runtime_state_stale_keys_csv: str = Field(
        default=(
            "lifecycle_action,lifecycle_action_score,reversal_should_exit,"
            "reversal_reasons,exit_action_selected,exit_action_probs,"
            "lifecycle_soft_degrade_reasons,lifecycle_capabilities,"
            "last_signal,last_ack,signals_sent,trades_executed,"
            "cycle_active,cycle_start_equity,cycle_target,current_thought,"
            "agent_decisions,agent_diagnostics,monitor,vol,runtime_diag"
        ),
        alias="FXSTACK_RUNTIME_STATE_STALE_KEYS",
    )
    model_activation_manifest: str = Field(
        default="fx-quant-stack/artifacts/active_models.json",
        alias="FXSTACK_MODEL_ACTIVATION_MANIFEST",
    )
    registry_root: str = Field(default="fx-quant-stack/artifacts/registry", alias="FXSTACK_REGISTRY_ROOT")
    startup_requeue_age_secs: float = Field(default=90.0, alias="FXSTACK_REQUEUE_AGE_SECS")
    db_connect_retries: int = Field(default=5, alias="FXSTACK_DB_CONNECT_RETRIES")
    runtime_allow_create_all: bool = Field(default=False, alias="FXSTACK_RUNTIME_ALLOW_CREATE_ALL")
    require_cuda: bool = Field(default=True, alias="FXSTACK_REQUIRE_CUDA")
    bridge_api_key: str = Field(default="", alias="FXSTACK_BRIDGE_API_KEY")
    strict_activation: bool = Field(default=True, alias="FXSTACK_STRICT_ACTIVATION")
    require_lifecycle_artifacts: bool = Field(default=True, alias="FXSTACK_REQUIRE_LIFECYCLE_ARTIFACTS")
    require_hierarchical_intraday_contract: bool = Field(
        default=False,
        alias="FXSTACK_REQUIRE_HIERARCHICAL_INTRADAY_CONTRACT",
    )
    allow_heuristic_meta_labels: bool = Field(default=False, alias="FXSTACK_ALLOW_HEURISTIC_META_LABELS")
    strict_command_validation: bool = Field(default=True, alias="FXSTACK_STRICT_COMMAND_VALIDATION")
    deep_model_stale_hours: float = Field(default=24.0, alias="FXSTACK_DEEP_MODEL_STALE_HOURS")
    tier1_pairs_csv: str = Field(
        default="EURUSD,GBPUSD,USDJPY,AUDUSD",
        alias="FXSTACK_TIER1_PAIRS",
    )
    intraday_retrain_min_new_rows: int = Field(default=500, alias="FXSTACK_INTRADAY_RETRAIN_MIN_NEW_ROWS")
    meta_retrain_min_new_rows: int = Field(default=500, alias="FXSTACK_META_RETRAIN_MIN_NEW_ROWS")
    lifecycle_retrain_min_new_events: int = Field(default=100, alias="FXSTACK_LIFECYCLE_RETRAIN_MIN_NEW_EVENTS")
    deep_retrain_max_age_hours: float = Field(default=72.0, alias="FXSTACK_DEEP_RETRAIN_MAX_AGE_HOURS")
    deep_retrain_min_new_rows: int = Field(default=2000, alias="FXSTACK_DEEP_RETRAIN_MIN_NEW_ROWS")
    force_weekly_retrain_day: str = Field(default="saturday", alias="FXSTACK_FORCE_WEEKLY_RETRAIN_DAY")
    weekly_full_retrain_time: str = Field(default="03:00", alias="FXSTACK_WEEKLY_FULL_RETRAIN_TIME")
    weekly_auto_activate: bool = Field(default=True, alias="FXSTACK_WEEKLY_AUTO_ACTIVATE")
    drift_trigger_ece: float = Field(default=0.20, alias="FXSTACK_DRIFT_TRIGGER_ECE")
    drift_trigger_throughput_drop: float = Field(default=0.08, alias="FXSTACK_DRIFT_TRIGGER_THROUGHPUT_DROP")
    live_spread_reject_rate_trigger: float = Field(default=0.25, alias="FXSTACK_LIVE_SPREAD_REJECT_RATE_TRIGGER")
    model_load_timeout_secs: float = Field(default=12.0, alias="FXSTACK_MODEL_LOAD_TIMEOUT_SECS")
    runtime_startup_progress_stale_secs: float = Field(
        default=180.0,
        alias="FXSTACK_RUNTIME_STARTUP_PROGRESS_STALE_SECS",
    )
    swing_model_policy: str = Field(
        default="xgb_only",
        alias="FXSTACK_SWING_MODEL_POLICY",
    )
    intraday_model_policy: str = Field(
        default="xgb_only",
        alias="FXSTACK_INTRADAY_MODEL_POLICY",
    )
    tcn_window_size: int = Field(default=128, alias="FXSTACK_TCN_WINDOW_SIZE")
    transformer_window_size: int = Field(default=96, alias="FXSTACK_TRANSFORMER_WINDOW_SIZE")
    deep_train_epochs: int = Field(default=5, alias="FXSTACK_DEEP_TRAIN_EPOCHS")
    deep_batch_size: int = Field(default=64, alias="FXSTACK_DEEP_BATCH_SIZE")
    xgb_device: str = Field(default="auto", alias="FXSTACK_XGB_DEVICE")
    xgb_tree_method: str = Field(default="hist", alias="FXSTACK_XGB_TREE_METHOD")
    xgb_allow_cpu_fallback: bool = Field(default=True, alias="FXSTACK_XGB_ALLOW_CPU_FALLBACK")
    min_segment_samples: int = Field(default=64, alias="FXSTACK_MIN_SEGMENT_SAMPLES")
    uncertainty_threshold: float = Field(default=0.25, alias="FXSTACK_UNCERTAINTY_THRESHOLD")
    use_uncertainty_gate: bool = Field(default=True, alias="FXSTACK_USE_UNCERTAINTY_GATE")
    max_entry_uncertainty: float = Field(default=0.25, alias="FXSTACK_MAX_ENTRY_UNCERTAINTY")
    blocked_entry_sessions_csv: str = Field(default="pacific", alias="FXSTACK_BLOCKED_ENTRY_SESSIONS")
    use_portfolio_ranking: bool = Field(default=True, alias="FXSTACK_USE_PORTFOLIO_RANKING")
    max_new_entries_per_cycle: int = Field(default=0, alias="FXSTACK_MAX_NEW_ENTRIES_PER_CYCLE")
    use_deep_model_shadow: bool = Field(default=False, alias="FXSTACK_USE_DEEP_MODEL_SHADOW")
    shadow_policy_enabled: bool = Field(default=True, alias="FXSTACK_SHADOW_POLICY_ENABLED")
    use_structure_timing_shadow: bool = Field(default=True, alias="FXSTACK_USE_STRUCTURE_TIMING_SHADOW")
    structure_timing_rescue_min_score: float = Field(default=0.66, alias="FXSTACK_STRUCTURE_TIMING_RESCUE_MIN_SCORE")
    structure_timing_entry_rescue_margin: float = Field(default=0.05, alias="FXSTACK_STRUCTURE_TIMING_ENTRY_RESCUE_MARGIN")
    structure_timing_max_chase_risk: float = Field(default=0.78, alias="FXSTACK_STRUCTURE_TIMING_MAX_CHASE_RISK")
    entry_hysteresis_margin_bps: float = Field(default=1.0, alias="FXSTACK_ENTRY_HYSTERESIS_MARGIN_BPS")
    reversal_hysteresis_margin_bps: float = Field(default=1.0, alias="FXSTACK_REVERSAL_HYSTERESIS_MARGIN_BPS")
    enable_pair_quality_prior: bool = Field(default=False, alias="FXSTACK_ENABLE_PAIR_QUALITY_PRIOR")
    throughput_floor: float = Field(default=0.08, alias="FXSTACK_THROUGHPUT_FLOOR")
    promotion_policy: str = Field(default="balanced", alias="FXSTACK_PROMOTION_POLICY")
    promotion_min_cv_score: float = Field(default=0.53, alias="FXSTACK_PROMOTION_MIN_CV_SCORE")
    promotion_min_wf_score: float = Field(default=0.51, alias="FXSTACK_PROMOTION_MIN_WF_SCORE")
    promotion_max_calibration_error: float = Field(default=0.20, alias="FXSTACK_PROMOTION_MAX_CALIBRATION_ERROR")
    promotion_min_delta: float = Field(default=0.005, alias="FXSTACK_PROMOTION_MIN_DELTA")
    wf_train_months: int = Field(default=6, alias="FXSTACK_WF_TRAIN_MONTHS")
    wf_test_months: int = Field(default=1, alias="FXSTACK_WF_TEST_MONTHS")
    wf_step_months: int = Field(default=1, alias="FXSTACK_WF_STEP_MONTHS")
    cv_splits: int = Field(default=5, alias="FXSTACK_CV_SPLITS")
    cv_embargo_pct: float = Field(default=0.02, alias="FXSTACK_CV_EMBARGO_PCT")

    project_root: Path = Path(__file__).resolve().parents[2]

    @property
    def pairs(self) -> list[str]:
        return self._csv_symbols(self.pairs_csv)

    @staticmethod
    def _csv_symbols(value: str) -> list[str]:
        out: list[str] = []
        for raw in str(value).split(","):
            sym = raw.strip().upper()
            if sym:
                out.append(sym)
        return out

    @property
    def tier1_pairs(self) -> list[str]:
        return self._csv_symbols(self.tier1_pairs_csv)

    @property
    def tier2_pairs(self) -> list[str]:
        tier1 = set(self.tier1_pairs)
        return [pair for pair in self.pairs if pair not in tier1]

    def pair_tier(self, pair: str) -> str:
        return "tier1" if str(pair).upper().strip() in set(self.tier1_pairs) else "tier2"

    @property
    def blocked_entry_sessions(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.blocked_entry_sessions_csv).split(","):
            item = str(raw).strip().lower()
            if item:
                out.append(item)
        return out

    @property
    def is_sqlite_url(self) -> bool:
        txt = str(self.database_url).strip().lower()
        return txt.startswith("sqlite")

    @property
    def normalized_data_provider(self) -> str:
        txt = str(self.data_provider).strip().lower()
        return txt if txt else "dukascopy"

    @property
    def runtime_state_stale_keys(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.runtime_state_stale_keys_csv).split(","):
            key = str(raw).strip()
            if key:
                out.append(key)
        return out

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "data_provider": self.normalized_data_provider,
            "dukascopy_source_root": self.dukascopy_source_root,
            "dukascopy_file_pattern": self.dukascopy_file_pattern,
            "database_url": self.database_url,
            "mt4_bridge_url": self.mt4_bridge_url,
            "bridge_stale_heartbeat_secs": float(self.bridge_stale_heartbeat_secs),
            "bridge_stale_tick_secs": float(self.bridge_stale_tick_secs),
            "pairs": self.pairs,
            "start_profile": self.start_profile,
            "run_fast_gate": bool(self.run_fast_gate),
            "run_shadow_24h": bool(self.run_shadow_24h),
            "allow_sqlite": bool(self.allow_sqlite),
            "require_active_models": bool(self.require_active_models),
            "intraday_timeframe": self.intraday_timeframe,
            "swing_timeframe": self.swing_timeframe,
            "regime_timeframe": self.regime_timeframe,
            "max_pair_positions": int(self.max_pair_positions),
            "max_total_positions": int(self.max_total_positions),
            "default_order_lots": float(self.default_order_lots),
            "equity_lots_per_usd": float(self.equity_lots_per_usd),
            "min_order_lots": float(self.min_order_lots),
            "order_lot_step": float(self.order_lot_step),
            "max_order_lots": float(self.max_order_lots),
            "min_swing_prob": float(self.min_swing_prob),
            "min_entry_prob": float(self.min_entry_prob),
            "min_trade_prob": float(self.min_trade_prob),
            "max_allowed_spread_bps": float(self.max_allowed_spread_bps),
            "min_expected_edge_bps": float(self.min_expected_edge_bps),
            "policy_version": self.policy_version,
            "frame_profile": self.frame_profile,
            "swing_primary_timeframe": self.swing_primary_timeframe,
            "enable_lifecycle_actions": bool(self.enable_lifecycle_actions),
            "enable_adjust_actions": bool(self.enable_adjust_actions),
            "hard_time_stop_secs": float(self.hard_time_stop_secs),
            "adjust_stop_buffer_pips": float(self.adjust_stop_buffer_pips),
            "partial_close_fraction": float(self.partial_close_fraction),
            "partial_close_cooldown_secs": float(self.partial_close_cooldown_secs),
            "max_partial_closes_per_position": int(self.max_partial_closes_per_position),
            "lifecycle_model_action_min_prob": float(self.lifecycle_model_action_min_prob),
            "reversal_failure_min_prob": float(self.reversal_failure_min_prob),
            "reversal_opportunity_min_prob": float(self.reversal_opportunity_min_prob),
            "runtime_state_prune_stale_keys": bool(self.runtime_state_prune_stale_keys),
            "registry_root": self.registry_root,
            "model_activation_manifest": self.model_activation_manifest,
            "runtime_allow_create_all": bool(self.runtime_allow_create_all),
            "require_cuda": bool(self.require_cuda),
            "strict_activation": bool(self.strict_activation),
            "require_lifecycle_artifacts": bool(self.require_lifecycle_artifacts),
            "require_hierarchical_intraday_contract": bool(self.require_hierarchical_intraday_contract),
            "allow_heuristic_meta_labels": bool(self.allow_heuristic_meta_labels),
            "strict_command_validation": bool(self.strict_command_validation),
            "deep_model_stale_hours": float(self.deep_model_stale_hours),
            "tier1_pairs": self.tier1_pairs,
            "tier2_pairs": self.tier2_pairs,
            "intraday_retrain_min_new_rows": int(self.intraday_retrain_min_new_rows),
            "meta_retrain_min_new_rows": int(self.meta_retrain_min_new_rows),
            "lifecycle_retrain_min_new_events": int(self.lifecycle_retrain_min_new_events),
            "deep_retrain_max_age_hours": float(self.deep_retrain_max_age_hours),
            "deep_retrain_min_new_rows": int(self.deep_retrain_min_new_rows),
            "force_weekly_retrain_day": str(self.force_weekly_retrain_day),
            "weekly_full_retrain_time": str(self.weekly_full_retrain_time),
            "weekly_auto_activate": bool(self.weekly_auto_activate),
            "drift_trigger_ece": float(self.drift_trigger_ece),
            "drift_trigger_throughput_drop": float(self.drift_trigger_throughput_drop),
            "live_spread_reject_rate_trigger": float(self.live_spread_reject_rate_trigger),
            "model_load_timeout_secs": float(self.model_load_timeout_secs),
            "runtime_startup_progress_stale_secs": float(self.runtime_startup_progress_stale_secs),
            "swing_model_policy": self.swing_model_policy,
            "intraday_model_policy": self.intraday_model_policy,
            "tcn_window_size": int(self.tcn_window_size),
            "transformer_window_size": int(self.transformer_window_size),
            "deep_train_epochs": int(self.deep_train_epochs),
            "deep_batch_size": int(self.deep_batch_size),
            "xgb_device": self.xgb_device,
            "xgb_tree_method": self.xgb_tree_method,
            "xgb_allow_cpu_fallback": bool(self.xgb_allow_cpu_fallback),
            "min_segment_samples": int(self.min_segment_samples),
            "uncertainty_threshold": float(self.uncertainty_threshold),
            "use_uncertainty_gate": bool(self.use_uncertainty_gate),
            "max_entry_uncertainty": float(self.max_entry_uncertainty),
            "blocked_entry_sessions": list(self.blocked_entry_sessions),
            "use_portfolio_ranking": bool(self.use_portfolio_ranking),
            "max_new_entries_per_cycle": int(self.max_new_entries_per_cycle),
            "use_deep_model_shadow": bool(self.use_deep_model_shadow),
            "shadow_policy_enabled": bool(self.shadow_policy_enabled),
            "use_structure_timing_shadow": bool(self.use_structure_timing_shadow),
            "structure_timing_rescue_min_score": float(self.structure_timing_rescue_min_score),
            "structure_timing_entry_rescue_margin": float(self.structure_timing_entry_rescue_margin),
            "structure_timing_max_chase_risk": float(self.structure_timing_max_chase_risk),
            "entry_hysteresis_margin_bps": float(self.entry_hysteresis_margin_bps),
            "reversal_hysteresis_margin_bps": float(self.reversal_hysteresis_margin_bps),
            "enable_pair_quality_prior": bool(self.enable_pair_quality_prior),
            "throughput_floor": float(self.throughput_floor),
            "promotion_policy": self.promotion_policy,
            "promotion_min_cv_score": float(self.promotion_min_cv_score),
            "promotion_min_wf_score": float(self.promotion_min_wf_score),
            "promotion_max_calibration_error": float(self.promotion_max_calibration_error),
            "promotion_min_delta": float(self.promotion_min_delta),
            "wf_train_months": int(self.wf_train_months),
            "wf_test_months": int(self.wf_test_months),
            "wf_step_months": int(self.wf_step_months),
            "cv_splits": int(self.cv_splits),
            "cv_embargo_pct": float(self.cv_embargo_pct),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
