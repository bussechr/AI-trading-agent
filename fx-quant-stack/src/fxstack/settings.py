# AGENT: ROLE: Typed env-backed settings contract shared by live runtime, bridge API, twin replay, and ops.
# AGENT: ENTRYPOINT: imported through `get_settings()`.
# AGENT: PRIMARY INPUTS: process env, `.env`, Windows `_env.bat` defaults.
# AGENT: PRIMARY OUTPUTS: cached `Settings` instance with thresholds, paths, caps, and feature flags.
# AGENT: DEPENDS ON: pydantic settings.
# AGENT: CALLED BY: runtime, live scorer/policy, API, twin, ops helpers.
# AGENT: STATE / SIDE EFFECTS: cached settings singleton only.
# AGENT: HANDSHAKES: env threshold contract between Windows ops bootstrap and Python processes.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` -> `ops/windows/_env.bat` -> `docs/agents/ops-entrypoints.md`
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    data_provider: str = Field(default="dukascopy", alias="FXSTACK_DATA_PROVIDER")
    history_provider: str = Field(default="", alias="FXSTACK_HISTORY_PROVIDER")
    market_data_provider: str = Field(default="mt4_bridge", alias="FXSTACK_MARKET_DATA_PROVIDER")
    execution_provider: str = Field(default="mt4", alias="FXSTACK_EXECUTION_PROVIDER")
    provider_shadow_only: bool = Field(default=False, alias="FXSTACK_PROVIDER_SHADOW_ONLY")
    provider_symbol_allowlist_csv: str = Field(
        default="BTCUSDT,ETHUSDT,SOLUSDT",
        alias="FXSTACK_PROVIDER_SYMBOL_ALLOWLIST",
    )
    crypto_exchange_id: str = Field(default="binance", alias="FXSTACK_CRYPTO_EXCHANGE_ID")
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
    max_allowed_spread_bps: float = Field(default=3.0, alias="FXSTACK_MAX_ALLOWED_SPREAD_BPS")
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
    weekly_full_retrain_time: str = Field(default="01:00", alias="FXSTACK_WEEKLY_FULL_RETRAIN_TIME")
    weekly_auto_activate: bool = Field(default=True, alias="FXSTACK_WEEKLY_AUTO_ACTIVATE")
    drift_trigger_ece: float = Field(default=0.20, alias="FXSTACK_DRIFT_TRIGGER_ECE")
    drift_trigger_throughput_drop: float = Field(default=0.08, alias="FXSTACK_DRIFT_TRIGGER_THROUGHPUT_DROP")
    live_spread_reject_rate_trigger: float = Field(default=0.25, alias="FXSTACK_LIVE_SPREAD_REJECT_RATE_TRIGGER")
    model_load_timeout_secs: float = Field(default=12.0, alias="FXSTACK_MODEL_LOAD_TIMEOUT_SECS")
    min_expected_edge_rescue_margin_bps: float = Field(
        default=0.5,
        alias="FXSTACK_MIN_EXPECTED_EDGE_RESCUE_MARGIN_BPS",
    )
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
    patchtst_patch_length: int = Field(default=12, alias="FXSTACK_PATCHTST_PATCH_LENGTH")
    patchtst_stride: int = Field(default=6, alias="FXSTACK_PATCHTST_STRIDE")
    patchtst_d_model: int = Field(default=64, alias="FXSTACK_PATCHTST_D_MODEL")
    patchtst_num_layers: int = Field(default=2, alias="FXSTACK_PATCHTST_NUM_LAYERS")
    patchtst_num_heads: int = Field(default=4, alias="FXSTACK_PATCHTST_NUM_HEADS")
    patchtst_dropout: float = Field(default=0.1, alias="FXSTACK_PATCHTST_DROPOUT")
    deep_train_epochs: int = Field(default=5, alias="FXSTACK_DEEP_TRAIN_EPOCHS")
    deep_batch_size: int = Field(default=64, alias="FXSTACK_DEEP_BATCH_SIZE")
    sequence_dataset_cache_root: str = Field(
        default="fx-quant-stack/artifacts/sequence_cache",
        alias="FXSTACK_SEQUENCE_DATASET_CACHE_ROOT",
    )
    xgb_device: str = Field(default="auto", alias="FXSTACK_XGB_DEVICE")
    xgb_tree_method: str = Field(default="hist", alias="FXSTACK_XGB_TREE_METHOD")
    xgb_allow_cpu_fallback: bool = Field(default=True, alias="FXSTACK_XGB_ALLOW_CPU_FALLBACK")
    min_segment_samples: int = Field(default=64, alias="FXSTACK_MIN_SEGMENT_SAMPLES")
    uncertainty_threshold: float = Field(default=0.25, alias="FXSTACK_UNCERTAINTY_THRESHOLD")
    use_uncertainty_gate: bool = Field(default=True, alias="FXSTACK_USE_UNCERTAINTY_GATE")
    max_entry_uncertainty: float = Field(default=0.25, alias="FXSTACK_MAX_ENTRY_UNCERTAINTY")
    adaptive_playbook_threshold_slack: float = Field(
        default=0.03,
        alias="FXSTACK_ADAPTIVE_PLAYBOOK_THRESHOLD_SLACK",
    )
    blocked_entry_sessions_csv: str = Field(default="pacific", alias="FXSTACK_BLOCKED_ENTRY_SESSIONS")
    use_portfolio_ranking: bool = Field(default=True, alias="FXSTACK_USE_PORTFOLIO_RANKING")
    strategy_engine_mode: str = Field(default="supervised_legacy", alias="FXSTACK_STRATEGY_ENGINE_MODE")
    portfolio_corr_mode: str = Field(default="heuristic", alias="FXSTACK_PORTFOLIO_CORR_MODE")
    belief_influence_mode: str = Field(default="off", alias="FXSTACK_BELIEF_INFLUENCE_MODE")
    challenger_conflict_mode: str = Field(default="off", alias="FXSTACK_CHALLENGER_CONFLICT_MODE")
    rl_supervised_fallback_required: bool = Field(default=True, alias="FXSTACK_RL_SUPERVISED_FALLBACK_REQUIRED")
    intraday_tcn_fallback_live_allowed: bool = Field(default=False, alias="FXSTACK_INTRADAY_TCN_FALLBACK_LIVE_ALLOWED")
    portfolio_realized_corr_window_bars: int = Field(default=96, alias="FXSTACK_PORTFOLIO_REALIZED_CORR_WINDOW_BARS")
    portfolio_realized_corr_min_obs: int = Field(default=24, alias="FXSTACK_PORTFOLIO_REALIZED_CORR_MIN_OBS")
    portfolio_realized_corr_max_age_secs: float = Field(default=21600.0, alias="FXSTACK_PORTFOLIO_REALIZED_CORR_MAX_AGE_SECS")
    max_new_entries_per_cycle: int = Field(default=0, alias="FXSTACK_MAX_NEW_ENTRIES_PER_CYCLE")
    use_deep_model_shadow: bool = Field(default=False, alias="FXSTACK_USE_DEEP_MODEL_SHADOW")
    sequence_shadow_enabled: bool = Field(default=False, alias="FXSTACK_SEQUENCE_SHADOW_ENABLED")
    shadow_policy_enabled: bool = Field(default=True, alias="FXSTACK_SHADOW_POLICY_ENABLED")
    adaptive_shadow_enabled: bool = Field(default=True, alias="FXSTACK_ADAPTIVE_SHADOW_ENABLED")
    adaptive_shadow_history_bars: int = Field(default=128, alias="FXSTACK_ADAPTIVE_SHADOW_HISTORY_BARS")
    adaptive_shadow_playbooks_csv: str = Field(
        default="trend_pullback,range_mean_reversion,breakout_expansion,failed_breakout_reversal",
        alias="FXSTACK_ADAPTIVE_SHADOW_PLAYBOOKS",
    )
    adaptive_execution_enabled: bool = Field(default=False, alias="FXSTACK_ADAPTIVE_EXECUTION_ENABLED")
    belief_shadow_enabled: bool = Field(default=False, alias="FXSTACK_BELIEF_SHADOW_ENABLED")
    belief_runtime_required: bool = Field(default=False, alias="FXSTACK_BELIEF_RUNTIME_REQUIRED")
    belief_short_horizon_bars: int = Field(default=3, alias="FXSTACK_BELIEF_SHORT_HORIZON_BARS")
    belief_trade_horizon_bars: int = Field(default=12, alias="FXSTACK_BELIEF_TRADE_HORIZON_BARS")
    belief_structural_horizon_bars: int = Field(default=48, alias="FXSTACK_BELIEF_STRUCTURAL_HORIZON_BARS")
    campaign_manager_enabled: bool = Field(default=False, alias="FXSTACK_CAMPAIGN_MANAGER_ENABLED")
    campaign_shadow_only: bool = Field(default=True, alias="FXSTACK_CAMPAIGN_SHADOW_ONLY")
    campaign_abandon_cooldown_bars: int = Field(default=8, alias="FXSTACK_CAMPAIGN_ABANDON_COOLDOWN_BARS")
    campaign_press_protected_bars: int = Field(default=4, alias="FXSTACK_CAMPAIGN_PRESS_PROTECTED_BARS")
    campaign_reattack_cooldown_scale: float = Field(default=0.5, alias="FXSTACK_CAMPAIGN_REATTACK_COOLDOWN_SCALE")
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
    mlflow_enabled: bool = Field(default=False, alias="FXSTACK_MLFLOW_ENABLED")
    mlflow_tracking_uri: str = Field(default="http://127.0.0.1:5000", alias="FXSTACK_MLFLOW_TRACKING_URI")
    mlflow_registry_uri: str = Field(default="", alias="FXSTACK_MLFLOW_REGISTRY_URI")
    mlflow_cache_root: str = Field(
        default="fx-quant-stack/artifacts/mlflow_cache",
        alias="FXSTACK_MLFLOW_CACHE_ROOT",
    )
    feast_enabled: bool = Field(default=False, alias="FXSTACK_FEAST_ENABLED")
    feast_repo_root: str = Field(default="fx-quant-stack/feature_repo", alias="FXSTACK_FEAST_REPO_ROOT")
    feast_online_latency_budget_ms: float = Field(default=50.0, alias="FXSTACK_FEAST_ONLINE_LATENCY_BUDGET_MS")
    feast_online_stale_secs: float = Field(default=600.0, alias="FXSTACK_FEAST_ONLINE_STALE_SECS")
    feature_push_enabled: bool = Field(default=False, alias="FXSTACK_FEATURE_PUSH_ENABLED")
    feature_push_worker_id: str = Field(default="feature-push-worker", alias="FXSTACK_FEATURE_PUSH_WORKER_ID")
    feature_push_batch_size: int = Field(default=50, alias="FXSTACK_FEATURE_PUSH_BATCH_SIZE")
    feature_push_max_retries: int = Field(default=5, alias="FXSTACK_FEATURE_PUSH_MAX_RETRIES")
    feature_push_claim_timeout_secs: float = Field(default=120.0, alias="FXSTACK_FEATURE_PUSH_CLAIM_TIMEOUT_SECS")
    feature_push_backlog_warn: int = Field(default=250, alias="FXSTACK_FEATURE_PUSH_BACKLOG_WARN")
    feature_parity_tolerance: float = Field(default=1e-6, alias="FXSTACK_FEATURE_PARITY_TOLERANCE")
    risk_max_drawdown_pct: float = Field(default=0.0, alias="FXSTACK_RISK_MAX_DRAWDOWN_PCT")
    risk_max_gross_exposure: float = Field(default=0.0, alias="FXSTACK_RISK_MAX_GROSS_EXPOSURE")
    risk_max_net_exposure: float = Field(default=0.0, alias="FXSTACK_RISK_MAX_NET_EXPOSURE")
    phase5_release_root: str = Field(default="fx-quant-stack/artifacts/releases", alias="FXSTACK_PHASE5_RELEASE_ROOT")
    phase5_observation_window_minutes: int = Field(
        default=60,
        alias="FXSTACK_PHASE5_OBSERVATION_WINDOW_MINUTES",
    )
    phase5_canary_budget_scale: float = Field(default=0.25, alias="FXSTACK_PHASE5_CANARY_BUDGET_SCALE")
    phase5_canary_latency_budget_ms: float = Field(
        default=5000.0,
        alias="FXSTACK_PHASE5_CANARY_LATENCY_BUDGET_MS",
    )
    phase5_canary_stale_feature_limit: int = Field(
        default=1,
        alias="FXSTACK_PHASE5_CANARY_STALE_FEATURE_LIMIT",
    )
    phase5_canary_drawdown_limit_pct: float = Field(
        default=5.0,
        alias="FXSTACK_PHASE5_CANARY_DRAWDOWN_LIMIT_PCT",
    )
    phase5_canary_calibration_drift_limit: float = Field(
        default=0.05,
        alias="FXSTACK_PHASE5_CANARY_CALIBRATION_DRIFT_LIMIT",
    )
    phase5_auto_rollback: bool = Field(default=True, alias="FXSTACK_PHASE5_AUTO_ROLLBACK")
    capital_band_mode: str = Field(default="paper", alias="FXSTACK_CAPITAL_BAND_MODE")
    capital_entries_only: bool = Field(default=False, alias="FXSTACK_CAPITAL_ENTRIES_ONLY")
    capital_governance_enabled: bool = Field(default=False, alias="FXSTACK_CAPITAL_GOVERNANCE_ENABLED")
    capital_max_drawdown_micro_live_pct: float = Field(default=3.0, alias="FXSTACK_CAPITAL_MAX_DRAWDOWN_MICRO_LIVE_PCT")
    capital_max_drawdown_low_risk_pct: float = Field(default=5.0, alias="FXSTACK_CAPITAL_MAX_DRAWDOWN_LOW_RISK_PCT")
    capital_max_drawdown_full_risk_pct: float = Field(default=8.0, alias="FXSTACK_CAPITAL_MAX_DRAWDOWN_FULL_RISK_PCT")
    capital_max_tail_loss_pct: float = Field(default=2.5, alias="FXSTACK_CAPITAL_MAX_TAIL_LOSS_PCT")
    capital_max_latency_breach_count: int = Field(default=0, alias="FXSTACK_CAPITAL_MAX_LATENCY_BREACH_COUNT")
    capital_max_stale_feature_count: int = Field(default=0, alias="FXSTACK_CAPITAL_MAX_STALE_FEATURE_COUNT")
    capital_max_calibration_drift: float = Field(default=0.05, alias="FXSTACK_CAPITAL_MAX_CALIBRATION_DRIFT")
    capital_max_operational_fault_count: int = Field(default=0, alias="FXSTACK_CAPITAL_MAX_OPERATIONAL_FAULT_COUNT")
    capital_max_concentration_share: float = Field(default=0.6, alias="FXSTACK_CAPITAL_MAX_CONCENTRATION_SHARE")
    capital_max_realized_corr_share: float = Field(default=0.75, alias="FXSTACK_CAPITAL_MAX_REALIZED_CORR_SHARE")
    capital_min_shadow_alignment_share: float = Field(default=0.7, alias="FXSTACK_CAPITAL_MIN_SHADOW_ALIGNMENT_SHARE")
    capital_rollout_budget_scale_micro_live: float = Field(default=0.1, alias="FXSTACK_CAPITAL_BUDGET_SCALE_MICRO_LIVE")
    capital_rollout_budget_scale_low_risk: float = Field(default=0.25, alias="FXSTACK_CAPITAL_BUDGET_SCALE_LOW_RISK")
    capital_rollout_budget_scale_full_risk: float = Field(default=1.0, alias="FXSTACK_CAPITAL_BUDGET_SCALE_FULL_RISK")
    rl_artifact_root: str = Field(default="fx-quant-stack/artifacts/rl", alias="FXSTACK_RL_ARTIFACT_ROOT")
    rl_transition_dataset_root: str = Field(
        default="fx-quant-stack/artifacts/rl/datasets",
        alias="FXSTACK_RL_TRANSITION_DATASET_ROOT",
    )
    rl_online_worker_count: int = Field(default=4, alias="FXSTACK_RL_ONLINE_WORKER_COUNT")
    rl_stress_root: str = Field(default="fx-quant-stack/artifacts/rl/stress", alias="FXSTACK_RL_STRESS_ROOT")
    agent_mode: str = Field(default="off", alias="FXSTACK_AGENT_MODE")
    agent_runtime: str = Field(default="langgraph", alias="FXSTACK_AGENT_RUNTIME")
    agent_durability: str = Field(default="async", alias="FXSTACK_AGENT_DURABILITY")
    agent_decision_timeout_ms: int = Field(default=250, alias="FXSTACK_AGENT_DECISION_TIMEOUT_MS")
    agent_max_node_ms: int = Field(default=50, alias="FXSTACK_AGENT_MAX_NODE_MS")
    agent_max_parallel_proposals: int = Field(default=8, alias="FXSTACK_AGENT_MAX_PARALLEL_PROPOSALS")
    agent_shadow_pair_allowlist_csv: str = Field(default="", alias="FXSTACK_AGENT_SHADOW_PAIR_ALLOWLIST")
    agent_paper_pair_allowlist_csv: str = Field(default="", alias="FXSTACK_AGENT_PAPER_PAIR_ALLOWLIST")
    agent_paper_sleeve_allowlist_csv: str = Field(default="", alias="FXSTACK_AGENT_PAPER_SLEEVE_ALLOWLIST")
    agent_paper_intent_allowlist_csv: str = Field(default="enter", alias="FXSTACK_AGENT_PAPER_INTENT_ALLOWLIST")
    agent_live_pair_allowlist_csv: str = Field(default="", alias="FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST")
    agent_live_sleeve_allowlist_csv: str = Field(default="", alias="FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST")
    agent_live_intent_allowlist_csv: str = Field(default="enter", alias="FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST")
    agent_allow_remote_llm: bool = Field(default=False, alias="FXSTACK_AGENT_ALLOW_REMOTE_LLM")
    agent_allow_external_tools: bool = Field(default=False, alias="FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS")
    agent_require_human_approval: bool = Field(default=True, alias="FXSTACK_AGENT_REQUIRE_HUMAN_APPROVAL")
    agent_trace_retention_days: int = Field(default=90, alias="FXSTACK_AGENT_TRACE_RETENTION_DAYS")
    agent_enable_otel: bool = Field(default=True, alias="FXSTACK_AGENT_ENABLE_OTEL")
    agent_otel_exporter: str = Field(default="otlp", alias="FXSTACK_AGENT_OTEL_EXPORTER")
    phase6b_canary_p95_overhead_ms: float = Field(default=250.0, alias="FXSTACK_PHASE6B_CANARY_P95_OVERHEAD_MS")
    phase6b_canary_p99_overhead_ms: float = Field(default=500.0, alias="FXSTACK_PHASE6B_CANARY_P99_OVERHEAD_MS")
    phase6b_canary_ack_success_floor: float = Field(default=0.995, alias="FXSTACK_PHASE6B_CANARY_ACK_SUCCESS_FLOOR")
    phase6b_canary_orphan_command_limit: int = Field(default=0, alias="FXSTACK_PHASE6B_CANARY_ORPHAN_COMMAND_LIMIT")
    phase6b_canary_entry_ratio_floor: float = Field(default=0.90, alias="FXSTACK_PHASE6B_CANARY_ENTRY_RATIO_FLOOR")
    phase6b_canary_slot_utilisation_floor: float = Field(default=0.90, alias="FXSTACK_PHASE6B_CANARY_SLOT_UTILISATION_FLOOR")
    phase6b_canary_drawdown_deterioration_pct: float = Field(
        default=-1.0,
        alias="FXSTACK_PHASE6B_CANARY_DRAWDOWN_DETERIORATION_PCT",
    )
    phase6b_canary_ramp_steps_pct_csv: str = Field(default="1,5,10", alias="FXSTACK_PHASE6B_CANARY_RAMP_STEPS_PCT")
    phase6b_canary_alert_window_minutes: int = Field(default=15, alias="FXSTACK_PHASE6B_CANARY_ALERT_WINDOW_MINUTES")
    mcp_enabled: bool = Field(default=False, alias="FXSTACK_MCP_ENABLED")
    mcp_transport: str = Field(default="stdio", alias="FXSTACK_MCP_TRANSPORT")
    openclaw_enabled: bool = Field(default=False, alias="FXSTACK_OPENCLAW_ENABLED")
    openclaw_scopes: str = Field(default="operator.read", alias="FXSTACK_OPENCLAW_SCOPES")
    openclaw_sandbox_required: bool = Field(default=True, alias="FXSTACK_OPENCLAW_SANDBOX_REQUIRED")
    model_bundle_version: str = Field(default="", alias="FXSTACK_MODEL_BUNDLE_VERSION")
    model_manifest_path: str = Field(default="", alias="FXSTACK_MODEL_MANIFEST_PATH")

    project_root: Path = Path(__file__).resolve().parents[2]

    @property
    def pairs(self) -> list[str]:
        return self._csv_symbols(self.pairs_csv)

    @property
    def agent_shadow_pair_allowlist(self) -> list[str]:
        return self._csv_symbols(self.agent_shadow_pair_allowlist_csv)

    @property
    def agent_paper_pair_allowlist(self) -> list[str]:
        return self._csv_symbols(self.agent_paper_pair_allowlist_csv)

    @property
    def agent_paper_sleeve_allowlist(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.agent_paper_sleeve_allowlist_csv).split(","):
            item = str(raw).strip().lower()
            if item:
                out.append(item)
        return out

    @property
    def agent_paper_intent_allowlist(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.agent_paper_intent_allowlist_csv).split(","):
            item = str(raw).strip().lower()
            if item:
                out.append(item)
        return out or ["enter"]

    @property
    def agent_live_pair_allowlist(self) -> list[str]:
        return self._csv_symbols(self.agent_live_pair_allowlist_csv)

    @property
    def agent_live_sleeve_allowlist(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.agent_live_sleeve_allowlist_csv).split(","):
            item = str(raw).strip().lower()
            if item:
                out.append(item)
        return out

    @property
    def agent_live_intent_allowlist(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.agent_live_intent_allowlist_csv).split(","):
            item = str(raw).strip().lower()
            if item:
                out.append(item)
        return out or ["enter"]

    @property
    def phase6b_canary_ramp_steps_pct(self) -> list[int]:
        out: list[int] = []
        for raw in str(self.phase6b_canary_ramp_steps_pct_csv).split(","):
            item = str(raw).strip()
            if not item:
                continue
            try:
                value = int(float(item))
            except Exception:
                continue
            if value > 0:
                out.append(value)
        return out or [1, 5, 10]

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
            if item in {"", "none", "off", "disabled", "false", "0"}:
                continue
            if item:
                out.append(item)
        return out

    @property
    def adaptive_shadow_playbooks(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.adaptive_shadow_playbooks_csv).split(","):
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
    def normalized_history_provider(self) -> str:
        txt = str(self.history_provider).strip().lower()
        return txt if txt else self.normalized_data_provider

    @property
    def normalized_market_data_provider(self) -> str:
        txt = str(self.market_data_provider).strip().lower()
        return txt if txt else "mt4_bridge"

    @property
    def normalized_execution_provider(self) -> str:
        if str(self.agent_mode or "").strip().lower() == "paper":
            return "paper"
        txt = str(self.execution_provider).strip().lower()
        return txt if txt else "mt4"

    @property
    def provider_symbol_allowlist(self) -> list[str]:
        return self._csv_symbols(self.provider_symbol_allowlist_csv)

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
            "history_provider": self.normalized_history_provider,
            "market_data_provider": self.normalized_market_data_provider,
            "execution_provider": self.normalized_execution_provider,
            "provider_shadow_only": bool(self.provider_shadow_only),
            "provider_symbol_allowlist": list(self.provider_symbol_allowlist),
            "crypto_exchange_id": str(self.crypto_exchange_id),
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
            "min_expected_edge_rescue_margin_bps": float(self.min_expected_edge_rescue_margin_bps),
            "runtime_startup_progress_stale_secs": float(self.runtime_startup_progress_stale_secs),
            "swing_model_policy": self.swing_model_policy,
            "intraday_model_policy": self.intraday_model_policy,
            "tcn_window_size": int(self.tcn_window_size),
            "transformer_window_size": int(self.transformer_window_size),
            "patchtst_patch_length": int(self.patchtst_patch_length),
            "patchtst_stride": int(self.patchtst_stride),
            "patchtst_d_model": int(self.patchtst_d_model),
            "patchtst_num_layers": int(self.patchtst_num_layers),
            "patchtst_num_heads": int(self.patchtst_num_heads),
            "patchtst_dropout": float(self.patchtst_dropout),
            "deep_train_epochs": int(self.deep_train_epochs),
            "deep_batch_size": int(self.deep_batch_size),
            "sequence_dataset_cache_root": str(self.sequence_dataset_cache_root),
            "xgb_device": self.xgb_device,
            "xgb_tree_method": self.xgb_tree_method,
            "xgb_allow_cpu_fallback": bool(self.xgb_allow_cpu_fallback),
            "min_segment_samples": int(self.min_segment_samples),
            "uncertainty_threshold": float(self.uncertainty_threshold),
            "use_uncertainty_gate": bool(self.use_uncertainty_gate),
            "max_entry_uncertainty": float(self.max_entry_uncertainty),
            "adaptive_playbook_threshold_slack": float(self.adaptive_playbook_threshold_slack),
            "blocked_entry_sessions": list(self.blocked_entry_sessions),
            "use_portfolio_ranking": bool(self.use_portfolio_ranking),
            "strategy_engine_mode": str(self.strategy_engine_mode),
            "portfolio_corr_mode": str(self.portfolio_corr_mode),
            "belief_influence_mode": str(self.belief_influence_mode),
            "challenger_conflict_mode": str(self.challenger_conflict_mode),
            "rl_supervised_fallback_required": bool(self.rl_supervised_fallback_required),
            "intraday_tcn_fallback_live_allowed": bool(self.intraday_tcn_fallback_live_allowed),
            "portfolio_realized_corr_window_bars": int(self.portfolio_realized_corr_window_bars),
            "portfolio_realized_corr_min_obs": int(self.portfolio_realized_corr_min_obs),
            "portfolio_realized_corr_max_age_secs": float(self.portfolio_realized_corr_max_age_secs),
            "max_new_entries_per_cycle": int(self.max_new_entries_per_cycle),
            "use_deep_model_shadow": bool(self.use_deep_model_shadow),
            "sequence_shadow_enabled": bool(self.sequence_shadow_enabled),
            "shadow_policy_enabled": bool(self.shadow_policy_enabled),
            "adaptive_shadow_enabled": bool(self.adaptive_shadow_enabled),
            "adaptive_shadow_history_bars": int(self.adaptive_shadow_history_bars),
            "adaptive_shadow_playbooks": list(self.adaptive_shadow_playbooks),
            "adaptive_execution_enabled": bool(self.adaptive_execution_enabled),
            "belief_shadow_enabled": bool(self.belief_shadow_enabled),
            "belief_runtime_required": bool(self.belief_runtime_required),
            "belief_short_horizon_bars": int(self.belief_short_horizon_bars),
            "belief_trade_horizon_bars": int(self.belief_trade_horizon_bars),
            "belief_structural_horizon_bars": int(self.belief_structural_horizon_bars),
            "campaign_manager_enabled": bool(self.campaign_manager_enabled),
            "campaign_shadow_only": bool(self.campaign_shadow_only),
            "campaign_abandon_cooldown_bars": int(self.campaign_abandon_cooldown_bars),
            "campaign_press_protected_bars": int(self.campaign_press_protected_bars),
            "campaign_reattack_cooldown_scale": float(self.campaign_reattack_cooldown_scale),
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
            "mlflow_enabled": bool(self.mlflow_enabled),
            "mlflow_tracking_uri": str(self.mlflow_tracking_uri),
            "mlflow_registry_uri": str(self.mlflow_registry_uri or self.mlflow_tracking_uri),
            "mlflow_cache_root": str(self.mlflow_cache_root),
            "feast_enabled": bool(self.feast_enabled),
            "feast_repo_root": str(self.feast_repo_root),
            "feast_online_latency_budget_ms": float(self.feast_online_latency_budget_ms),
            "feast_online_stale_secs": float(self.feast_online_stale_secs),
            "feature_push_enabled": bool(self.feature_push_enabled),
            "feature_push_worker_id": str(self.feature_push_worker_id),
            "feature_push_batch_size": int(self.feature_push_batch_size),
            "feature_push_max_retries": int(self.feature_push_max_retries),
            "feature_push_claim_timeout_secs": float(self.feature_push_claim_timeout_secs),
            "feature_push_backlog_warn": int(self.feature_push_backlog_warn),
            "feature_parity_tolerance": float(self.feature_parity_tolerance),
            "risk_max_drawdown_pct": float(self.risk_max_drawdown_pct),
            "risk_max_gross_exposure": float(self.risk_max_gross_exposure),
            "risk_max_net_exposure": float(self.risk_max_net_exposure),
            "phase5_release_root": str(self.phase5_release_root),
            "phase5_observation_window_minutes": int(self.phase5_observation_window_minutes),
            "phase5_canary_budget_scale": float(self.phase5_canary_budget_scale),
            "phase5_canary_latency_budget_ms": float(self.phase5_canary_latency_budget_ms),
            "phase5_canary_stale_feature_limit": int(self.phase5_canary_stale_feature_limit),
            "phase5_canary_drawdown_limit_pct": float(self.phase5_canary_drawdown_limit_pct),
            "phase5_canary_calibration_drift_limit": float(self.phase5_canary_calibration_drift_limit),
            "phase5_auto_rollback": bool(self.phase5_auto_rollback),
            "capital_band_mode": str(self.capital_band_mode),
            "capital_entries_only": bool(self.capital_entries_only),
            "capital_governance_enabled": bool(self.capital_governance_enabled),
            "capital_max_drawdown_micro_live_pct": float(self.capital_max_drawdown_micro_live_pct),
            "capital_max_drawdown_low_risk_pct": float(self.capital_max_drawdown_low_risk_pct),
            "capital_max_drawdown_full_risk_pct": float(self.capital_max_drawdown_full_risk_pct),
            "capital_max_tail_loss_pct": float(self.capital_max_tail_loss_pct),
            "capital_max_latency_breach_count": int(self.capital_max_latency_breach_count),
            "capital_max_stale_feature_count": int(self.capital_max_stale_feature_count),
            "capital_max_calibration_drift": float(self.capital_max_calibration_drift),
            "capital_max_operational_fault_count": int(self.capital_max_operational_fault_count),
            "capital_max_concentration_share": float(self.capital_max_concentration_share),
            "capital_max_realized_corr_share": float(self.capital_max_realized_corr_share),
            "capital_min_shadow_alignment_share": float(self.capital_min_shadow_alignment_share),
            "capital_rollout_budget_scale_micro_live": float(self.capital_rollout_budget_scale_micro_live),
            "capital_rollout_budget_scale_low_risk": float(self.capital_rollout_budget_scale_low_risk),
            "capital_rollout_budget_scale_full_risk": float(self.capital_rollout_budget_scale_full_risk),
            "rl_artifact_root": str(self.rl_artifact_root),
            "rl_transition_dataset_root": str(self.rl_transition_dataset_root),
            "rl_online_worker_count": int(self.rl_online_worker_count),
            "rl_stress_root": str(self.rl_stress_root),
            "agent_mode": str(self.agent_mode),
            "agent_runtime": str(self.agent_runtime),
            "agent_durability": str(self.agent_durability),
            "agent_decision_timeout_ms": int(self.agent_decision_timeout_ms),
            "agent_max_node_ms": int(self.agent_max_node_ms),
            "agent_max_parallel_proposals": int(self.agent_max_parallel_proposals),
            "agent_shadow_pair_allowlist": list(self.agent_shadow_pair_allowlist),
            "agent_paper_pair_allowlist": list(self.agent_paper_pair_allowlist),
            "agent_paper_sleeve_allowlist": list(self.agent_paper_sleeve_allowlist),
            "agent_paper_intent_allowlist": list(self.agent_paper_intent_allowlist),
            "agent_live_pair_allowlist": list(self.agent_live_pair_allowlist),
            "agent_live_sleeve_allowlist": list(self.agent_live_sleeve_allowlist),
            "agent_live_intent_allowlist": list(self.agent_live_intent_allowlist),
            "agent_allow_remote_llm": bool(self.agent_allow_remote_llm),
            "agent_allow_external_tools": bool(self.agent_allow_external_tools),
            "agent_require_human_approval": bool(self.agent_require_human_approval),
            "agent_trace_retention_days": int(self.agent_trace_retention_days),
            "agent_enable_otel": bool(self.agent_enable_otel),
            "agent_otel_exporter": str(self.agent_otel_exporter),
            "phase6b_canary_p95_overhead_ms": float(self.phase6b_canary_p95_overhead_ms),
            "phase6b_canary_p99_overhead_ms": float(self.phase6b_canary_p99_overhead_ms),
            "phase6b_canary_ack_success_floor": float(self.phase6b_canary_ack_success_floor),
            "phase6b_canary_orphan_command_limit": int(self.phase6b_canary_orphan_command_limit),
            "phase6b_canary_entry_ratio_floor": float(self.phase6b_canary_entry_ratio_floor),
            "phase6b_canary_slot_utilisation_floor": float(self.phase6b_canary_slot_utilisation_floor),
            "phase6b_canary_drawdown_deterioration_pct": float(self.phase6b_canary_drawdown_deterioration_pct),
            "phase6b_canary_ramp_steps_pct": list(self.phase6b_canary_ramp_steps_pct),
            "phase6b_canary_alert_window_minutes": int(self.phase6b_canary_alert_window_minutes),
            "mcp_enabled": bool(self.mcp_enabled),
            "mcp_transport": str(self.mcp_transport),
            "openclaw_enabled": bool(self.openclaw_enabled),
            "openclaw_scopes": str(self.openclaw_scopes),
            "openclaw_sandbox_required": bool(self.openclaw_sandbox_required),
            "model_bundle_version": str(self.model_bundle_version),
            "model_manifest_path": str(self.model_manifest_path),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
