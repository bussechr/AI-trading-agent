from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path
import sys

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

import fxstack.runtime.runner as runtime_runner
from fxstack.risk.contracts import RiskDecision
from fxstack.runtime.runner import _prepare_pair_rows_for_scoring
from fxstack.runtime.runner import _build_allocator_open_positions
from fxstack.runtime.runner import _latest_feature_row, _FEATURE_SERVING_TELEMETRY, _sequence_shadow_metrics, _sync_lifecycle_action_payloads
from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings


class _Model:
    def __init__(self, feature_columns: list[str] | None = None) -> None:
        self.feature_columns = list(feature_columns or [])

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"p0": [0.3] * len(X), "p1": [0.7] * len(X)}, index=X.index)


def _bars(pair: str, timeframe: str, rows: int = 600) -> pd.DataFrame:
    base = 1.10 if pair == "EURUSD" else 145.0
    step = 0.0001 if pair == "EURUSD" else 0.01
    tf_minutes = {"M5": 5}[timeframe]
    out = []
    for i in range(rows):
        px = base + (i * step)
        out.append(
            {
                "pair": pair,
                "ts": pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=tf_minutes * i),
                "timeframe": timeframe,
                "bid_open": px - step,
                "bid_high": px + (2 * step),
                "bid_low": px - (2 * step),
                "bid_close": px - (0.5 * step),
                "ask_open": px + step,
                "ask_high": px + (3 * step),
                "ask_low": px,
                "ask_close": px + (0.5 * step),
                "mid_open": px,
                "mid_high": px + (2 * step),
                "mid_low": px - (2 * step),
                "mid_close": px + (0.25 * step),
                "spread": step,
                "volume": 100.0 + i,
                "date": (pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(minutes=tf_minutes * i)).strftime("%Y-%m-%d"),
            }
        )
    return pd.DataFrame(out)


def test_prepare_pair_rows_for_scoring_enriches_nan_meta_fields_from_raw_contract(tmp_path) -> None:
    provider = get_settings().normalized_data_provider
    store = ParquetStore(tmp_path)
    store.write_partitioned(_bars("EURUSD", "M5", rows=4000), provider=provider, pair="EURUSD", timeframe="M5")
    store.write_partitioned(_bars("USDJPY", "M5", rows=4000), provider=provider, pair="USDJPY", timeframe="M5")

    row = store.read_pair_timeframe(provider=provider, pair="EURUSD", timeframe="M5").sort_values("ts").tail(1).copy()
    row["m15_ret_1"] = float("nan")
    row["cross_pair_dispersion"] = float("nan")

    loaded = SimpleNamespace(
        scorer=SimpleNamespace(
            swing_model=_Model([]),
            intraday_model=_Model([]),
            meta_model=_Model(["m15_ret_1", "cross_pair_dispersion"]),
        ),
        exit_model=None,
        reversal_failure_model=None,
        reversal_opportunity_model=None,
    )

    prepared = _prepare_pair_rows_for_scoring(
        raw_store=store,
        pair="EURUSD",
        loaded=loaded,
        pair_rows={"M5": row},
        swing_timeframe="H1",
        intraday_timeframe="M5",
        all_pairs=["EURUSD", "USDJPY"],
    )

    out = prepared["M5"].reset_index(drop=True).iloc[0]
    assert pd.notna(out["m15_ret_1"])
    assert pd.notna(out["cross_pair_dispersion"])


def test_latest_feature_row_records_feature_serving_telemetry(tmp_path, monkeypatch) -> None:
    provider = get_settings().normalized_data_provider
    feature_store = ParquetStore(tmp_path / "feature")
    raw_store = ParquetStore(tmp_path / "raw")
    raw_store.write_partitioned(_bars("EURUSD", "M5", rows=4000), provider=provider, pair="EURUSD", timeframe="M5")

    _FEATURE_SERVING_TELEMETRY.clear()

    monkeypatch.setattr(
        "fxstack.feast.online_features._cached_feature_store_handle",
        lambda *args, **kwargs: None,
    )
    row = _latest_feature_row(store=feature_store, raw_store=raw_store, pair="EURUSD", timeframe="M5", all_pairs=["EURUSD"])
    assert not row.empty
    snapshot = _FEATURE_SERVING_TELEMETRY[("EURUSD", "M5")]
    assert snapshot["source"] == "raw_contract_fallback"
    assert snapshot["source_chain"] == ["feast_online", "parquet_fallback", "raw_contract_fallback"]


