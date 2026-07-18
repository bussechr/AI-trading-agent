from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.io.parquet_store import ParquetStore  # noqa: E402


def _csv(value: str) -> list[str]:
    return [item.strip().upper() for item in str(value).split(",") if item.strip()]


def _utc(value: str) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _copy_scope(
    *,
    source: ParquetStore,
    destination: ParquetStore,
    provider: str,
    pair: str,
    timeframe: str,
    cutoff: pd.Timestamp,
) -> dict[str, Any]:
    frame = source.read_pair_timeframe(
        provider=provider,
        pair=pair,
        timeframe=timeframe,
    )
    if frame.empty:
        return {"pair": pair, "timeframe": timeframe, "rows": 0, "status": "missing"}
    timestamps = pd.to_datetime(frame["ts"], utc=True, errors="coerce")
    selected = frame.loc[timestamps.notna() & (timestamps <= cutoff)].copy()
    if selected.empty:
        return {"pair": pair, "timeframe": timeframe, "rows": 0, "status": "empty_before_cutoff"}
    selected["ts"] = pd.to_datetime(selected["ts"], utc=True)
    destination.replace_partitioned(
        selected,
        provider=provider,
        pair=pair,
        timeframe=timeframe,
    )
    return {
        "pair": pair,
        "timeframe": timeframe,
        "rows": int(len(selected)),
        "min_ts": str(selected["ts"].min()),
        "max_ts": str(selected["ts"].max()),
        "source_max_ts": str(pd.to_datetime(frame["ts"], utc=True, errors="coerce").max()),
        "status": "ok",
    }


def build_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    cutoff = _utc(args.train_end)
    test_start = _utc(args.test_start)
    if test_start <= cutoff:
        raise ValueError("test_start must be after train_end")
    output_root = Path(args.out_root).resolve()
    if output_root.exists() and any(output_root.iterdir()):
        raise FileExistsError(f"walk-forward output must be empty: {output_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    provider = str(args.provider).strip().lower()
    pairs = _csv(args.pairs)
    feature_timeframes = _csv(args.feature_timeframes)
    label_timeframes = _csv(args.label_timeframes)
    source_features = Path(args.feature_root).resolve()
    source_labels = Path(args.label_root).resolve()

    specs = [
        ("features", source_features, output_root / "features", feature_timeframes),
        ("labels", source_labels, output_root / "labels", label_timeframes),
        ("exit_labels", source_labels / "exit", output_root / "labels" / "exit", ["M5"]),
        ("reversal_labels", source_labels / "reversal", output_root / "labels" / "reversal", ["M5"]),
    ]
    results: dict[str, list[dict[str, Any]]] = {}
    for name, source_root, destination_root, timeframes in specs:
        source = ParquetStore(source_root)
        destination = ParquetStore(destination_root)
        rows: list[dict[str, Any]] = []
        for pair in pairs:
            for timeframe in timeframes:
                rows.append(
                    _copy_scope(
                        source=source,
                        destination=destination,
                        provider=provider,
                        pair=pair,
                        timeframe=timeframe,
                        cutoff=cutoff,
                    )
                )
        results[name] = rows

    manifest = {
        "version": "walk_forward_snapshot_v1",
        "causal_contract": {
            "future_data_access": "forbidden",
            "train_end_inclusive": str(cutoff),
            "test_start_inclusive": str(test_start),
            "embargo_seconds": float((test_start - cutoff).total_seconds()),
            "labels_after_train_end_included": False,
        },
        "provider": provider,
        "pairs": pairs,
        "source_roots": {
            "features": str(source_features),
            "labels": str(source_labels),
        },
        "output_root": str(output_root),
        "scopes": results,
    }
    manifest_path = output_root / "walk_forward_snapshot.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build a training-only parquet snapshot for causal walk-forward replay.")
    parser.add_argument("--feature-root", default="fx-quant-stack/data/features")
    parser.add_argument("--label-root", default="fx-quant-stack/data/labels")
    parser.add_argument("--out-root", required=True)
    parser.add_argument("--pairs", required=True)
    parser.add_argument("--provider", default="dukascopy")
    parser.add_argument("--feature-timeframes", default="M5,M15,H1,H4,D")
    parser.add_argument("--label-timeframes", default="M5,D")
    parser.add_argument("--train-end", required=True)
    parser.add_argument("--test-start", required=True)
    return parser


def main() -> int:
    manifest = build_snapshot(build_parser().parse_args())
    print(json.dumps(manifest["causal_contract"], indent=2, sort_keys=True))
    print(f"manifest={manifest['manifest_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
