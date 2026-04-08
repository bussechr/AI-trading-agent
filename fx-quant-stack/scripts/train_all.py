from __future__ import annotations

import argparse
import json
import platform
import uuid
from pathlib import Path
from typing import Any

import yaml

from fxstack.io.parquet_store import ParquetStore
from fxstack.backtest.harness import (
    DEFAULT_PHASE3_SCENARIOS,
    EconomicReport,
    HarnessRunManifest,
    IntentReplayBundle,
    MarketReplayBundle,
    build_golden_dataset_report,
    build_harness_comparison,
    parity_from_reports,
    run_lean_harness,
    run_nautilus_harness,
)
from fxstack.feast.compaction import compact_feature_repo_for_pair
from fxstack.feast.repository import feature_repo_manifest, feature_repo_manifest_path
from fxstack.mlops.lineage import compute_lineage_snapshot
from fxstack.mlops.registry import (
    COMPONENT_FAMILIES,
    experiment_name_for_component,
    register_component_version,
)
from fxstack.mlops.run_context import MlflowRunContext, build_standard_run_tags
from fxstack.mlops.types import BundleManifest, ModelVersionRef
from fxstack.settings import get_settings
from fxstack.training.phase5_gates import build_phase5_gate_bundle, write_phase5_gate_bundle
from fxstack.tasks import (
    artifact_retrain_decision,
    build_features_task,
    build_exit_labels_task,
    build_fx_lifecycle_features_task,
    build_labels_task,
    build_meta_labels_task,
    build_reversal_labels_task,
    ingest_task,
    train_deep_stale_task,
    train_belief_task,
    train_exit_task,
    train_intraday_task,
    train_intraday_patchtst_task,
    train_meta_task,
    train_regime_task,
    train_reversal_task,
    train_swing_patchtst_task,
    train_swing_task,
)
from fxstack.training.registry import ArtifactRegistry


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return dict(yaml.safe_load(f) or {})


def _read_partition(root: str, *, pair: str, timeframe: str) -> Any:
    return ParquetStore(Path(root)).read_pair_timeframe(
        provider=get_settings().normalized_data_provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
    )


def _ensure_ingested(*, pair: str, timeframe: str, raw_root: Path) -> None:
    ingest_task(
        pair=str(pair).upper(),
        granularity=str(timeframe).upper(),
        store_root=str(raw_root),
    )


def _ensure_simple_features(*, pair: str, timeframe: str, raw_root: Path, feature_root: str) -> None:
    _ensure_ingested(pair=pair, timeframe=timeframe, raw_root=raw_root)
    build_features_task(
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        input_root=str(raw_root),
        output_root=str(feature_root),
    )


def _ensure_hierarchical_intraday_features(*, pair: str, timeframe: str, raw_root: Path, feature_root: str) -> None:
    existing = ParquetStore(Path(feature_root)).read_latest_row(
        provider=get_settings().normalized_data_provider,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
    )
    if not existing.empty:
        row = existing.iloc[0]
        if (
            str(row.get("context_frame_profile", "")).strip() == "hierarchical_v1"
            and "m15_ret_1" in existing.columns
            and "h1_ret_1" in existing.columns
            and "h4_trend_slope_20" in existing.columns
            and "d_trend_slope_20" in existing.columns
        ):
            return
    required_raw = [str(timeframe).upper(), "H4", "D"]
    optional_derived = ["M15", "H1"]
    for tf in required_raw:
        _ensure_ingested(pair=pair, timeframe=tf, raw_root=raw_root)
    for tf in optional_derived:
        try:
            _ensure_ingested(pair=pair, timeframe=tf, raw_root=raw_root)
        except RuntimeError:
            # The hierarchical builder can derive these midframes from the anchor raw bars.
            pass
    build_fx_lifecycle_features_task(
        pair=str(pair).upper(),
        input_root=str(raw_root),
        output_root=str(feature_root),
        anchor_timeframe=str(timeframe).upper(),
        context_timeframes=["M15", "H1", "H4", "D"],
    )


def _ensure_primary_labels(
    *,
    pair: str,
    timeframe: str,
    feature_root: str,
    label_root: str,
    horizon_bars: int,
    tp_atr_mult: float,
    sl_atr_mult: float,
) -> None:
    build_labels_task(
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        feature_root=str(feature_root),
        label_root=str(label_root),
        horizon_bars=int(horizon_bars),
        tp_mult=float(tp_atr_mult),
        sl_mult=float(sl_atr_mult),
    )


def _read_meta_json(path: Path) -> dict[str, Any]:
    meta = path / "meta.json"
    if not meta.exists():
        return {}
    import json

    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload or {}) if isinstance(payload, dict) else {}


def _artifact_training_summary(path: Path) -> dict[str, Any]:
    meta = _read_meta_json(path)
    summary = dict(meta.get("training_window_summary") or {})
    if summary:
        return summary
    return {
        "rows": int(meta.get("train_rows", 0) or 0),
        "start_ts": str(meta.get("data_window_start") or ""),
        "end_ts": str(meta.get("data_window_end") or ""),
    }


def _pair_policies(*, tier: str, settings: Any) -> dict[str, str]:
    if str(tier).lower() == "tier1":
        return {
            "swing": str(settings.swing_model_policy),
            "intraday": str(settings.intraday_model_policy),
        }
    return {
        "swing": "xgb_only",
        "intraday": "xgb_only",
    }


def _artifact_exists(path: Path) -> bool:
    return (path / "meta.json").exists()


def _require_existing_artifact(path: Path, *, label: str) -> None:
    if not _artifact_exists(path):
        raise SystemExit(f"missing required existing artifact for lifecycle-only run: {label} -> {path}")


def _report_status(path: Path) -> str:
    report = _read_json(path)
    decision = dict(report.get("promotion_decision") or {})
    return str(decision.get("status") or "unknown")


def _report_path_for_artifact(path: Path) -> Path:
    return path / "reports" / "training_report.json"


def _legacy_report_path_for_artifact(path: Path) -> Path:
    return path.parent / "reports" / "training_report.json"


def _resolve_report_path(path: Path, report_path: Path | None) -> Path | None:
    if report_path is None:
        return None
    if report_path.exists():
        return report_path
    legacy = _legacy_report_path_for_artifact(path)
    if legacy.exists():
        return legacy
    return report_path


def _aggregate_promotion_status(*, tier: str, lifecycle_complete: bool, component_statuses: dict[str, str]) -> str:
    meta_status = str(component_statuses.get("meta") or "").strip().lower()

    if meta_status != "eligible":
        return meta_status or "unknown"

    if str(tier).lower() == "tier1":
        return "eligible" if bool(lifecycle_complete) else "research_only"

    # Tier2 pair eligibility tracks the promotable entry stack; lifecycle quality is
    # still captured in component statuses and capabilities, but it does not block
    # the whole pair from becoming eligible.
    return "eligible"


def _reuse_result(path: Path, *, model: str, report_path: Path | None = None) -> dict[str, Any]:
    meta = _read_meta_json(path)
    out: dict[str, Any] = {
        "model": model,
        "rows": int(meta.get("train_rows", 0) or 0),
        "path": str(path),
        "action": "reused",
    }
    resolved_report_path = _resolve_report_path(path, report_path)
    if resolved_report_path is not None:
        out["report_path"] = str(resolved_report_path)
        out["promotion_status"] = _report_status(resolved_report_path)
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return path