def test_loaded_feature_service_name_prefers_active_metadata() -> None:
    loaded = SimpleNamespace(
        component_feature_services={
            "regime": {"feature_service_name": "fx_eurusd_regime_hmm_h4"},
            "swing_xgb": {"name": "fx_eurusd_swing_xgb_d"},
            "intraday_xgb": {"feature_service": "fx_eurusd_intraday_xgb_m5"},
        }
    )

    assert (
        runtime_runner._loaded_feature_service_name(
            loaded,
            pair="EURUSD",
            timeframe="H4",
            regime_timeframe="H4",
            swing_timeframe="D",
            intraday_timeframe="M5",
        )
        == "fx_eurusd_regime_hmm_h4"
    )
    assert (
        runtime_runner._loaded_feature_service_name(
            loaded,
            pair="EURUSD",
            timeframe="D",
            regime_timeframe="H4",
            swing_timeframe="D",
            intraday_timeframe="M5",
        )
        == "fx_eurusd_swing_xgb_d"
    )
    assert (
        runtime_runner._loaded_feature_service_name(
            loaded,
            pair="EURUSD",
            timeframe="M5",
            regime_timeframe="H4",
            swing_timeframe="D",
            intraday_timeframe="M5",
        )
        == "fx_eurusd_intraday_xgb_m5"
    )


def test_latest_feature_row_forwards_feature_service_name(tmp_path, monkeypatch) -> None:
    provider = get_settings().normalized_data_provider
    feature_store = ParquetStore(tmp_path / "feature")
    raw_store = ParquetStore(tmp_path / "raw")
    raw_store.write_partitioned(_bars("EURUSD", "M5", rows=4000), provider=provider, pair="EURUSD", timeframe="M5")

    captured: dict[str, str | None] = {}

    def _fake_resolve_latest_feature_row(*, feature_service_name=None, **kwargs):  # noqa: ANN001
        captured["feature_service_name"] = feature_service_name
        return (
            pd.DataFrame([{"pair": "EURUSD", "ts": "2026-01-01T00:00:00Z", "ret_1": 0.0}]),
            runtime_runner.FeatureServingTelemetry(source="feast_online", feature_service=str(feature_service_name or ""), cache_hit=True, reason="ok"),
        )

    monkeypatch.setattr(runtime_runner, "resolve_latest_feature_row", _fake_resolve_latest_feature_row)

    row = _latest_feature_row(
        store=feature_store,
        raw_store=raw_store,
        pair="EURUSD",
        timeframe="M5",
        feature_service_name="fx_eurusd_intraday_xgb_m5",
        all_pairs=["EURUSD"],
    )

    assert not row.empty
    assert captured["feature_service_name"] == "fx_eurusd_intraday_xgb_m5"


def test_startup_required_column_gaps_reports_missing_columns(monkeypatch) -> None:
    loaded = SimpleNamespace(
        scorer=SimpleNamespace(
            swing_model=_Model(["swing_a", "swing_b"]),
            intraday_model=_Model([]),
            meta_model=_Model([]),
        ),
        exit_model=None,
        reversal_failure_model=None,
        reversal_opportunity_model=None,
    )

    monkeypatch.setattr(runtime_runner, "_required_model_feature_columns", lambda *args, **kwargs: ["intraday_a", "intraday_b"])

    gaps = runtime_runner._startup_required_column_gaps(
        loaded=loaded,
        pair_rows={
            "D": pd.DataFrame([{"swing_a": 1.0}]),
            "M5": pd.DataFrame([{"intraday_a": 1.0}]),
        },
        swing_timeframe="D",
        intraday_timeframe="M5",
    )

    assert gaps == {"D": ["swing_b"], "M5": ["intraday_b"]}


