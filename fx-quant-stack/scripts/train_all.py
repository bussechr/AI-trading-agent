from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from typing import Any

import yaml

from fxstack.io.parquet_store import ParquetStore
from fxstack.settings import get_settings
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
    train_exit_task,
    train_intraday_task,
    train_meta_task,
    train_regime_task,
    train_reversal_task,
    train_swing_task,
)
from fxstack.training.fingerprint import dataset_fingerprint
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
    import json

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
    intraday_out = pair_root / "intraday_xgb"
    intraday_tcn_out = pair_root / "intraday_tcn"
    meta_out = pair_root / "meta_filter"
    exit_out = pair_root / "exit_policy_xgb"
    reversal_failure_out = pair_root / "reversal_failure_xgb"
    reversal_opportunity_out = pair_root / "reversal_opportunity_xgb"
    meta_report = _report_path_for_artifact(meta_out)
    exit_report = _report_path_for_artifact(exit_out)
    reversal_failure_report = _report_path_for_artifact(reversal_failure_out)
    reversal_opportunity_report = _report_path_for_artifact(reversal_opportunity_out)

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

    run_id = str(uuid.uuid4())
    feature_schema = {
        "version": 2,
        "pair": pair,
        "tier": tier,
        "training_cfg": training_cfg,
        "swing_policy": str(policies["swing"]),
        "intraday_policy": str(policies["intraday"]),
        "intraday_contract": "hierarchical_v1",
    }
    provider = s.normalized_data_provider
    fingerprint_paths = [
        Path(args.feature_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={regime_timeframe}",
        Path(args.feature_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={swing_timeframe}",
        Path(args.feature_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={swing_timeframe}",
        Path(args.label_root) / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / "meta" / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / "exit" / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
        Path(args.label_root) / "reversal" / f"provider={provider}" / f"pair={pair}" / f"timeframe={intraday_timeframe}",
    ]
    fp = dataset_fingerprint(
        data_paths=[p for p in fingerprint_paths if p.exists()],
        feature_schema=feature_schema,
        run_id=run_id,
    )

    lifecycle_complete = bool(_artifact_exists(exit_out) and _artifact_exists(reversal_failure_out) and _artifact_exists(reversal_opportunity_out))
    if tier == "tier1" and not lifecycle_complete:
        raise SystemExit(f"tier1 pair {pair} is missing lifecycle artifacts after training")

    component_promotion_status = {
        "meta": str(r_meta.get("promotion_status", "")),
        "exit": str(r_exit.get("promotion_status", "")),
        "reversal_failure": str((r_reversal.get("failure_model") or {}).get("promotion_status", "")),
        "reversal_opportunity": str((r_reversal.get("opportunity_model") or {}).get("promotion_status", "")),
    }
    promotion_status = _aggregate_promotion_status(
        tier=tier,
        lifecycle_complete=lifecycle_complete,
        component_statuses=component_promotion_status,
    )
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
    if _artifact_exists(intraday_tcn_out):
        training_window_summary["intraday_tcn"] = _artifact_training_summary(intraday_tcn_out)

    intraday_meta = _read_meta_json(intraday_out)
    trained_at = intraday_meta.get("trained_at")
    data_window_end = intraday_meta.get("data_window_end")
    capabilities = {
        "has_exit_model": _artifact_exists(exit_out),
        "has_reversal_models": bool(_artifact_exists(reversal_failure_out) and _artifact_exists(reversal_opportunity_out)),
        "lifecycle_complete": lifecycle_complete,
    }
    artifact_map = {
        "regime": {"path": str(regime_out), "model": str(r_regime.get("model", "regime_hmm"))},
        "meta": {"path": str(meta_out), "model": str(r_meta.get("model", "meta_filter"))},
        "swing_transformer": {
            "path": str(swing_tf_out) if _artifact_exists(swing_tf_out) else "",
            "model": "swing_transformer",
        },
        "swing_xgb": {"path": str(swing_out), "model": str(r_swing.get("model", "swing_xgb"))},
        "intraday_tcn": {
            "path": str(intraday_tcn_out) if _artifact_exists(intraday_tcn_out) else "",
            "model": "intraday_tcn",
        },
        "intraday_xgb": {"path": str(intraday_out), "model": str(r_intraday.get("model", "intraday_xgb"))},
        "exit_policy": {
            "path": str(exit_out) if _artifact_exists(exit_out) else "",
            "model": str(r_exit.get("model", "exit_policy_xgb")),
        },
        "reversal_failure": {
            "path": str(reversal_failure_out) if _artifact_exists(reversal_failure_out) else "",
            "model": "reversal_failure_xgb",
        },
        "reversal_opportunity": {
            "path": str(reversal_opportunity_out) if _artifact_exists(reversal_opportunity_out) else "",
            "model": "reversal_opportunity_xgb",
        },
        "swing": {"path": str(swing_out), "model": str(r_swing.get("model", "swing_xgb"))},
        "intraday": {"path": str(intraday_out), "model": str(r_intraday.get("model", "intraday_xgb"))},
    }
    training_eval_reports = {
        "meta": str(r_meta.get("report_path") or meta_report),
        "exit": str(r_exit.get("report_path") or exit_report),
        "reversal_failure": str((r_reversal.get("failure_model") or {}).get("report_path") or reversal_failure_report),
        "reversal_opportunity": str((r_reversal.get("opportunity_model") or {}).get("report_path") or reversal_opportunity_report),
    }

    reg = ArtifactRegistry(Path(args.registry_root))
    path = reg.register(
        name=f"{pair.lower()}_{run_id}",
        metadata={
            "run_id": run_id,
            "dataset_fingerprint": fp,
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
            },
            "new_lifecycle_events_since_champion": {
                "exit": int(exit_decision["new_rows"]),
                "reversal": int(reversal_decision["new_rows"]),
            },
            "drift_flags": {},
            "live_shadow_summary": {},
            "promotion_components": component_promotion_status,
            "training_eval_reports": training_eval_reports,
        },
    )

    print(
        {
            "run_id": run_id,
            "dataset_fingerprint": fp,
            "registry_path": str(path),
            "tier": tier,
            "policies": policies,
            "promotion_status": promotion_status,
            "artifacts": {
                "regime": str(regime_out),
                "swing_transformer": str(swing_tf_out) if _artifact_exists(swing_tf_out) else "",
                "swing_xgb": str(swing_out),
                "intraday_tcn": str(intraday_tcn_out) if _artifact_exists(intraday_tcn_out) else "",
                "intraday_xgb": str(intraday_out),
                "meta": str(meta_out),
                "exit_policy": str(exit_out) if _artifact_exists(exit_out) else "",
                "reversal_failure": str(reversal_failure_out) if _artifact_exists(reversal_failure_out) else "",
                "reversal_opportunity": str(reversal_opportunity_out) if _artifact_exists(reversal_opportunity_out) else "",
            },
            "deep_stale": deep_out,
            "lifecycle": {
                "exit": r_exit,
                "reversal": r_reversal,
            },
        }
    )


if __name__ == "__main__":
    main()