def _synthesize_backtest_summary(
    *,
    pair: str,
    tier: str,
    promotion_status: str,
    training_window_summary: dict[str, Any],
    component_promotion_status: dict[str, str],
    capabilities: dict[str, Any],
    policies: dict[str, str],
    deep_out: dict[str, Any],
) -> dict[str, Any]:
    return {
        "pair": str(pair).upper(),
        "tier": str(tier),
        "promotion_status": str(promotion_status),
        "component_promotion_status": {str(k): str(v) for k, v in dict(component_promotion_status).items()},
        "training_window_summary": dict(training_window_summary or {}),
        "capabilities": dict(capabilities or {}),
        "policies": dict(policies or {}),
        "deep_stale": dict(deep_out or {}),
        "summary_kind": "phase1_train_all_backtest_summary",
    }


def _artifact_component_specs(
    *,
    pair: str,
    artifact_map: dict[str, dict[str, Any]],
    timeframes: dict[str, str],
) -> list[tuple[str, Path, str]]:
    specs: list[tuple[str, Path, str]] = []
    for component_key in [
        "regime",
        "swing_xgb",
        "intraday_xgb",
        "meta",
        "exit_policy",
        "reversal_failure",
        "reversal_opportunity",
        "swing_transformer",
        "swing_patchtst",
        "intraday_tcn",
        "intraday_patchtst",
        "directional_belief",
    ]:
        raw = dict((artifact_map or {}).get(component_key) or {})
        path_txt = str(raw.get("path") or "").strip()
        if not path_txt:
            continue
        path = Path(path_txt)
        if not (path / "meta.json").exists():
            continue
        timeframe = (
            str(timeframes.get("regime") or "")
            if component_key == "regime"
            else str(timeframes.get("swing") or "")
            if component_key in {"swing_xgb", "swing_transformer", "swing_patchtst"}
            else str(timeframes.get("intraday") or "")
        )
        specs.append((component_key, path, timeframe))
    return specs


def _artifact_entry(*, result: dict[str, Any], fallback_path: Path, fallback_model: str) -> dict[str, Any]:
    entry = {"path": str(result.get("path") or fallback_path), "model": str(result.get("model") or fallback_model)}
    for key in [
        "mlflow_run_id",
        "model_name",
        "model_version",
        "model_uri",
        "bundle_run_id",
        "dataset_fingerprint",
        "feature_service_name",
        "feature_service_version",
        "feature_contract_hash",
        "feature_view_names",
        "feature_retrieval",
        "report_path",
        "sequence_dataset_manifest",
        "portfolio_report",
        "challenger_head_to_head",
        "portfolio_disagreement",
        "source",
        "fallback_reason",
        "point_in_time_key",
        "provider",
        "repo_root",
        "all_pairs",
        "context_timeframes",
    ]:
        value = result.get(key)
        if value not in (None, "", [], {}):
            entry[key] = value
    return entry


def _log_component_evidence(
    *,
    run: MlflowRunContext,
    artifact_path: Path,
    feature_schema: dict[str, Any],
    lineage: Any,
    backtest_summary_path: Path,
) -> None:
    meta_path = artifact_path / "meta.json"
    if meta_path.exists():
        run.log_artifact(meta_path, artifact_path="evidence")
    report_dir = artifact_path / "reports"
    if report_dir.exists():
        run.log_artifacts(report_dir, artifact_path="evidence/reports")
    if backtest_summary_path.exists():
        run.log_artifact(backtest_summary_path, artifact_path="evidence")
    run.log_dict(dict(feature_schema or {}), "feature_schema.json")
    if lineage is not None:
        run.log_dict(lineage.to_dict(), "lineage.json")


def _phase3_risk_trace_schema() -> dict[str, Any]:
    return {
        "schema_version": "phase3_risk_trace_schema_v1",
        "kernel_version": "phase3_risk_kernel_v1",
        "rule_order": [
            "data_freshness",
            "marketability",
            "spread_session",
            "exposure",
            "position_caps",
            "drawdown",
            "lifecycle_overrides",
            "final_sizing_order",
        ],
    }


def _phase3_execution_metrics(*, pair: str, backtest_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "planned",
        "engine": "internal",
        "pair": str(pair).upper(),
        "legacy_summary": dict(backtest_summary or {}),
        "realized_pnl_usd": float(backtest_summary.get("net_pnl_usd", 0.0) or 0.0),
        "trade_count": int(backtest_summary.get("trades", 0) or 0),
        "max_drawdown_pct": float(backtest_summary.get("max_drawdown_pct", 0.0) or 0.0),
        "turnover_lots": 0.0,
        "latency_ms_p95": 0.0,
        "rejection_rate": 0.0,
        "notes": ["phase3_execution_runner_pending"],
    }


