from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import yaml

from fxstack.settings import get_settings
from fxstack.tasks import (
    train_deep_stale_task,
    train_intraday_task,
    train_meta_task,
    train_regime_task,
    train_swing_task,
)
from fxstack.training.fingerprint import dataset_fingerprint
from fxstack.training.registry import ArtifactRegistry


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return dict(yaml.safe_load(f) or {})


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
    ap.add_argument("--deep-stale-hours", type=float, default=float(s.deep_model_stale_hours))
    args = ap.parse_args()

    pair = str(args.pair).upper()
    artifact_root = Path(args.artifact_root)
    training_cfg = _load_yaml(Path(args.training_config))

    pair_root = artifact_root / pair.lower()
    regime_out = pair_root / "regime_hmm"
    swing_out = pair_root / "swing_xgb"
    intraday_out = pair_root / "intraday_xgb"
    meta_out = pair_root / "meta_filter"

    r_regime = train_regime_task(
        pair=pair,
        timeframe=str(args.regime_timeframe).upper(),
        feature_root=args.feature_root,
        out=str(regime_out),
    )
    r_swing = train_swing_task(
        pair=pair,
        timeframe=str(args.swing_timeframe).upper(),
        feature_root=args.feature_root,
        label_root=args.label_root,
        out=str(swing_out),
    )
    r_intraday = train_intraday_task(
        pair=pair,
        timeframe=str(args.intraday_timeframe).upper(),
        feature_root=args.feature_root,
        label_root=args.label_root,
        out=str(intraday_out),
    )
    r_meta = train_meta_task(
        pair=pair,
        timeframe=str(args.intraday_timeframe).upper(),
        feature_root=args.feature_root,
        out=str(meta_out),
    )
    deep_out = train_deep_stale_task(
        pair=pair,
        swing_timeframe=str(args.swing_timeframe).upper(),
        intraday_timeframe=str(args.intraday_timeframe).upper(),
        feature_root=args.feature_root,
        label_root=args.label_root,
        artifact_root=str(artifact_root),
        stale_hours=float(args.deep_stale_hours),
    )

    swing_tf_out = pair_root / "swing_transformer"
    intraday_tcn_out = pair_root / "intraday_tcn"
    if not (swing_tf_out / "meta.json").exists():
        raise SystemExit(f"missing swing transformer artifact for {pair}: {swing_tf_out}")
    if not (intraday_tcn_out / "meta.json").exists():
        raise SystemExit(f"missing intraday tcn artifact for {pair}: {intraday_tcn_out}")

    run_id = str(uuid.uuid4())
    fp = dataset_fingerprint(
        data_paths=[Path(args.feature_root), Path(args.label_root)],
        feature_schema={
            "version": 2,
            "pair": pair,
            "training_cfg": training_cfg,
            "swing_policy": str(s.swing_model_policy),
            "intraday_policy": str(s.intraday_model_policy),
        },
        run_id=run_id,
    )

    reg = ArtifactRegistry(Path(args.registry_root))
    path = reg.register(
        name=f"{pair.lower()}_{run_id}",
        metadata={
            "run_id": run_id,
            "dataset_fingerprint": fp,
            "pair": pair,
            "artifacts": {
                "regime": {"path": str(regime_out), "model": str(r_regime.get("model", "regime_hmm"))},
                "meta": {"path": str(meta_out), "model": str(r_meta.get("model", "meta_filter"))},
                "swing_transformer": {"path": str(swing_tf_out), "model": "swing_transformer"},
                "swing_xgb": {"path": str(swing_out), "model": str(r_swing.get("model", "swing_xgb"))},
                "intraday_tcn": {"path": str(intraday_tcn_out), "model": "intraday_tcn"},
                "intraday_xgb": {"path": str(intraday_out), "model": str(r_intraday.get("model", "intraday_xgb"))},
                # Compatibility aliases for older loaders.
                "swing": {"path": str(swing_out), "model": str(r_swing.get("model", "swing_xgb"))},
                "intraday": {"path": str(intraday_out), "model": str(r_intraday.get("model", "intraday_xgb"))},
            },
            "policies": {
                "swing": str(s.swing_model_policy),
                "intraday": str(s.intraday_model_policy),
            },
            "deep_stale": deep_out,
            "training_config": training_cfg,
        },
    )

    print(
        {
            "run_id": run_id,
            "dataset_fingerprint": fp,
            "registry_path": str(path),
            "artifacts": {
                "regime": str(regime_out),
                "swing_transformer": str(swing_tf_out),
                "swing_xgb": str(swing_out),
                "intraday_tcn": str(intraday_tcn_out),
                "intraday_xgb": str(intraday_out),
                "meta": str(meta_out),
            },
            "deep_stale": deep_out,
        }
    )


if __name__ == "__main__":
    main()