def test_enqueue_feature_pushes_activates_when_feast_is_enabled(tmp_path, monkeypatch) -> None:
    provider = get_settings().normalized_data_provider
    feature_store = ParquetStore(tmp_path / "feature")
    feature_store.write_partitioned(
        pd.DataFrame(
            [
                {
                    "pair": "EURUSD",
                    "ts": pd.Timestamp("2026-01-01T00:00:00Z"),
                    "feature_a": 1.0,
                }
            ]
        ),
        provider=provider,
        pair="EURUSD",
        timeframe="M5",
    )

    class _Svc:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def enqueue_feature_push(self, payload):  # noqa: ANN001
            self.payloads.append(dict(payload))
            return dict(payload)

    monkeypatch.setenv("FXSTACK_FEAST_ENABLED", "1")
    monkeypatch.setenv("FXSTACK_FEATURE_PUSH_ENABLED", "0")
    get_settings.cache_clear()
    try:
        out = runtime_runner._enqueue_feature_pushes(
            svc=_Svc(),
            feature_store=feature_store,
            provider=provider,
            pair="EURUSD",
            feature_refresh={"M5": {"ok": True}},
        )
    finally:
        get_settings.cache_clear()

    assert out["enabled"] is True
    assert out["mode"] == "feast_enabled"
    assert out["queued"] == 1
    assert out["items"]["M5"]["feature_service"] == "fx_eurusd_intraday_xgb_m5"


def test_sync_lifecycle_action_payloads_rewrites_approved_order_after_override() -> None:
    decision = {
        "symbol": "EURUSD",
        "metadata": {
            "pair": "EURUSD",
            "ts": "2026-04-07T12:00:00Z",
            "lifecycle_action": "partial_tp",
            "lifecycle_reason": "take_profit",
            "lifecycle_action_score": 0.81,
            "approved_order": {
                "cmd": "CLOSE_PARTIAL",
                "symbol": "EURUSD",
                "lots": 0.12,
                "close_lots": 0.12,
                "action": "partial_tp",
            },
            "risk_decision": {
                "lifecycle_action": "partial_tp",
                "close_lots": 0.12,
                "approved_order": {
                    "cmd": "CLOSE_PARTIAL",
                    "symbol": "EURUSD",
                    "lots": 0.12,
                    "close_lots": 0.12,
                    "action": "partial_tp",
                },
                "metadata": {},
            },
        },
    }
    action_item = {
        "pair": "EURUSD",
        "ts_value": "2026-04-07T12:00:00Z",
        "lifecycle_action": "exit",
        "lifecycle_reason": "adaptive_replacement_exit",
        "lifecycle_action_score": 0.93,
        "close_lots": 0.0,
        "sl_price": 0.0,
    }

    _sync_lifecycle_action_payloads(decision=decision, action_item=action_item)

    approved = dict(decision["metadata"]["approved_order"] or {})
    assert approved["cmd"] == "CLOSE"
    assert approved["action"] == "exit"
    assert float(approved["close_lots"]) == 0.0
    assert dict(action_item["approved_order"] or {})["cmd"] == "CLOSE"
    risk_decision = dict(decision["metadata"]["risk_decision"] or {})
    assert risk_decision["lifecycle_action"] == "exit"
    assert dict(risk_decision["approved_order"] or {})["cmd"] == "CLOSE"