def _build_phase3_evidence(
    *,
    pair: str,
    reports_root: Path,
    lineage: Any,
    intraday_timeframe: str,
    backtest_summary: dict[str, Any],
) -> dict[str, Any]:
    phase3_root = reports_root / "phase3"
    phase3_root.mkdir(parents=True, exist_ok=True)
    market_bundle = MarketReplayBundle(
        pair=str(pair).upper(),
        timeframe=str(intraday_timeframe).upper(),
        dataset_hash=str(lineage.dataset_fingerprint),
        feature_service_name=f"fx_{str(pair).lower()}_execution_grade_{str(intraday_timeframe).lower()}",
        feature_service_version=str(lineage.feature_service_version),
        metadata={"label_version": str(lineage.label_version)},
    )
    intent_bundle = IntentReplayBundle(
        pair=str(pair).upper(),
        intents_path=str(phase3_root / "intent_replay_bundle.json"),
        policy_version="phase3_risk_kernel_v1",
        kernel_version="phase3_risk_kernel_v1",
        metadata={"risk_config_version": str(lineage.risk_config_version)},
    )
    execution_metrics = _phase3_execution_metrics(pair=pair, backtest_summary=backtest_summary)
    execution_metrics.update(
        {
            "dataset_hash": str(lineage.dataset_fingerprint),
            "feature_service_name": str(market_bundle.feature_service_name),
            "feature_service_version": str(market_bundle.feature_service_version),
            "kernel_version": "phase3_risk_kernel_v1",
        }
    )
    execution_report = EconomicReport(
        engine="internal",
        pair=str(pair).upper(),
        status=str(execution_metrics.get("status") or "planned"),
        realized_pnl_usd=float(execution_metrics.get("realized_pnl_usd", 0.0) or 0.0),
        turnover_lots=float(execution_metrics.get("turnover_lots", 0.0) or 0.0),
        max_drawdown_pct=float(execution_metrics.get("max_drawdown_pct", 0.0) or 0.0),
        trade_count=int(execution_metrics.get("trade_count", 0) or 0),
        latency_ms_p95=float(execution_metrics.get("latency_ms_p95", 0.0) or 0.0),
        rejection_rate=float(execution_metrics.get("rejection_rate", 0.0) or 0.0),
        notes=list(execution_metrics.get("notes") or []),
        metadata={"legacy_backtest_summary": dict(backtest_summary or {})},
    )
    nautilus_manifest = run_nautilus_harness(
        bundle_dir=phase3_root,
        output_dir=phase3_root / "nautilus",
        pair=pair,
        dataset_hash=str(lineage.dataset_fingerprint),
        feature_service_name=str(market_bundle.feature_service_name),
        feature_service_version=str(market_bundle.feature_service_version),
        kernel_version="phase3_risk_kernel_v1",
        execute=False,
    )
    lean_manifest = run_lean_harness(
        bundle_dir=phase3_root,
        output_dir=phase3_root / "lean",
        pair=pair,
        dataset_hash=str(lineage.dataset_fingerprint),
        feature_service_name=str(market_bundle.feature_service_name),
        feature_service_version=str(market_bundle.feature_service_version),
        kernel_version="phase3_risk_kernel_v1",
        execute=False,
    )
    internal_manifest = HarnessRunManifest(
        engine="internal",
        status="planned",
        pair=str(pair).upper(),
        dataset_hash=str(lineage.dataset_fingerprint),
        feature_service_name=str(market_bundle.feature_service_name),
        feature_service_version=str(market_bundle.feature_service_version),
        kernel_version="phase3_risk_kernel_v1",
        engine_version=str(platform.python_version()),
        artifacts={"report": str(phase3_root / "execution_metrics.json")},
        metadata={
            "runner": "internal_pnl",
            "engine_package": "fxstack_internal",
            "pass_fail_gates": {
                "parity_required": True,
                "stress_required": True,
                "realized_pnl_required": True,
            },
        },
    )
    placeholder_external = EconomicReport(engine="nautilus", pair=str(pair).upper(), status="planned")
    placeholder_lean = EconomicReport(engine="lean", pair=str(pair).upper(), status="planned")
    parity_reports = [
        parity_from_reports(base_engine="internal", comparison_engine="nautilus", pair=pair, base=execution_report, comparison=placeholder_external),
        parity_from_reports(base_engine="internal", comparison_engine="lean", pair=pair, base=execution_report, comparison=placeholder_lean),
    ]
    golden_report = build_golden_dataset_report(
        market=market_bundle,
        intents=intent_bundle,
        row_count=0,
        schema_hash=str(lineage.feature_set_hash),
        feature_parity_score=1.0,
        metadata={"dataset_fingerprint": str(lineage.dataset_fingerprint)},
    )
    stress_report = {
        "status": "planned",
        "base_engine": "internal",
        "dataset_hash": str(lineage.dataset_fingerprint),
        "feature_service_name": str(market_bundle.feature_service_name),
        "feature_service_version": str(market_bundle.feature_service_version),
        "kernel_version": "phase3_risk_kernel_v1",
        "scenario_count": int(len(DEFAULT_PHASE3_SCENARIOS)),
        "scenarios": [scenario.to_dict() for scenario in DEFAULT_PHASE3_SCENARIOS],
    }
    harness_comparison = build_harness_comparison(
        internal_report=execution_report,
        nautilus_report=placeholder_external,
        lean_report=placeholder_lean,
        parity_reports=parity_reports,
        manifests=[internal_manifest, nautilus_manifest, lean_manifest],
    )
    risk_trace_schema = _phase3_risk_trace_schema()

    execution_metrics_path = _write_json(phase3_root / "execution_metrics.json", execution_metrics)
    intent_bundle_path = _write_json(phase3_root / "intent_replay_bundle.json", intent_bundle.to_dict())
    market_bundle_path = _write_json(phase3_root / "market_replay_bundle.json", market_bundle.to_dict())
    golden_dataset_path = _write_json(phase3_root / "golden_dataset_report.json", golden_report)
    stress_summary_path = _write_json(phase3_root / "stress_harness_summary.json", stress_report)
    harness_comparison_path = _write_json(phase3_root / "harness_comparison.json", harness_comparison)
    risk_trace_schema_path = _write_json(phase3_root / "risk_trace_schema.json", risk_trace_schema)
    internal_manifest_path = _write_json(phase3_root / "internal_harness_manifest.json", internal_manifest.to_dict())
    nautilus_manifest_path = _write_json(phase3_root / "nautilus_harness_manifest.json", nautilus_manifest.to_dict())
    lean_manifest_path = _write_json(phase3_root / "lean_harness_manifest.json", lean_manifest.to_dict())
    return {
        "execution_metrics": str(execution_metrics_path),
        "intent_replay_bundle": str(intent_bundle_path),
        "market_replay_bundle": str(market_bundle_path),
        "golden_dataset_report": str(golden_dataset_path),
        "stress_harness_summary": str(stress_summary_path),
        "harness_comparison": str(harness_comparison_path),
        "risk_trace_schema": str(risk_trace_schema_path),
        "internal_harness_manifest": str(internal_manifest_path),
        "nautilus_harness_manifest": str(nautilus_manifest_path),
        "lean_harness_manifest": str(lean_manifest_path),
    }


