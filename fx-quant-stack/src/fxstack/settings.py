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

    command_ttl_secs: float = Field(default=120.0, alias="FXSTACK_COMMAND_TTL_SECS")
    default_session_id: str = Field(default="default", alias="FXSTACK_DEFAULT_SESSION_ID")
    pg_service_name: str = Field(default="", alias="FXSTACK_PG_SERVICE_NAME")
    start_profile: str = Field(default="staged_safe", alias="FXSTACK_START_PROFILE")
    run_fast_gate: bool = Field(default=False, alias="FXSTACK_RUN_FAST_GATE")
    run_shadow_24h: bool = Field(default=False, alias="FXSTACK_RUN_SHADOW_24H")
    allow_sqlite: bool = Field(default=False, alias="FXSTACK_ALLOW_SQLITE")
    require_active_models: bool = Field(default=True, alias="FXSTACK_REQUIRE_ACTIVE_MODELS")
    pairs_csv: str = Field(
        default="EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD",
        alias="FXSTACK_PAIRS",
    )
    intraday_timeframe: str = Field(default="M5", alias="FXSTACK_INTRADAY_TIMEFRAME")
    swing_timeframe: str = Field(default="D", alias="FXSTACK_SWING_TIMEFRAME")
    regime_timeframe: str = Field(default="H4", alias="FXSTACK_REGIME_TIMEFRAME")
    max_pair_positions: int = Field(default=1, alias="FXSTACK_MAX_PAIR_POSITIONS")
    max_total_positions: int = Field(default=6, alias="FXSTACK_MAX_TOTAL_POSITIONS")
    default_order_lots: float = Field(default=0.1, alias="FXSTACK_DEFAULT_ORDER_LOTS")
    max_allowed_spread_bps: float = Field(default=2.5, alias="FXSTACK_MAX_ALLOWED_SPREAD_BPS")
    min_expected_edge_bps: float = Field(default=3.0, alias="FXSTACK_MIN_EXPECTED_EDGE_BPS")
    model_activation_manifest: str = Field(
        default="fx-quant-stack/artifacts/active_models.json",
        alias="FXSTACK_MODEL_ACTIVATION_MANIFEST",
    )
    registry_root: str = Field(default="fx-quant-stack/artifacts/registry", alias="FXSTACK_REGISTRY_ROOT")
    startup_requeue_age_secs: float = Field(default=90.0, alias="FXSTACK_REQUEUE_AGE_SECS")
    db_connect_retries: int = Field(default=5, alias="FXSTACK_DB_CONNECT_RETRIES")
    require_cuda: bool = Field(default=True, alias="FXSTACK_REQUIRE_CUDA")
    deep_model_stale_hours: float = Field(default=24.0, alias="FXSTACK_DEEP_MODEL_STALE_HOURS")
    swing_model_policy: str = Field(
        default="transformer_primary_xgb_fallback",
        alias="FXSTACK_SWING_MODEL_POLICY",
    )
    intraday_model_policy: str = Field(
        default="tcn_primary_xgb_fallback",
        alias="FXSTACK_INTRADAY_MODEL_POLICY",
    )
    tcn_window_size: int = Field(default=128, alias="FXSTACK_TCN_WINDOW_SIZE")
    transformer_window_size: int = Field(default=96, alias="FXSTACK_TRANSFORMER_WINDOW_SIZE")
    deep_train_epochs: int = Field(default=5, alias="FXSTACK_DEEP_TRAIN_EPOCHS")
    deep_batch_size: int = Field(default=64, alias="FXSTACK_DEEP_BATCH_SIZE")
    xgb_device: str = Field(default="auto", alias="FXSTACK_XGB_DEVICE")
    xgb_tree_method: str = Field(default="hist", alias="FXSTACK_XGB_TREE_METHOD")
    xgb_allow_cpu_fallback: bool = Field(default=True, alias="FXSTACK_XGB_ALLOW_CPU_FALLBACK")

    project_root: Path = Path(__file__).resolve().parents[2]

    @property
    def pairs(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.pairs_csv).split(","):
            sym = raw.strip().upper()
            if sym:
                out.append(sym)
        return out

    @property
    def is_sqlite_url(self) -> bool:
        txt = str(self.database_url).strip().lower()
        return txt.startswith("sqlite")

    @property
    def normalized_data_provider(self) -> str:
        txt = str(self.data_provider).strip().lower()
        return txt if txt else "dukascopy"

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "data_provider": self.normalized_data_provider,
            "dukascopy_source_root": self.dukascopy_source_root,
            "dukascopy_file_pattern": self.dukascopy_file_pattern,
            "database_url": self.database_url,
            "mt4_bridge_url": self.mt4_bridge_url,
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
            "max_allowed_spread_bps": float(self.max_allowed_spread_bps),
            "min_expected_edge_bps": float(self.min_expected_edge_bps),
            "registry_root": self.registry_root,
            "model_activation_manifest": self.model_activation_manifest,
            "require_cuda": bool(self.require_cuda),
            "deep_model_stale_hours": float(self.deep_model_stale_hours),
            "swing_model_policy": self.swing_model_policy,
            "intraday_model_policy": self.intraday_model_policy,
            "tcn_window_size": int(self.tcn_window_size),
            "transformer_window_size": int(self.transformer_window_size),
            "deep_train_epochs": int(self.deep_train_epochs),
            "deep_batch_size": int(self.deep_batch_size),
            "xgb_device": self.xgb_device,
            "xgb_tree_method": self.xgb_tree_method,
            "xgb_allow_cpu_fallback": bool(self.xgb_allow_cpu_fallback),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