def test_sequence_shadow_metrics_reports_sidecar_probabilities() -> None:
    row = pd.DataFrame({"ret_1": [0.01], "vol_20": [0.2]})
    loaded = SimpleNamespace(
        swing_shadow_model=_Model(["ret_1", "vol_20"]),
        intraday_shadow_model=_Model(["ret_1", "vol_20"]),
        shadow_bundle_run_id="bundle-shadow-1",
        shadow_component_refs={
            "swing_patchtst": {
                "evidence_refs": {
                    "training_report": "swing-report.json",
                    "promotion_decision": "swing-promotion.json",
                    "model_manifest": "swing-manifest.json",
                    "sequence_dataset_manifest": "swing-sequence.json",
                    "portfolio_report": "swing-portfolio.json",
                    "challenger_head_to_head": "swing-head.json",
                    "portfolio_disagreement": "swing-disagreement.json",
                }
            },
            "intraday_patchtst": {
                "evidence_refs": {
                    "training_report": "intraday-report.json",
                    "promotion_decision": "intraday-promotion.json",
                    "model_manifest": "intraday-manifest.json",
                    "sequence_dataset_manifest": "intraday-sequence.json",
                    "portfolio_report": "intraday-portfolio.json",
                    "challenger_head_to_head": "intraday-head.json",
                    "portfolio_disagreement": "intraday-disagreement.json",
                }
            },
        },
    )
    signal = SimpleNamespace(swing_prob=0.62, entry_prob=0.58)

    out = _sequence_shadow_metrics(loaded=loaded, swing_row=row, intraday_row=row, signal=signal)

    assert bool(out["available"]) is True
    assert float(out["probs"]["swing_patchtst"]) == 0.7
    assert "swing_patchtst_vs_live" in out["disagreement"]
    assert out["report_refs"]["swing_patchtst"]["training_report"] == "swing-report.json"
    assert out["report_refs"]["swing_patchtst"]["sequence_dataset_manifest"] == "swing-sequence.json"
    assert out["report_refs"]["swing_patchtst"]["portfolio_report"] == "swing-portfolio.json"


def test_load_sequence_shadow_bundle_prefers_local_path(monkeypatch, tmp_path) -> None:
    swing_path = tmp_path / "swing_patchtst"
    intraday_path = tmp_path / "intraday_patchtst"
    swing_path.mkdir(parents=True, exist_ok=True)
    intraday_path.mkdir(parents=True, exist_ok=True)

    dummy_module = SimpleNamespace(SwingPatchTST=object(), IntradayPatchTST=object())
    monkeypatch.setitem(sys.modules, "fxstack.models.patchtst", dummy_module)
    monkeypatch.setattr(
        runtime_runner,
        "get_settings",
        lambda: SimpleNamespace(sequence_shadow_enabled=True, mlflow_enabled=True),
    )
    monkeypatch.setattr(
        runtime_runner,
        "resolve_bundle_manifest_by_alias",
        lambda **kwargs: SimpleNamespace(
            bundle_run_id="bundle-shadow-1",
            components={
                "swing_patchtst": SimpleNamespace(
                    to_dict=lambda: {
                        "path": str(swing_path),
                        "model_uri": "models:/fx.swing_patchtst.EURUSD.D@shadow",
                    }
                ),
                "intraday_patchtst": SimpleNamespace(
                    to_dict=lambda: {
                        "path": str(intraday_path),
                        "model_uri": "models:/fx.intraday_patchtst.EURUSD.M5@shadow",
                    }
                ),
            },
        ),
    )

    seen: list[str] = []

    def _fake_safe_load(model_cls, raw_path: str, project_root):
        seen.append(raw_path)
        return SimpleNamespace(), ""

    monkeypatch.setattr(runtime_runner, "_safe_load", _fake_safe_load)

    models, bundle_run_id, refs, errors = runtime_runner._load_sequence_shadow_bundle(
        pair="EURUSD",
        timeframes={"swing": "D", "intraday": "M5"},
        project_root=tmp_path,
    )

    assert bundle_run_id == "bundle-shadow-1"
    assert seen == [str(swing_path), str(intraday_path)]
    assert set(models) == {"swing_patchtst", "intraday_patchtst"}
    assert not errors
    assert str(refs["swing_patchtst"]["path"]) == str(swing_path)
    assert str(refs["intraday_patchtst"]["path"]) == str(intraday_path)