def main() -> None:
    s = get_settings()
    ap = argparse.ArgumentParser(description="Train baseline model stack and register artifacts")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--swing-timeframe", default="D")
    ap.add_argument("--intraday-timeframe", default="M5")
    ap.add_argument("--regime-timeframe", default="H4")
    ap.add_argument("--feature-root", default="data/features")
    ap.add_argument("--label-root", default="data/labels")
    ap.add_argument("--artifact-root", default="artifacts")
    ap.add_argument("--training-config", default="configs/training.yaml")
    ap.add_argument("--registry-root", default="artifacts/registry")
    ap.add_argument("--deep-stale-hours", type=float, default=float(s.deep_retrain_max_age_hours))
    ap.add_argument("--force-retrain", action="store_true")
    ap.add_argument("--lifecycle-only", action="store_true")
    ap.add_argument("--with-belief", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--with-patchtst", action="store_true")
    args = ap.parse_args()

    pair = str(args.pair).upper()
    artifact_root = Path(args.artifact_root)
    training_cfg = _load_yaml(Path(args.training_config))
    raw_root = s.project_root / "data" / "raw"
    swing_timeframe = str(args.swing_timeframe).upper()
    intraday_timeframe = str(args.intraday_timeframe).upper()
    regime_timeframe = str(args.regime_timeframe).upper()
    labeling_cfg = dict(training_cfg.get("labeling") or {})
    swing_label_cfg = dict(labeling_cfg.get("swing") or {})
    intraday_label_cfg = dict(labeling_cfg.get("intraday") or {})
    tier = str(s.pair_tier(pair))
    policies = _pair_policies(tier=tier, settings=s)

    pair_root = artifact_root / pair.lower()
    regime_out = pair_root / "regime_hmm"
    swing_out = pair_root / "swing_xgb"
    swing_tf_out = pair_root / "swing_transformer"
    swing_patchtst_out = pair_root / "swing_patchtst"
    intraday_out = pair_root / "intraday_xgb"
    intraday_tcn_out = pair_root / "intraday_tcn"
    intraday_patchtst_out = pair_root / "intraday_patchtst"
    meta_out = pair_root / "meta_filter"
    exit_out = pair_root / "exit_policy_xgb"
    reversal_failure_out = pair_root / "reversal_failure_xgb"
    reversal_opportunity_out = pair_root / "reversal_opportunity_xgb"
    belief_out = artifact_root / "directional_belief"
    meta_report = _report_path_for_artifact(meta_out)
    exit_report = _report_path_for_artifact(exit_out)
    reversal_failure_report = _report_path_for_artifact(reversal_failure_out)
    reversal_opportunity_report = _report_path_for_artifact(reversal_opportunity_out)
    swing_patchtst_report = _report_path_for_artifact(swing_patchtst_out)
    intraday_patchtst_report = _report_path_for_artifact(intraday_patchtst_out)

    _ensure_hierarchical_intraday_features(pair=pair, timeframe=intraday_timeframe, raw_root=raw_root, feature_root=args.feature_root)
    regime_retrained = False
    swing_retrained = False
    intraday_retrained = False
    regime_decision = {"new_rows": 0, "should_retrain": False}
    swing_decision = {"new_rows": 0, "should_retrain": False}
    intraday_decision = {"new_rows": 0, "should_retrain": False}
    meta_decision = {"new_rows": 0, "should_retrain": False}

    if bool(args.lifecycle_only):
        for required_path, label in [
            (regime_out, "regime_hmm"),
            (swing_out, "swing_xgb"),
            (intraday_out, "intraday_xgb"),
            (meta_out, "meta_filter"),
        ]:
            _require_existing_artifact(required_path, label=label)
        r_regime = _reuse_result(regime_out, model="regime_hmm")
        r_swing = _reuse_result(swing_out, model="swing_xgb")
        r_intraday = _reuse_result(intraday_out, model="intraday_xgb")
        r_meta = _reuse_result(meta_out, model="meta_filter", report_path=meta_report)
    else:
        _ensure_simple_features(pair=pair, timeframe=regime_timeframe, raw_root=raw_root, feature_root=args.feature_root)
        _ensure_simple_features(pair=pair, timeframe=swing_timeframe, raw_root=raw_root, feature_root=args.feature_root)
        _ensure_primary_labels(
            pair=pair,
            timeframe=swing_timeframe,
            feature_root=args.feature_root,
            label_root=args.label_root,
            horizon_bars=int(swing_label_cfg.get("horizon_bars", 24)),
            tp_atr_mult=float(swing_label_cfg.get("tp_atr_mult", 2.0)),
            sl_atr_mult=float(swing_label_cfg.get("sl_atr_mult", 1.5)),
        )
        _ensure_primary_labels(
            pair=pair,
            timeframe=intraday_timeframe,
            feature_root=args.feature_root,
            label_root=args.label_root,
            horizon_bars=int(intraday_label_cfg.get("horizon_bars", 18)),
            tp_atr_mult=float(intraday_label_cfg.get("tp_atr_mult", 1.5)),
            sl_atr_mult=float(intraday_label_cfg.get("sl_atr_mult", 1.2)),
        )

        regime_features = _read_partition(args.feature_root, pair=pair, timeframe=regime_timeframe)
        swing_labels = _read_partition(args.label_root, pair=pair, timeframe=swing_timeframe)
        intraday_labels = _read_partition(args.label_root, pair=pair, timeframe=intraday_timeframe)

        regime_decision = artifact_retrain_decision(
            dataset=regime_features,
            artifact_path=regime_out,
            min_new_rows=max(1, int(len(regime_features) + 1)),
            weekly_only=True,
        )
        if bool(args.force_retrain) or bool(regime_decision["should_retrain"]):
            r_regime = train_regime_task(
                pair=pair,
                timeframe=regime_timeframe,
                feature_root=args.feature_root,
                out=str(regime_out),
            )
            regime_retrained = True
        else:
            r_regime = _reuse_result(regime_out, model="regime_hmm")

        swing_decision = artifact_retrain_decision(
            dataset=swing_labels,
            artifact_path=swing_out,
            min_new_rows=max(1, int(len(swing_labels) + 1)),
            weekly_only=True,
        )
        if bool(args.force_retrain) or bool(swing_decision["should_retrain"]):
            r_swing = train_swing_task(
                pair=pair,
                timeframe=swing_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(swing_out),
            )
            swing_retrained = True
        else:
            r_swing = _reuse_result(swing_out, model="swing_xgb")

        intraday_decision = artifact_retrain_decision(
            dataset=intraday_labels,
            artifact_path=intraday_out,
            min_new_rows=max(1, int(s.intraday_retrain_min_new_rows)),
        )
        if bool(args.force_retrain) or bool(intraday_decision["should_retrain"]):
            r_intraday = train_intraday_task(
                pair=pair,
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(intraday_out),
            )
            intraday_retrained = True
        else:
            r_intraday = _reuse_result(intraday_out, model="intraday_xgb")

        build_meta_labels_task(
            pair=pair,
            timeframe=intraday_timeframe,
            feature_root=args.feature_root,
            label_root=args.label_root,
            anchor_timeframe=intraday_timeframe,
            swing_timeframe=swing_timeframe,
            regime_timeframe=regime_timeframe,
            regime_model_path=str(regime_out),
            swing_model_path=str(swing_out),
            intraday_model_path=str(intraday_out),
        )
        meta_labels = ParquetStore(Path(args.label_root) / "meta").read_pair_timeframe(
            provider=s.normalized_data_provider,
            pair=pair,
            timeframe=intraday_timeframe,
        )
        meta_requires_refresh = bool(regime_retrained or swing_retrained or intraday_retrained)
        meta_decision = artifact_retrain_decision(
            dataset=meta_labels,
            artifact_path=meta_out,
            min_new_rows=max(1, int(s.meta_retrain_min_new_rows)),
        )
        if bool(args.force_retrain) or bool(meta_requires_refresh) or bool(meta_decision["should_retrain"]):
            r_meta = train_meta_task(
                pair=pair,
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                out=str(meta_out),
                label_root=args.label_root,
            )
        else:
            r_meta = _reuse_result(meta_out, model="meta_filter", report_path=meta_report)
    lifecycle_ready = bool(
        _artifact_exists(exit_out) and _artifact_exists(reversal_failure_out) and _artifact_exists(reversal_opportunity_out)
    )
    if bool(args.lifecycle_only) and lifecycle_ready and not bool(args.force_retrain):
        exit_decision = {"new_rows": 0, "should_retrain": False}
        reversal_decision = {"new_rows": 0, "should_retrain": False}
        r_exit = _reuse_result(exit_out, model="exit_policy_xgb", report_path=exit_report)
        r_reversal = {
            "failure_model": _reuse_result(
                reversal_failure_out,
                model="reversal_failure_xgb",
                report_path=reversal_failure_report,
            ),
            "opportunity_model": _reuse_result(
                reversal_opportunity_out,
                model="reversal_opportunity_xgb",
                report_path=reversal_opportunity_report,
            ),
            "action": "reused",
        }
    else:
        build_exit_labels_task(
            pair=pair,
            timeframe=intraday_timeframe,
            feature_root=args.feature_root,
            label_root=args.label_root,
        )
        build_reversal_labels_task(
            pair=pair,
            timeframe=intraday_timeframe,
            feature_root=args.feature_root,
            label_root=args.label_root,
        )
        exit_labels = ParquetStore(Path(args.label_root) / "exit").read_pair_timeframe(
            provider=s.normalized_data_provider,
            pair=pair,
            timeframe=intraday_timeframe,
        )
        reversal_labels = ParquetStore(Path(args.label_root) / "reversal").read_pair_timeframe(
            provider=s.normalized_data_provider,
            pair=pair,
            timeframe=intraday_timeframe,
        )

        exit_decision = artifact_retrain_decision(
            dataset=exit_labels,
            artifact_path=exit_out,
            min_new_rows=max(1, int(s.lifecycle_retrain_min_new_events)),
        )
        if bool(args.force_retrain) or bool(exit_decision["should_retrain"]):
            r_exit = train_exit_task(
                pair=pair,
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(exit_out),
            )
        else:
            r_exit = _reuse_result(exit_out, model="exit_policy_xgb", report_path=exit_report)

        reversal_decision = artifact_retrain_decision(
            dataset=reversal_labels,
            artifact_path=reversal_failure_out,
            min_new_rows=max(1, int(s.lifecycle_retrain_min_new_events)),
        )
        if bool(args.force_retrain) or bool(reversal_decision["should_retrain"]):
            r_reversal = train_reversal_task(
                pair=pair,
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out_failure=str(reversal_failure_out),
                out_opportunity=str(reversal_opportunity_out),
            )
        else:
            r_reversal = {
                "failure_model": _reuse_result(
                    reversal_failure_out,
                    model="reversal_failure_xgb",
                    report_path=reversal_failure_report,
                ),
                "opportunity_model": _reuse_result(
                    reversal_opportunity_out,
                    model="reversal_opportunity_xgb",
                    report_path=reversal_opportunity_report,
                ),
                "action": "reused",
            }

    deep_required = not (str(policies["swing"]) == "xgb_only" and str(policies["intraday"]) == "xgb_only")

    if tier == "tier1":
        if not deep_required:
            deep_out = {
                "pair": pair,
                "tier": tier,
                "policy_skip": True,
                "result": {
                    "swing_transformer": {
                        "path": str(swing_tf_out),
                        "action": "policy_skip",
                        "exists": _artifact_exists(swing_tf_out),
                    },
                    "intraday_tcn": {
                        "path": str(intraday_tcn_out),
                        "action": "policy_skip",
                        "exists": _artifact_exists(intraday_tcn_out),
                    },
                },
            }
        elif bool(args.force_retrain):
            from fxstack.tasks import train_intraday_tcn_task, train_swing_transformer_task

            swing_deep = train_swing_transformer_task(
                pair=pair,
                timeframe=swing_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(swing_tf_out),
            )
            intraday_deep = train_intraday_tcn_task(
                pair=pair,
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(intraday_tcn_out),
            )
            deep_out = {
                "pair": pair,
                "stale_hours": float(args.deep_stale_hours),
                "min_new_rows": int(s.deep_retrain_min_new_rows),
                "result": {
                    "swing_transformer": {**swing_deep, "action": "forced_retrain"},
                    "intraday_tcn": {**intraday_deep, "action": "forced_retrain"},
                },
            }
        elif bool(args.lifecycle_only):
            _require_existing_artifact(swing_tf_out, label="swing_transformer")
            _require_existing_artifact(intraday_tcn_out, label="intraday_tcn")
            deep_out = {
                "pair": pair,
                "tier": tier,
                "result": {
                    "swing_transformer": {
                        "path": str(swing_tf_out),
                        "action": "lifecycle_only_reuse",
                        "exists": True,
                        "new_rows": 0,
                    },
                    "intraday_tcn": {
                        "path": str(intraday_tcn_out),
                        "action": "lifecycle_only_reuse",
                        "exists": True,
                        "new_rows": 0,
                    },
                },
            }
        else:
            deep_out = train_deep_stale_task(
                pair=pair,
                swing_timeframe=swing_timeframe,
                intraday_timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                artifact_root=str(artifact_root),
                stale_hours=float(args.deep_stale_hours),
            )
        if deep_required:
            if not _artifact_exists(swing_tf_out):
                raise SystemExit(f"missing swing transformer artifact for tier1 pair {pair}: {swing_tf_out}")
            if not _artifact_exists(intraday_tcn_out):
                raise SystemExit(f"missing intraday tcn artifact for tier1 pair {pair}: {intraday_tcn_out}")
    else:
        deep_out = {
            "pair": pair,
            "tier": tier,
            "result": {
                "swing_transformer": {
                    "path": str(swing_tf_out),
                    "action": "tier2_optional_skip",
                    "exists": _artifact_exists(swing_tf_out),
                },
                "intraday_tcn": {
                    "path": str(intraday_tcn_out),
                    "action": "tier2_optional_skip",
                    "exists": _artifact_exists(intraday_tcn_out),
                },
            },
        }

    if bool(getattr(args, "with_patchtst", False)):
        swing_patch_decision = artifact_retrain_decision(
            dataset=swing_labels,
            artifact_path=swing_patchtst_out,
            min_new_rows=max(1, int(s.deep_retrain_min_new_rows)),
        )
        intraday_patch_decision = artifact_retrain_decision(
            dataset=intraday_labels,
            artifact_path=intraday_patchtst_out,
            min_new_rows=max(1, int(s.deep_retrain_min_new_rows)),
        )
        if bool(args.force_retrain) or bool(swing_retrained) or bool(swing_patch_decision["should_retrain"]):
            r_swing_patchtst = train_swing_patchtst_task(
                pair=pair,
                timeframe=swing_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(swing_patchtst_out),
            )
        else:
            r_swing_patchtst = _reuse_result(
                swing_patchtst_out,
                model="swing_patchtst",
                report_path=swing_patchtst_report,
            )
        if bool(args.force_retrain) or bool(intraday_retrained) or bool(intraday_patch_decision["should_retrain"]):
            r_intraday_patchtst = train_intraday_patchtst_task(
                pair=pair,
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                label_root=args.label_root,
                out=str(intraday_patchtst_out),
            )
        else:
            r_intraday_patchtst = _reuse_result(
                intraday_patchtst_out,
                model="intraday_patchtst",
                report_path=intraday_patchtst_report,
            )
    else:
        r_swing_patchtst = {"model": "swing_patchtst", "path": str(swing_patchtst_out), "action": "disabled"}
        r_intraday_patchtst = {"model": "intraday_patchtst", "path": str(intraday_patchtst_out), "action": "disabled"}

    bundle_run_id = str(uuid.uuid4())
    run_id = bundle_run_id
    timeframes = {
        "regime": regime_timeframe,
        "swing": swing_timeframe,
        "intraday": intraday_timeframe,
    }
    feature_schema = {
        "version": 2,
        "pair": pair,
        "tier": tier,
        "training_cfg": training_cfg,
        "swing_policy": str(policies["swing"]),
        "intraday_policy": str(policies["intraday"]),
        "intraday_contract": "hierarchical_v1",
        "belief_contract": "directional_belief_v2",
        "belief_horizons_bars": {
            "short": int(s.belief_short_horizon_bars),
            "trade": int(s.belief_trade_horizon_bars),
            "structural": int(s.belief_structural_horizon_bars),
        },
        "belief_scenarios": [
            "trend_pullback",
            "range_mean_reversion",
            "breakout_expansion",
            "failed_breakout_reversal",
        ],
    }
    provider = s.normalized_data_provider
    raw_paths = [
        raw_root / f"provider={provider}" / f"pair={pair}" / f"timeframe={regime_timeframe}",
        raw_root / f"provider={provider}" / f"pair={pair}" / f"timeframe={swing_timeframe}",
        raw_root / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
    ]
    feature_paths = [
        Path(args.feature_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={regime_timeframe}",
        Path(args.feature_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={swing_timeframe}",
        Path(args.feature_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
    ]
    label_paths = [
        Path(args.label_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={swing_timeframe}",
        Path(args.label_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / "meta" / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / "exit" / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / "reversal" / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
    ]
    lineage = compute_lineage_snapshot(
        raw_paths=[p for p in raw_paths if p.exists()],
        feature_paths=[p for p in feature_paths if p.exists()],
        label_paths=[p for p in label_paths if p.exists()],
        feature_schema=feature_schema,
        label_config=labeling_cfg,
        risk_config={
            "promotion_policy": str(s.promotion_policy),
            "promotion_min_cv_score": float(s.promotion_min_cv_score),
            "promotion_min_wf_score": float(s.promotion_min_wf_score),
            "promotion_max_calibration_error": float(s.promotion_max_calibration_error),
            "promotion_min_delta": float(s.promotion_min_delta),
        },
        training_config=training_cfg,
        pair=pair,
        timeframes=timeframes,
        project_root=s.project_root,
    )
    fp = str(lineage.dataset_fingerprint)

    lifecycle_complete = bool(_artifact_exists(exit_out) and _artifact_exists(reversal_failure_out) and _artifact_exists(reversal_opportunity_out))
    if tier == "tier1" and not lifecycle_complete:
        raise SystemExit(f"tier1 pair {pair} is missing lifecycle artifacts after training")

    component_promotion_status = {
        "meta": str(r_meta.get("promotion_status", "")),
        "exit": str(r_exit.get("promotion_status", "")),
        "reversal_failure": str((r_reversal.get("failure_model") or {}).get("promotion_status", "")),
        "reversal_opportunity": str((r_reversal.get("opportunity_model") or {}).get("promotion_status", "")),
        "swing_patchtst": str(r_swing_patchtst.get("promotion_status", "")),
        "intraday_patchtst": str(r_intraday_patchtst.get("promotion_status", "")),
    }
    promotion_status = _aggregate_promotion_status(
        tier=tier,
        lifecycle_complete=lifecycle_complete,
        component_statuses=component_promotion_status,
    )
    intraday_meta = _read_meta_json(intraday_out)
    trained_at = intraday_meta.get("trained_at")
    data_window_end = intraday_meta.get("data_window_end")
    if bool(args.with_belief) and (not bool(args.lifecycle_only) or _artifact_exists(belief_out)):
        if bool(args.lifecycle_only):
            r_belief = _reuse_result(belief_out, model="directional_belief")
        else:
            r_belief = train_belief_task(
                timeframe=intraday_timeframe,
                feature_root=args.feature_root,
                out=str(belief_out),
                pairs=list(s.pairs),
            )
    else:
        r_belief = {"model": "directional_belief", "rows": 0, "path": str(belief_out), "action": "disabled"}
    training_window_summary = {
        "regime": _artifact_training_summary(regime_out),
        "swing_xgb": _artifact_training_summary(swing_out),
        "intraday_xgb": _artifact_training_summary(intraday_out),
        "meta": _artifact_training_summary(meta_out),
        "exit_policy": _artifact_training_summary(exit_out),
        "reversal_failure": _artifact_training_summary(reversal_failure_out),
        "reversal_opportunity": _artifact_training_summary(reversal_opportunity_out),
    }
    if _artifact_exists(swing_tf_out):
        training_window_summary["swing_transformer"] = _artifact_training_summary(swing_tf_out)
    if _artifact_exists(swing_patchtst_out):
        training_window_summary["swing_patchtst"] = _artifact_training_summary(swing_patchtst_out)
    if _artifact_exists(intraday_tcn_out):
        training_window_summary["intraday_tcn"] = _artifact_training_summary(intraday_tcn_out)
    if _artifact_exists(intraday_patchtst_out):
        training_window_summary["intraday_patchtst"] = _artifact_training_summary(intraday_patchtst_out)
    if _artifact_exists(belief_out):
        training_window_summary["directional_belief"] = _artifact_training_summary(belief_out)
    capabilities = {
        "has_exit_model": _artifact_exists(exit_out),
        "has_reversal_models": bool(_artifact_exists(reversal_failure_out) and _artifact_exists(reversal_opportunity_out)),
        "lifecycle_complete": lifecycle_complete,
        "has_directional_belief": _artifact_exists(belief_out),
        "has_sequence_challengers": bool(_artifact_exists(swing_patchtst_out) or _artifact_exists(intraday_patchtst_out)),
    }
    artifact_map = {
        "regime": _artifact_entry(result=r_regime, fallback_path=regime_out, fallback_model="regime_hmm"),
        "meta": _artifact_entry(result=r_meta, fallback_path=meta_out, fallback_model="meta_filter"),
        "swing_transformer": _artifact_entry(result={}, fallback_path=swing_tf_out, fallback_model="swing_transformer"),
        "swing_xgb": _artifact_entry(result=r_swing, fallback_path=swing_out, fallback_model="swing_xgb"),
        "intraday_tcn": _artifact_entry(result={}, fallback_path=intraday_tcn_out, fallback_model="intraday_tcn"),
        "intraday_xgb": _artifact_entry(result=r_intraday, fallback_path=intraday_out, fallback_model="intraday_xgb"),
        "directional_belief": _artifact_entry(result=r_belief, fallback_path=belief_out, fallback_model="directional_belief"),
        "exit_policy": _artifact_entry(result=r_exit, fallback_path=exit_out, fallback_model="exit_policy_xgb"),
        "reversal_failure": _artifact_entry(
            result=(r_reversal.get("failure_model") or {}),
            fallback_path=reversal_failure_out,
            fallback_model="reversal_failure_xgb",
        ),
        "reversal_opportunity": _artifact_entry(
            result=(r_reversal.get("opportunity_model") or {}),
            fallback_path=reversal_opportunity_out,
            fallback_model="reversal_opportunity_xgb",
        ),
        "swing": _artifact_entry(result=r_swing, fallback_path=swing_out, fallback_model="swing_xgb"),
        "intraday": _artifact_entry(result=r_intraday, fallback_path=intraday_out, fallback_model="intraday_xgb"),
    }
    if bool(getattr(args, "with_patchtst", False)) or _artifact_exists(swing_patchtst_out):
        artifact_map["swing_patchtst"] = _artifact_entry(
            result=r_swing_patchtst,
            fallback_path=swing_patchtst_out,
            fallback_model="swing_patchtst",
        )
    if bool(getattr(args, "with_patchtst", False)) or _artifact_exists(intraday_patchtst_out):
        artifact_map["intraday_patchtst"] = _artifact_entry(
            result=r_intraday_patchtst,
            fallback_path=intraday_patchtst_out,
            fallback_model="intraday_patchtst",
        )
    training_eval_reports = {
        "meta": str(r_meta.get("report_path") or meta_report),
        "exit": str(r_exit.get("report_path") or exit_report),
        "reversal_failure": str((r_reversal.get("failure_model") or {}).get("report_path") or reversal_failure_report),
        "reversal_opportunity": str((r_reversal.get("opportunity_model") or {}).get("report_path") or reversal_opportunity_report),
        "swing_patchtst": str(r_swing_patchtst.get("report_path") or swing_patchtst_report),
        "intraday_patchtst": str(r_intraday_patchtst.get("report_path") or intraday_patchtst_report),
    }
    reports_root = pair_root / "reports"
    feature_schema_path = _write_json(reports_root / "feature_schema.json", feature_schema)
    lineage_path = _write_json(reports_root / "lineage.json", lineage.to_dict())
    backtest_summary = _synthesize_backtest_summary(
        pair=pair,
        tier=tier,
        promotion_status=promotion_status,
        training_window_summary=training_window_summary,
        component_promotion_status=component_promotion_status,
        capabilities=capabilities,
        policies=policies,
        deep_out=deep_out,
    )
    backtest_summary_path = _write_json(reports_root / "backtest_summary.json", backtest_summary)
    phase3_evidence_refs = _build_phase3_evidence(
        pair=pair,
        reports_root=reports_root,
        lineage=lineage,
        intraday_timeframe=intraday_timeframe,
        backtest_summary=backtest_summary,
    )

    component_refs: dict[str, ModelVersionRef] = {}
    mlflow_component_runs: dict[str, str] = {}
    component_specs = _artifact_component_specs(pair=pair, artifact_map=artifact_map, timeframes=timeframes)
    for component_key, artifact_path, timeframe in component_specs:
        model_family = str(COMPONENT_FAMILIES.get(component_key) or component_key)
        window_summary = dict(training_window_summary.get(component_key) or {})
        train_end = str(window_summary.get("end_ts") or data_window_end or "").replace(":", "-").replace("+00:00", "Z") or "latest"
        training_window_tag = (
            f"{str(window_summary.get('start_ts') or '')}->{str(window_summary.get('end_ts') or '')}"
            if window_summary
            else ""
        )
        component_status = str(component_promotion_status.get(component_key) or promotion_status or "")
        run_tags = build_standard_run_tags(
            git_sha=str(lineage.git_sha),
            experiment_family=model_family,
            pair=pair,
            timeframe=timeframe,
            training_window=training_window_tag,
            validation_window="purged_cv+walk_forward",
            feature_service_version=str(lineage.feature_service_version),
            label_version=str(lineage.label_version),
            risk_config_version=str(lineage.risk_config_version),
            model_family=model_family,
            hyperparameter_profile="default",
            hardware_profile="gpu_required" if bool(s.require_cuda) else "cpu_allowed",
            activation_candidate=component_status,
            bundle_run_id=bundle_run_id,
            extra={
                "fxstack.dataset_fingerprint": fp,
                "fxstack.feature_schema_version": str(feature_schema.get("version") or ""),
            },
        )
        with MlflowRunContext(
            experiment_name=experiment_name_for_component(family=model_family, pair=pair, timeframe=timeframe),
            run_name=f"{model_family}/{pair}/{timeframe}/{train_end}",
            tags=run_tags,
            lineage=lineage,
            enabled=bool(s.mlflow_enabled),
        ) as run:
            run.log_params(
                {
                    "pair": pair,
                    "timeframe": timeframe,
                    "bundle_run_id": bundle_run_id,
                    "dataset_fingerprint": fp,
                    "feature_service_version": str(lineage.feature_service_version),
                    "label_version": str(lineage.label_version),
                    "risk_config_version": str(lineage.risk_config_version),
                    "policy_version": str(s.policy_version),
                    "promotion_status": component_status,
                }
            )
            _log_component_evidence(
                run=run,
                artifact_path=artifact_path,
                feature_schema=feature_schema,
                lineage=lineage,
                backtest_summary_path=backtest_summary_path,
            )
            ref = register_component_version(
                run=run,
                component_key=component_key,
                pair=pair,
                timeframe=timeframe,
                artifact_path=artifact_path,
                lineage=lineage,
                bundle_run_id=bundle_run_id,
                intended_alias="shadow",
                runtime_compatible=True,
                evidence_refs={
                    "artifact_path": str(artifact_path),
                    "meta": str(artifact_path / "meta.json"),
                    "training_report": str(artifact_path / "reports" / "training_report.json"),
                    "promotion_decision": str(artifact_path / "reports" / "promotion_decision.json"),
                    "feature_schema": str(feature_schema_path),
                    "lineage": str(lineage_path),
                    "backtest_summary": str(backtest_summary_path),
                    **dict(phase3_evidence_refs),
                },
                extra_tags={
                    "fxstack.promotion_status": component_status,
                    "fxstack.lifecycle_complete": "1" if lifecycle_complete else "0",
                },
            )
            component_refs[component_key] = ref
            if run.run_id:
                mlflow_component_runs[component_key] = str(run.run_id)
            enriched = {
                **dict(artifact_map.get(component_key) or {}),
                **ref.to_dict(),
                "path": str(artifact_path),
            }
            artifact_map[component_key] = enriched

    if "swing_xgb" in artifact_map:
        artifact_map["swing"] = dict(artifact_map["swing_xgb"])
    if "intraday_xgb" in artifact_map:
        artifact_map["intraday"] = dict(artifact_map["intraday_xgb"])

    component_columns = {
        component_key: list(_read_meta_json(Path(str((artifact_map.get(component_key) or {}).get("path") or ""))).get("feature_columns") or [])
        for component_key in component_refs.keys()
    }
    feature_repo_compaction = compact_feature_repo_for_pair(
        pair=pair,
        feature_root=args.feature_root,
        timeframes=[intraday_timeframe, regime_timeframe, swing_timeframe],
    )
    services_manifest_payload = feature_repo_manifest(pair=pair, component_columns=component_columns)
    services_manifest_file = _write_json(feature_repo_manifest_path(), services_manifest_payload)

    bundle_manifest = BundleManifest(
        bundle_run_id=bundle_run_id,
        pair=pair,
        tier=tier,
        dataset_fingerprint=fp,
        feature_service_version=str(lineage.feature_service_version),
        label_version=str(lineage.label_version),
        risk_config_version=str(lineage.risk_config_version),
        promotion_status=promotion_status,
        intended_alias="shadow",
        training_window_summary=training_window_summary,
        feature_schema=feature_schema,
        policies=policies,
        capabilities=capabilities,
        lifecycle_complete=lifecycle_complete,
        training_config=training_cfg,
        promotion_components=component_promotion_status,
        training_eval_reports=training_eval_reports,
        deep_stale=deep_out,
        new_rows_since_champion={
            "intraday_xgb": int(intraday_decision["new_rows"]),
            "meta": int(meta_decision["new_rows"]),
            "exit": int(exit_decision["new_rows"]),
            "reversal": int(reversal_decision["new_rows"]),
            "deep": {
                "swing_transformer": int(((deep_out.get("result") or {}).get("swing_transformer") or {}).get("new_rows", 0) or 0),
                "intraday_tcn": int(((deep_out.get("result") or {}).get("intraday_tcn") or {}).get("new_rows", 0) or 0),
            },
            "patchtst": {
                "swing_patchtst": int(((r_swing_patchtst or {}).get("rows", 0) or 0)),
                "intraday_patchtst": int(((r_intraday_patchtst or {}).get("rows", 0) or 0)),
            },
        },
        new_lifecycle_events_since_champion={
            "exit": int(exit_decision["new_rows"]),
            "reversal": int(reversal_decision["new_rows"]),
        },
        drift_flags={},
        live_shadow_summary={},
        timeframes=timeframes,
        components=component_refs,
        mlflow={
            "enabled": bool(s.mlflow_enabled),
            "tracking_uri": str(s.mlflow_tracking_uri),
            "registry_uri": str(s.mlflow_registry_uri or s.mlflow_tracking_uri),
            "component_runs": mlflow_component_runs,
            "component_versions": {key: ref.to_dict() for key, ref in component_refs.items()},
        },
        metadata={
            "trained_at": trained_at,
            "data_window_end": data_window_end,
            "edge_formula_id": "live_scorer_v2",
            "policy_version": str(s.policy_version),
            "runtime_compatible": True,
            "feature_repo_manifest": str(services_manifest_file),
            "feature_repo_compaction": dict(feature_repo_compaction),
            "phase3_execution_required": True,
            "phase3_evidence": dict(phase3_evidence_refs),
            "phase5_gates": dict(phase5_evidence_refs),
            "phase4_shadow_only": True,
            "phase4_sequence_dataset_manifests": {
                "swing_patchtst": str(r_swing_patchtst.get("sequence_dataset_manifest") or ""),
                "intraday_patchtst": str(r_intraday_patchtst.get("sequence_dataset_manifest") or ""),
            },
            "phase4_portfolio_reports": {
                "swing_patchtst": str(r_swing_patchtst.get("portfolio_report") or ""),
                "intraday_patchtst": str(r_intraday_patchtst.get("portfolio_report") or ""),
            },
            "phase4_challenger_reports": {
                "swing_patchtst": str(r_swing_patchtst.get("challenger_head_to_head") or ""),
                "intraday_patchtst": str(r_intraday_patchtst.get("challenger_head_to_head") or ""),
            },
        },
    )
    model_manifest_path = _write_json(reports_root / "model_manifest.json", bundle_manifest.to_dict())
    phase5_bundle = build_phase5_gate_bundle(
        pair=pair,
        reports_root=reports_root,
        backtest_summary=backtest_summary,
        promotion_status=promotion_status,
        training_window_summary=training_window_summary,
        capabilities=capabilities,
        training_eval_reports=training_eval_reports,
        phase3_evidence_refs=phase3_evidence_refs,
        feature_schema_path=feature_schema_path,
        lineage_path=lineage_path,
        model_manifest_path=model_manifest_path,
        backtest_summary_path=backtest_summary_path,
        stress_summary_path=phase3_evidence_refs.get("stress_harness_summary"),
        harness_comparison_path=phase3_evidence_refs.get("harness_comparison"),
        execution_metrics_path=phase3_evidence_refs.get("execution_metrics"),
        risk_trace_schema_path=phase3_evidence_refs.get("risk_trace_schema"),
        phase3_execution_required=True,
        phase4_shadow_only=True,
        phase4_sequence_dataset_manifests={
            "swing_patchtst": str(r_swing_patchtst.get("sequence_dataset_manifest") or ""),
            "intraday_patchtst": str(r_intraday_patchtst.get("sequence_dataset_manifest") or ""),
        },
        phase4_portfolio_reports={
            "swing_patchtst": str(r_swing_patchtst.get("portfolio_report") or ""),
            "intraday_patchtst": str(r_intraday_patchtst.get("portfolio_report") or ""),
        },
        phase4_challenger_reports={
            "swing_patchtst": str(r_swing_patchtst.get("challenger_head_to_head") or ""),
            "intraday_patchtst": str(r_intraday_patchtst.get("challenger_head_to_head") or ""),
        },
    )
    phase5_evidence_refs = write_phase5_gate_bundle(phase5_bundle, reports_root=reports_root)
    if bool(s.mlflow_enabled) and mlflow_component_runs:
        from fxstack.mlops.run_context import configure_mlflow

        client = configure_mlflow().tracking.MlflowClient()
        for run_id_txt in mlflow_component_runs.values():
            try:
                client.log_artifact(run_id_txt, str(model_manifest_path))
            except Exception:
                continue
    for ref in component_refs.values():
        ref.evidence_refs["model_manifest"] = str(model_manifest_path)
    for component_key, ref in component_refs.items():
        artifact_map[component_key]["evidence_refs"] = dict(ref.evidence_refs)
        artifact_map[component_key]["bundle_run_id"] = bundle_run_id
        artifact_map[component_key]["dataset_fingerprint"] = fp
    if "swing_xgb" in artifact_map:
        artifact_map["swing"] = dict(artifact_map["swing_xgb"])
    if "intraday_xgb" in artifact_map:
        artifact_map["intraday"] = dict(artifact_map["intraday_xgb"])

    reg = ArtifactRegistry(Path(args.registry_root))
    path = reg.register(
        name=f"{pair.lower()}_{run_id}",
        metadata={
            "run_id": run_id,
            "bundle_run_id": bundle_run_id,
            "dataset_fingerprint": fp,
            "feature_service_version": str(lineage.feature_service_version),
            "label_version": str(lineage.label_version),
            "risk_config_version": str(lineage.risk_config_version),
            "feature_schema": feature_schema,
            "pair": pair,
            "tier": tier,
            "trained_at": trained_at,
            "data_window_end": data_window_end,
            "training_window_summary": training_window_summary,
            "promotion_status": promotion_status,
            "intraday_contract": "hierarchical_v1",
            "artifacts": artifact_map,
            "policies": policies,
            "deep_stale": deep_out,
            "training_config": training_cfg,
            "capabilities": capabilities,
            "lifecycle_complete": lifecycle_complete,
            "edge_formula_id": "live_scorer_v2",
            "policy_version": str(s.policy_version),
            "new_rows_since_champion": {
                "intraday_xgb": int(intraday_decision["new_rows"]),
                "meta": int(meta_decision["new_rows"]),
                "exit": int(exit_decision["new_rows"]),
                "reversal": int(reversal_decision["new_rows"]),
                "deep": {
                    "swing_transformer": int(((deep_out.get("result") or {}).get("swing_transformer") or {}).get("new_rows", 0) or 0),
                    "intraday_tcn": int(((deep_out.get("result") or {}).get("intraday_tcn") or {}).get("new_rows", 0) or 0),
                },
                "patchtst": {
                    "swing_patchtst": int(((r_swing_patchtst or {}).get("rows", 0) or 0)),
                    "intraday_patchtst": int(((r_intraday_patchtst or {}).get("rows", 0) or 0)),
                },
            },
            "new_lifecycle_events_since_champion": {
                "exit": int(exit_decision["new_rows"]),
                "reversal": int(reversal_decision["new_rows"]),
            },
            "drift_flags": {},
            "live_shadow_summary": {},
            "promotion_components": component_promotion_status,
            "training_eval_reports": training_eval_reports,
            "timeframes": timeframes,
            "intended_alias": "shadow",
            "runtime_compatible": True,
            "backtest_summary": str(backtest_summary_path),
            "model_manifest": str(model_manifest_path),
            "lineage": str(lineage_path),
            "feature_repo_manifest": str(services_manifest_file),
            "feature_repo_compaction": dict(feature_repo_compaction),
            "phase3_execution_required": True,
            "phase3_evidence": dict(phase3_evidence_refs),
            "phase5_gates": dict(phase5_evidence_refs),
            "phase4_shadow_only": True,
            "phase4_sequence_dataset_manifests": {
                "swing_patchtst": str(r_swing_patchtst.get("sequence_dataset_manifest") or ""),
                "intraday_patchtst": str(r_intraday_patchtst.get("sequence_dataset_manifest") or ""),
            },
            "phase4_portfolio_reports": {
                "swing_patchtst": str(r_swing_patchtst.get("portfolio_report") or ""),
                "intraday_patchtst": str(r_intraday_patchtst.get("portfolio_report") or ""),
            },
            "phase4_challenger_reports": {
                "swing_patchtst": str(r_swing_patchtst.get("challenger_head_to_head") or ""),
                "intraday_patchtst": str(r_intraday_patchtst.get("challenger_head_to_head") or ""),
            },
            "mlflow": dict(bundle_manifest.mlflow),
        },
    )

    print(
        {
            "run_id": run_id,
            "bundle_run_id": bundle_run_id,
            "dataset_fingerprint": fp,
            "registry_path": str(path),
            "tier": tier,
            "policies": policies,
            "promotion_status": promotion_status,
            "artifacts": artifact_map,
            "mlflow": dict(bundle_manifest.mlflow),
            "deep_stale": deep_out,
            "lifecycle": {
                "exit": r_exit,
                "reversal": r_reversal,
            },
        }
    )


if __name__ == "__main__":
    main()
