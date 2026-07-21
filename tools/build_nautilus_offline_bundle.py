from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SOURCE = REPOSITORY_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SOURCE) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SOURCE))

from fxstack.backtest.harness.nautilus_offline_bundle import (  # noqa: E402
    OfflineBundleBuildConfig,
    OfflineBundleError,
    _normalize_scorer_config,
    build_offline_bundle,
    file_sha256,
    read_json_object,
)


def _scorer_settings(path: Path) -> dict[str, Any]:
    payload = read_json_object(path, label="offline scorer config input")
    settings = dict(payload.get("settings") or {}) if "settings" in payload else payload
    return _normalize_scorer_config(settings)


def _csv_values(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.strip().upper()
            for item in str(value or "").split(",")
            if item.strip()
        )
    )


def build_plan(
    *,
    repository_root: Path,
    active_manifest: Path,
    raw_store_root: Path,
    destination: Path,
    pair: str,
    provider: str,
    all_pairs: tuple[str, ...],
    replay_start: str,
    replay_end: str,
    scorer_config_source: str,
    scorer_settings: Mapping[str, Any],
    protected_roots: Sequence[Path],
) -> dict[str, Any]:
    return {
        "status": "planned",
        "execute": False,
        "authority": {
            "advisory_only": True,
            "activation_capability": False,
            "database_capability": False,
            "broker_capability": False,
            "network_capability": False,
        },
        "source": {
            "repository_root": str(repository_root.resolve()),
            "active_manifest": str(active_manifest.resolve()),
            "active_manifest_file_sha256": file_sha256(active_manifest),
            "raw_store_root": str(raw_store_root.resolve()),
            "read_only": True,
        },
        "destination": {
            "path": str(destination.resolve()),
            "must_be_fresh": True,
            "outside_source_and_install_roots": True,
            "protected_roots": [str(path.resolve()) for path in protected_roots],
        },
        "replay": {
            "pair": str(pair).upper(),
            "provider": str(provider).lower(),
            "all_pairs": list(all_pairs),
            "anchor_timeframe": "M5",
            "context_timeframes": ["M15", "H1", "H4", "D"],
            "requested_start": str(replay_start),
            "requested_end": str(replay_end),
            "strictly_post_training": True,
        },
        "scoring": {
            "config_input": str(scorer_config_source),
            "config_fields": sorted(scorer_settings),
            "production_scorer": "fxstack.live.scorer.LiveScorer.score",
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a fresh physically isolated Nautilus replay bundle"
    )
    parser.add_argument("--repository-root", default=str(REPOSITORY_ROOT))
    parser.add_argument(
        "--active-manifest",
        default=str(REPOSITORY_ROOT / "fx-quant-stack" / "artifacts" / "active_models.json"),
    )
    parser.add_argument("--raw-store-root", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--pair", default="EURUSD")
    parser.add_argument("--provider", default="dukascopy")
    parser.add_argument("--all-pairs", required=True, help="Comma-separated causal context universe")
    parser.add_argument("--replay-start", default="")
    parser.add_argument("--replay-end", required=True)
    scorer_group = parser.add_mutually_exclusive_group(required=True)
    scorer_group.add_argument("--scorer-config")
    scorer_group.add_argument(
        "--scorer-config-json",
        help="Exact inline JSON settings object; avoids any environment-backed settings load",
    )
    parser.add_argument(
        "--protected-root",
        action="append",
        default=[],
        help="Additional DB/feature/registry/install root which destination must not overlap",
    )
    parser.add_argument("--execute", action=argparse.BooleanOptionalAction, default=False)
    args = parser.parse_args(list(argv) if argv is not None else None)
    repository_root = Path(args.repository_root).resolve()
    active_manifest = Path(args.active_manifest).resolve()
    raw_store_root = Path(args.raw_store_root).resolve()
    destination = Path(args.destination).resolve()
    protected_roots = tuple(Path(item).resolve() for item in args.protected_root)
    all_pairs = _csv_values(args.all_pairs)
    try:
        if str(args.scorer_config_json or "").strip():
            raw_settings = json.loads(str(args.scorer_config_json))
            if not isinstance(raw_settings, dict):
                raise OfflineBundleError("inline scorer config must be a JSON object")
            scorer_settings = _normalize_scorer_config(raw_settings)
            scorer_config_source = "inline-json"
        else:
            scorer_config_path = Path(args.scorer_config).resolve()
            scorer_settings = _scorer_settings(scorer_config_path)
            scorer_config_source = str(scorer_config_path)
        if not repository_root.is_dir() or not active_manifest.is_file() or not raw_store_root.is_dir():
            raise OfflineBundleError("repository, active manifest, and raw store must exist")
        if not all_pairs or str(args.pair).strip().upper() not in all_pairs:
            raise OfflineBundleError("all-pairs must include the requested pair")
        plan = build_plan(
            repository_root=repository_root,
            active_manifest=active_manifest,
            raw_store_root=raw_store_root,
            destination=destination,
            pair=str(args.pair),
            provider=str(args.provider),
            all_pairs=all_pairs,
            replay_start=str(args.replay_start),
            replay_end=str(args.replay_end),
            scorer_config_source=scorer_config_source,
            scorer_settings=scorer_settings,
            protected_roots=protected_roots,
        )
        if not args.execute:
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        manifest = build_offline_bundle(
            OfflineBundleBuildConfig(
                repository_root=repository_root,
                active_manifest_path=active_manifest,
                raw_store_root=raw_store_root,
                destination=destination,
                pair=str(args.pair),
                provider=str(args.provider),
                all_pairs=all_pairs,
                replay_start=str(args.replay_start),
                replay_end=str(args.replay_end),
                scorer_config=scorer_settings,
                protected_roots=protected_roots,
            )
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "authoritative": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(
        json.dumps(
            {
                "status": "completed",
                "authoritative": False,
                "advisory_only": True,
                "destination": str(destination),
                "bundle_payload_sha256": str(manifest.get("bundle_payload_sha256") or ""),
                "dataset_hash": str(dict(manifest.get("dataset") or {}).get("dataset_hash") or ""),
                "source_identity": dict(manifest.get("source_identity") or {}),
                "oos": dict(manifest.get("oos") or {}),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