def test_apply_adaptive_shadow_ranking_surfaces_allocator_portfolio_pressure_metadata() -> None:
    class Settings:
        adaptive_shadow_enabled = True
        use_portfolio_ranking = True
        max_total_positions = 6
        max_new_entries_per_cycle = 1
        max_pair_positions = 2
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-03-20T10:00:00Z",
                "entry_ready": True,
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "rejection_reason": "none",
                "lifecycle_action": "hold",
            },
        },
        {
            "symbol": "USDJPY",
            "side": "BUY",
            "execution_ready": True,
            "metadata": {
                "pair": "USDJPY",
                "ts": "2026-03-20T10:00:00Z",
                "entry_ready": True,
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "rejection_reason": "none",
                "lifecycle_action": "hold",
            },
        },
    ]
    adaptive_rows_by_pair = {
        "EURUSD": {
            "pair": "EURUSD",
            "signal_side": "long",
            "session_bucket": "london_open",
            "playbook": "trend_pullback",
            "playbook_score": 0.71,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.64,
            "environment_state": "PersistentTrend",
            "uncertainty_score": 0.10,
            "calibrated_ev_bps_shadow": 8.0,
        },
        "USDJPY": {
            "pair": "USDJPY",
            "signal_side": "long",
            "session_bucket": "asia",
            "playbook": "breakout_expansion",
            "playbook_score": 0.71,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.64,
            "environment_state": "PersistentTrend",
            "uncertainty_score": 0.10,
            "calibrated_ev_bps_shadow": 8.0,
        },
    }
    state = {
        "equity": 10_000.0,
        "positions": [
            {"symbol": "EURUSD", "lots": 0.2, "side": "long", "session_bucket": "london_open", "time_in_trade_bars": 7},
            {"symbol": "GBPUSD", "lots": 0.1, "side": "short", "session_bucket": "london_open", "time_in_trade_bars": 5},
        ],
    }

    diag = runtime_runner._apply_adaptive_shadow_ranking(
        decisions,
        settings=Settings(),
        open_position_count=2,
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        state=state,
        current_equity=10_000.0,
    )

    assert diag["adaptive_shadow_candidate_count"] == 2
    assert decisions[0]["metadata"]["portfolio_risk_pressure"] > decisions[1]["metadata"]["portfolio_risk_pressure"]
    assert decisions[0]["metadata"]["portfolio_session_pressure"] > decisions[1]["metadata"]["portfolio_session_pressure"]
    assert decisions[0]["metadata"]["portfolio_correlation_pressure"] > decisions[1]["metadata"]["portfolio_correlation_pressure"]
    assert diag["allocator_session_pressure_avg"] > 0.0
    assert diag["allocator_pair_pressure_avg"] > 0.0
    assert diag["allocator_correlation_pressure_max"] >= diag["allocator_correlation_pressure_avg"]
    assert diag["overlay_cycle_summary"]["session_pressure_avg"] == pytest.approx(diag["allocator_session_pressure_avg"])
    assert diag["overlay_cycle_summary"]["diagnostics"]["portfolio_pressure"]["session_avg"] == pytest.approx(
        diag["allocator_session_pressure_avg"]
    )
    assert diag["overlay_cycle_summary"]["diagnostics"]["portfolio_pressure"]["correlation_max"] == pytest.approx(
        diag["allocator_correlation_pressure_max"]
    )
    assert decisions[0]["metadata"]["allocator_rank"] in {1, 2}
    assert decisions[1]["metadata"]["allocator_rank"] in {1, 2}
    assert decisions[0]["metadata"]["allocator_selected"] in {True, False}
    assert decisions[1]["metadata"]["allocator_selected"] in {True, False}


def test_apply_adaptive_shadow_ranking_consumes_cross_pair_rank_metadata() -> None:
    class Settings:
        adaptive_shadow_enabled = True
        use_portfolio_ranking = True
        max_total_positions = 1
        max_new_entries_per_cycle = 1
        max_pair_positions = 2
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "metadata": {
                "pair": "EURUSD",
                "ts": "2026-03-20T10:00:00Z",
                "entry_ready": True,
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "rejection_reason": "none",
                "lifecycle_action": "hold",
                "cross_pair_rank_position": 1,
                "cross_pair_influence_score": 0.92,
                "cross_pair_recommendation_strength": 0.96,
                "cross_pair_influenced_by_pairs": ["GBPUSD", "USDJPY"],
                "cross_pair_reason_codes": ["local_edge", "basket_alignment", "peer_confluence"],
            },
        },
        {
            "symbol": "USDJPY",
            "side": "BUY",
            "execution_ready": True,
            "metadata": {
                "pair": "USDJPY",
                "ts": "2026-03-20T10:00:00Z",
                "entry_ready": True,
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "rejection_reason": "none",
                "lifecycle_action": "hold",
                "cross_pair_rank_position": 2,
                "cross_pair_influence_score": 0.21,
                "cross_pair_recommendation_strength": 0.24,
                "cross_pair_influenced_by_pairs": ["EURUSD"],
                "cross_pair_reason_codes": ["weak_cross_pair_signal"],
            },
        },
    ]
    adaptive_rows_by_pair = {
        "EURUSD": {
            "pair": "EURUSD",
            "signal_side": "long",
            "session_bucket": "london_open",
            "playbook": "trend_pullback",
            "playbook_score": 0.71,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.64,
            "environment_state": "PersistentTrend",
            "uncertainty_score": 0.10,
            "calibrated_ev_bps_shadow": 8.0,
        },
        "USDJPY": {
            "pair": "USDJPY",
            "signal_side": "long",
            "session_bucket": "london_open",
            "playbook": "trend_pullback",
            "playbook_score": 0.71,
            "location_score": 0.66,
            "trigger_score": 0.61,
            "macro_coherence_score": 0.64,
            "environment_state": "PersistentTrend",
            "uncertainty_score": 0.10,
            "calibrated_ev_bps_shadow": 8.0,
        },
    }

    diag = runtime_runner._apply_adaptive_shadow_ranking(
        decisions,
        settings=Settings(),
        open_position_count=0,
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        state={"equity": 10_000.0, "positions": []},
        current_equity=10_000.0,
    )

    assert diag["adaptive_shadow_candidate_count"] == 2
    assert decisions[0]["metadata"]["allocator_score"] > decisions[1]["metadata"]["allocator_score"]
    assert decisions[0]["metadata"]["allocator_rank"] == 1
    assert decisions[0]["metadata"]["allocator_selected"] is True
    assert decisions[1]["metadata"]["allocator_selected"] is False
    assert decisions[1]["metadata"]["allocator_rejection_reason"] == "allocator_ranked_out"


def test_runtime_artifact_path_prefers_local_manifest_path_over_model_uri() -> None:
    ref = {
        "path": "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/regime_hmm",
        "model_uri": "models:/fx.regime_hmm.EURUSD.H4@champion",
        "evidence_refs": {"artifact_path": "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/regime_hmm"},
    }

    assert runtime_runner._artifact_path(ref) == "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/regime_hmm"
    assert runtime_runner._artifact_value({"regime": ref}, "regime") == "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/regime_hmm"


def test_build_allocator_open_positions_uses_full_book_positions() -> None:
    state = {
        "positions": [
            {"symbol": "EURUSD", "lots": 0.2, "side": "long", "time_in_trade_bars": 7},
            {"symbol": "USDJPY", "lots": 0.1, "side": "short", "time_in_trade_bars": 4},
        ]
    }
    adaptive_rows_by_pair = {
        "EURUSD": {"playbook_score": 0.8, "location_score": 0.7, "trigger_score": 0.6},
        "USDJPY": {"playbook_score": 0.75, "location_score": 0.55, "trigger_score": 0.5},
    }

    open_positions = _build_allocator_open_positions(
        state=state,
        adaptive_position_registry={},
        adaptive_rows_by_pair=adaptive_rows_by_pair,
        current_equity=10000.0,
    )

    assert [item.pair for item in open_positions] == ["EURUSD", "USDJPY"]
    assert [item.replaceable_hold for item in open_positions] == [False, False]


def test_evaluate_runtime_risk_kernel_uses_whole_book_positions_for_allocator_and_telemetry(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _FakeBook:
        gross_exposure = 12.5
        net_exposure = 4.25

        def to_dict(self) -> dict[str, float]:
            return {"gross_exposure": self.gross_exposure, "net_exposure": self.net_exposure}

    class _FakeAllocation:
        allowed = True
        book = _FakeBook()
        concentration = SimpleNamespace(to_dict=lambda: {"portfolio_share": 0.4})
        correlation = SimpleNamespace(to_dict=lambda: {"correlation": 0.2})
        stress = SimpleNamespace(to_dict=lambda: {"drawdown_pct": 1.5})
        budget = SimpleNamespace(budget_scale=0.9, reason="ok")
        telemetry = {"open_position_count": 2, "pending_entry_count": 2}

        def to_dict(self) -> dict[str, object]:
            return {
                "allowed": self.allowed,
                "book": self.book.to_dict(),
                "concentration": self.concentration.to_dict(),
                "correlation": self.correlation.to_dict(),
                "stress": self.stress.to_dict(),
                "budget": {"budget_scale": self.budget.budget_scale, "reason": self.budget.reason},
                "telemetry": dict(self.telemetry),
            }

    def _fake_evaluate_portfolio_allocation(*, positions, pending_entries, **kwargs):
        captured["positions"] = [dict(item) for item in positions]
        captured["pending_entries"] = [dict(item) for item in pending_entries]
        return _FakeAllocation()

    def _fake_evaluate_risk_decision(*, policy_intent, market_state, portfolio_state, config):
        captured["policy_intent"] = policy_intent.to_dict()
        captured["portfolio_state"] = portfolio_state.to_dict()
        return RiskDecision(pair=policy_intent.pair, verdict="allow", reason="", portfolio_state=portfolio_state, metadata={"rollout": {}})

    monkeypatch.setattr(runtime_runner, "evaluate_portfolio_allocation", _fake_evaluate_portfolio_allocation)
    monkeypatch.setattr(runtime_runner, "evaluate_risk_decision", _fake_evaluate_risk_decision)

    out = runtime_runner._evaluate_runtime_risk_kernel(
        pair="EURUSD",
        ts_value="2026-04-07T12:00:00Z",
        side="BUY",
        signal=SimpleNamespace(trade_prob=0.66, session_bucket="london", reversal_ready=False),
        expected_edge_bps=8.0,
        spread_bps=1.2,
        feature_bar={"stale_after_secs": 180.0, "age_secs": 12.0, "stale": False, "reason": "fresh"},
        tick={"bid": 1.1010, "ask": 1.1012},
        spread_unit_source="live",
        mt4_fresh=True,
        ticks_fresh=True,
        paused=False,
        positions=[{"symbol": "EURUSD", "lots": 0.2, "side": "long", "time_in_trade_bars": 7}],
        pair_count=1,
        total_count=2,
        current_equity=10000.0,
        planned_entry_lots=0.15,
        lifecycle_action="hold",
        lifecycle_reason="hold",
        lifecycle_action_score=0.66,
        close_lots=0.0,
        sl_price=0.0,
        rejection_reasons=[],
        state={
            "equity_peak": 10400.0,
            "balance": 10050.0,
            "positions": [
                {"symbol": "EURUSD", "lots": 0.2, "side": "long", "time_in_trade_bars": 7},
                {"symbol": "USDJPY", "lots": 0.1, "side": "short", "time_in_trade_bars": 4},
            ],
        },
        settings=SimpleNamespace(max_total_positions=8, max_pair_positions=3, max_allowed_spread_bps=3.0),
        portfolio_positions=[
            {"symbol": "EURUSD", "lots": 0.2, "side": "long", "time_in_trade_bars": 7},
            {"symbol": "USDJPY", "lots": 0.1, "side": "short", "time_in_trade_bars": 4},
        ],
        governance_policy={"capital_band": "micro_live", "mode": "normal", "budget_scale": 1.0},
        pending_entries=[
            {"symbol": "GBPUSD", "side": "BUY", "lots": 0.05},
            {"symbol": "AUDUSD", "side": "SELL", "lots": 0.05},
        ],
    )

    assert [item["symbol"] for item in captured["positions"]] == ["EURUSD", "USDJPY"]
    assert [item["symbol"] for item in captured["pending_entries"]] == ["GBPUSD", "AUDUSD"]
    assert captured["policy_intent"]["metadata"]["position_count_pair"] == 1
    assert captured["policy_intent"]["metadata"]["position_count_total"] == 2
    assert captured["portfolio_state"]["open_position_count"] == 2
    assert captured["portfolio_state"]["pair_position_count"] == 1
    assert out["portfolio_allocation"]["telemetry"]["open_position_count"] == 2
