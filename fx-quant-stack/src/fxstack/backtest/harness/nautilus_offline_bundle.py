from __future__ import annotations

import hashlib
import importlib.metadata
import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd


BUNDLE_SCHEMA = "fxstack_nautilus_offline_bundle_v1"
SCORER_CONFIG_SCHEMA = "fxstack_offline_scorer_config_v1"
REQUIRED_ENGINE_VERSION = "1.230.0"
CONTEXT_TIMEFRAMES = ("M15", "H1", "H4", "D")
DATA_FILES = (
    "data/causal_contract_rows.parquet",
    "data/bid_ask_bars.parquet",
)
SCORER_CONFIG_FIELDS = (
    "min_swing_prob",
    "min_entry_prob",
    "min_trade_prob",
    "max_allowed_spread_bps",
    "min_expected_edge_bps",
    "use_uncertainty_gate",
    "max_entry_uncertainty",
    "blocked_entry_sessions_csv",
    "strategy_engine_mode",
    "structure_timing_enabled",
    "structure_timing_rescue_min_score",
    "structure_timing_entry_rescue_margin",
    "structure_timing_max_chase_risk",
    "entry_hysteresis_margin_bps",
    "enable_pair_quality_prior",
    "tier1_pairs_csv",
)
SCORING_RUNTIME_PACKAGES = (
    "joblib",
    "nautilus_trader",
    "numpy",
    "pandas",
    "pyarrow",
    "scikit-learn",
    "xgboost",
)
SCORING_CODE_MODULES: dict[str, str] = {
    "fxstack.backtest.harness.nautilus_offline_adapter": (
        "fx-quant-stack/src/fxstack/backtest/harness/nautilus_offline_adapter.py"
    ),
    "fxstack.backtest.harness.nautilus_offline_bundle": (
        "fx-quant-stack/src/fxstack/backtest/harness/nautilus_offline_bundle.py"
    ),
    "fxstack.backtest.harness.nautilus_offline_engine": (
        "fx-quant-stack/src/fxstack/backtest/harness/nautilus_offline_engine.py"
    ),
    "fxstack.backtest.research_support": (
        "fx-quant-stack/src/fxstack/backtest/research_support.py"
    ),
    "fxstack.live.scorer": "fx-quant-stack/src/fxstack/live/scorer.py",
    "fxstack.live.policy": "fx-quant-stack/src/fxstack/live/policy.py",
    "fxstack.settings": "fx-quant-stack/src/fxstack/settings.py",
    "fxstack.models.artifact_contract": (
        "fx-quant-stack/src/fxstack/models/artifact_contract.py"
    ),
    "fxstack.models.regime_hmm": "fx-quant-stack/src/fxstack/models/regime_hmm.py",
    "fxstack.models.swing_xgb": "fx-quant-stack/src/fxstack/models/swing_xgb.py",
    "fxstack.models.intraday_xgb": "fx-quant-stack/src/fxstack/models/intraday_xgb.py",
    "fxstack.models.meta_filter": "fx-quant-stack/src/fxstack/models/meta_filter.py",
    "fxstack.mlops.local_artifact": "fx-quant-stack/src/fxstack/mlops/local_artifact.py",
    "fxstack.mlops.model_uri": "fx-quant-stack/src/fxstack/mlops/model_uri.py",
    "fxstack.features.session_contract": (
        "fx-quant-stack/src/fxstack/features/session_contract.py"
    ),
}


class OfflineBundleError(RuntimeError):
    pass


@dataclass(frozen=True)
class OfflineBundleBuildConfig:
    repository_root: Path
    active_manifest_path: Path
    raw_store_root: Path
    destination: Path
    pair: str
    provider: str
    all_pairs: tuple[str, ...]
    replay_end: str
    scorer_config: Mapping[str, Any]
    replay_start: str = ""
    anchor_timeframe: str = "M5"
    protected_roots: tuple[Path, ...] = ()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def read_json_object(path: str | Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OfflineBundleError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise OfflineBundleError(f"{label} must be a JSON object: {path}")
    return dict(value)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _require_nonoverlap(destination: Path, protected_roots: Iterable[Path]) -> None:
    resolved = destination.resolve()
    for raw_root in protected_roots:
        root = raw_root.resolve()
        if resolved == root or _is_within(resolved, root) or _is_within(root, resolved):
            raise OfflineBundleError(
                f"bundle destination overlaps a protected source/install root: {root}"
            )


def _normalize_timestamp(value: Any, *, label: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise OfflineBundleError(f"{label} is missing or invalid")
    return pd.Timestamp(parsed)


def _normalize_scorer_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    supplied = dict(raw or {})
    if set(supplied) != set(SCORER_CONFIG_FIELDS):
        missing = sorted(set(SCORER_CONFIG_FIELDS) - set(supplied))
        extra = sorted(set(supplied) - set(SCORER_CONFIG_FIELDS))
        raise OfflineBundleError(
            "scorer config fields mismatch:"
            f"missing={','.join(missing) or 'none'}:extra={','.join(extra) or 'none'}"
        )
    normalized: dict[str, Any] = {}
    probability_fields = {
        "min_swing_prob",
        "min_entry_prob",
        "min_trade_prob",
        "max_entry_uncertainty",
        "structure_timing_rescue_min_score",
        "structure_timing_entry_rescue_margin",
        "structure_timing_max_chase_risk",
    }
    finite_nonnegative_fields = {
        "max_allowed_spread_bps",
        "min_expected_edge_bps",
        "entry_hysteresis_margin_bps",
    }
    bool_fields = {
        "use_uncertainty_gate",
        "structure_timing_enabled",
        "enable_pair_quality_prior",
    }
    for field in SCORER_CONFIG_FIELDS:
        value = supplied[field]
        if field in bool_fields:
            if not isinstance(value, bool):
                raise OfflineBundleError(f"scorer config {field} must be boolean")
            normalized[field] = bool(value)
        elif field in probability_fields:
            number = float(value)
            if not math.isfinite(number) or not 0.0 <= number <= 1.0:
                raise OfflineBundleError(f"scorer config {field} must be finite in [0,1]")
            normalized[field] = number
        elif field in finite_nonnegative_fields:
            number = float(value)
            if not math.isfinite(number) or number < 0.0:
                raise OfflineBundleError(f"scorer config {field} must be finite and nonnegative")
            normalized[field] = number
        else:
            text = str(value or "").strip()
            if not text:
                raise OfflineBundleError(f"scorer config {field} must be nonempty")
            normalized[field] = text
    return normalized


def _training_cutoff(active_row: Mapping[str, Any]) -> tuple[pd.Timestamp, dict[str, str]]:
    metadata = dict(active_row.get("metadata") or {})
    summary = dict(metadata.get("training_window_summary") or {})
    bounds: dict[str, str] = {}
    parsed: list[pd.Timestamp] = []
    for component, raw_window in summary.items():
        window = dict(raw_window or {})
        raw_end = window.get("end_ts")
        timestamp = pd.to_datetime(raw_end, utc=True, errors="coerce")
        if pd.isna(timestamp):
            continue
        resolved = pd.Timestamp(timestamp)
        bounds[str(component)] = resolved.isoformat()
        parsed.append(resolved)
    if not parsed:
        raise OfflineBundleError("active manifest has no valid training-window end timestamps")
    return max(parsed), bounds


def _iter_string_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _iter_string_values(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_string_values(child)
    elif isinstance(value, str):
        yield value


def _resolve_repo_reference(raw: str, *, repository_root: Path) -> Path | None:
    text = str(raw or "").strip()
    if not text or "://" in text:
        return None
    candidate = Path(text)
    resolved = candidate.resolve() if candidate.is_absolute() else (repository_root / candidate).resolve()
    if not _is_within(resolved, repository_root) or not resolved.exists():
        return None
    return resolved


def _artifact_reference_plan(
    active_row: Mapping[str, Any],
    *,
    repository_root: Path,
) -> tuple[list[Path], list[dict[str, str]]]:
    required_values: list[str] = [str(active_row.get("registry_path") or "")]
    evidence_values: list[str] = []
    artifacts = dict(active_row.get("artifacts") or {})
    for artifact in artifacts.values():
        ref = dict(artifact or {}) if isinstance(artifact, Mapping) else {}
        path_value = str(ref.get("path") or ref.get("model_uri") or "").strip()
        if path_value:
            required_values.append(path_value)
        evidence_values.extend(
            str(item) for item in dict(ref.get("evidence_refs") or {}).values() if str(item).strip()
        )

    resolved: dict[str, Path] = {}
    for raw in required_values:
        if not str(raw).strip():
            continue
        path = _resolve_repo_reference(raw, repository_root=repository_root)
        if path is None:
            raise OfflineBundleError(f"required active artifact reference is missing or external: {raw}")
        resolved[str(path).lower()] = path
    evidence_audit: list[dict[str, str]] = []
    for raw in sorted(set(evidence_values)):
        path = _resolve_repo_reference(raw, repository_root=repository_root)
        if path is None:
            evidence_audit.append(
                {
                    "reference": raw,
                    "status": "missing_or_external",
                    "bundle_path": "",
                }
            )
            continue
        resolved[str(path).lower()] = path
        evidence_audit.append(
            {
                "reference": raw,
                "status": "copied",
                "bundle_path": path.relative_to(repository_root).as_posix(),
            }
        )
    return (
        sorted(resolved.values(), key=lambda item: item.as_posix()),
        evidence_audit,
    )


def _copy_repo_path(*, source: Path, repository_root: Path, destination: Path) -> None:
    if source.is_symlink():
        raise OfflineBundleError(f"symlinked source is forbidden: {source}")
    paths = [source] if source.is_file() else sorted(source.rglob("*"), key=lambda item: item.as_posix())
    for path in paths:
        if path.is_dir():
            continue
        if path.is_symlink():
            raise OfflineBundleError(f"symlinked source is forbidden: {path}")
        if not _is_within(path, repository_root):
            raise OfflineBundleError(f"source escaped repository root: {path}")
        relative = path.resolve().relative_to(repository_root.resolve())
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if file_sha256(target) != file_sha256(path):
                raise OfflineBundleError(f"copy collision changed bytes: {relative.as_posix()}")
            continue
        shutil.copy2(path, target)


def directory_file_hashes(root: str | Path) -> dict[str, str]:
    source = Path(root).resolve()
    if not source.is_dir():
        raise OfflineBundleError(f"bundle root is missing: {source}")
    files: dict[str, str] = {}
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_dir():
            continue
        if path.is_symlink():
            raise OfflineBundleError(f"bundle symlink is forbidden: {path}")
        relative = path.resolve().relative_to(source).as_posix()
        if relative == "bundle_manifest.json":
            continue
        files[relative] = file_sha256(path)
    return files


def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(dict(payload), handle, indent=2, sort_keys=True)
        handle.write("\n")


def _identity_dict(identity: Any) -> dict[str, str]:
    return {
        "pair": str(identity.pair),
        "bundle_run_id": str(identity.bundle_run_id),
        "model_set_id": str(identity.model_set_id),
        "model_identity_sha256": str(identity.model_manifest_sha256).lower(),
        "artifact_set_sha256": str(identity.artifact_set_sha256).lower(),
    }


def _runtime_package_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in SCORING_RUNTIME_PACKAGES:
        try:
            value = str(importlib.metadata.version(package)).strip()
        except importlib.metadata.PackageNotFoundError as exc:
            raise OfflineBundleError(
                f"required offline scoring package is not installed: {package}"
            ) from exc
        if not value:
            raise OfflineBundleError(f"required offline scoring package has no version: {package}")
        versions[package] = value
    if versions.get("nautilus_trader") != REQUIRED_ENGINE_VERSION:
        raise OfflineBundleError(
            "offline bundle must be built in the pinned NautilusTrader 1.230.0 environment"
        )
    return versions


def build_offline_bundle(config: OfflineBundleBuildConfig) -> dict[str, Any]:
    repository_root = config.repository_root.resolve()
    active_manifest = config.active_manifest_path.resolve()
    raw_store_root = config.raw_store_root.resolve()
    destination = config.destination.resolve()
    pair = str(config.pair or "").strip().upper()
    provider = str(config.provider or "").strip().lower()
    all_pairs = tuple(dict.fromkeys(str(item).strip().upper() for item in config.all_pairs if str(item).strip()))
    if not repository_root.is_dir() or not active_manifest.is_file() or not raw_store_root.is_dir():
        raise OfflineBundleError("repository, active manifest, and raw store must already exist")
    if not pair or not provider or pair not in all_pairs:
        raise OfflineBundleError("pair/provider/all-pairs scope is invalid")
    if destination.exists():
        raise OfflineBundleError(f"fresh bundle destination already exists: {destination}")
    _require_nonoverlap(
        destination,
        (
            repository_root,
            active_manifest.parent,
            raw_store_root,
            Path(__file__).resolve().parents[4],
            *tuple(path.resolve() for path in config.protected_roots),
        ),
    )
    scorer_config = _normalize_scorer_config(config.scorer_config)
    payload = read_json_object(active_manifest, label="active manifest")
    active_row = dict(dict(payload.get("active_model_sets") or {}).get(pair) or {})
    if not active_row:
        raise OfflineBundleError(f"active manifest has no row for {pair}")
    artifact_sources, evidence_reference_audit = _artifact_reference_plan(
        active_row,
        repository_root=repository_root,
    )
    from fxstack.training.release_evidence import active_manifest_identity

    identity = active_manifest_identity(manifest_path=active_manifest, pair=pair)
    source_identity = {
        **_identity_dict(identity),
        "manifest_file_sha256": file_sha256(active_manifest),
    }
    if not all(
        (
            source_identity["bundle_run_id"],
            source_identity["model_set_id"],
            source_identity["model_identity_sha256"],
            source_identity["artifact_set_sha256"],
        )
    ):
        raise OfflineBundleError("active model semantic identity is incomplete")
    training_cutoff, training_bounds = _training_cutoff(active_row)
    requested_start = (
        _normalize_timestamp(config.replay_start, label="replay_start")
        if str(config.replay_start or "").strip()
        else training_cutoff + pd.Timedelta(1, unit="ns")
    )
    replay_start = max(requested_start, training_cutoff + pd.Timedelta(1, unit="ns"))
    replay_end = _normalize_timestamp(config.replay_end, label="replay_end")
    if replay_end <= replay_start:
        raise OfflineBundleError("replay_end must be later than the strict post-training start")

    from fxstack.features.multi_tf_contract import build_multi_tf_rows

    contract_rows, source_report = build_multi_tf_rows(
        pair=pair,
        raw_store_root=raw_store_root,
        provider=provider,
        anchor_timeframe=str(config.anchor_timeframe).strip().upper(),
        context_timeframes=list(CONTEXT_TIMEFRAMES),
        all_pairs=list(all_pairs),
        start_ts=replay_start,
        end_ts=replay_end,
    )
    if contract_rows.empty:
        raise OfflineBundleError("causal row builder returned no OOS rows")
    contract_rows = contract_rows.copy()
    contract_rows["ts"] = pd.to_datetime(contract_rows["ts"], utc=True, errors="coerce")
    contract_rows = contract_rows[
        contract_rows["ts"].notna()
        & (contract_rows["ts"] > training_cutoff)
        & (contract_rows["ts"] >= replay_start)
        & (contract_rows["ts"] <= replay_end)
    ].sort_values("ts").drop_duplicates("ts", keep="last").reset_index(drop=True)
    if contract_rows.empty or not bool((contract_rows["ts"] > training_cutoff).all()):
        raise OfflineBundleError("bundle contains no strictly post-training causal rows")
    market_columns = (
        "pair",
        "timeframe",
        "ts",
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
        "volume",
        "spread",
    )
    missing_market = [column for column in market_columns if column not in contract_rows.columns]
    if missing_market:
        raise OfflineBundleError(
            "causal rows are missing true bid/ask market columns:" + ",".join(missing_market)
        )
    bid_ask_bars = contract_rows[list(market_columns)].copy()
    numeric_market = [column for column in market_columns if column not in {"pair", "timeframe", "ts"}]
    for column in numeric_market:
        bid_ask_bars[column] = pd.to_numeric(bid_ask_bars[column], errors="coerce")
    if bid_ask_bars[numeric_market].isna().any().any():
        raise OfflineBundleError("bid/ask replay bars contain non-finite values")
    for suffix in ("open", "high", "low", "close"):
        if not bool((bid_ask_bars[f"ask_{suffix}"] >= bid_ask_bars[f"bid_{suffix}"]).all()):
            raise OfflineBundleError(f"bid/ask replay is crossed at {suffix}")

    destination.mkdir(parents=True, exist_ok=False)
    copied_manifest = destination / "active_models.json"
    shutil.copy2(active_manifest, copied_manifest)
    for source in artifact_sources:
        _copy_repo_path(
            source=source,
            repository_root=repository_root,
            destination=destination,
        )
    for relative in SCORING_CODE_MODULES.values():
        source = (repository_root / relative).resolve()
        if not source.is_file() or not _is_within(source, repository_root):
            raise OfflineBundleError(f"required scoring code is missing: {relative}")
        target = destination / "code" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    data_dir = destination / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    contract_path = data_dir / "causal_contract_rows.parquet"
    bars_path = data_dir / "bid_ask_bars.parquet"
    contract_rows.to_parquet(contract_path, index=False)
    bid_ask_bars.to_parquet(bars_path, index=False)
    config_payload = {
        "schema_version": SCORER_CONFIG_SCHEMA,
        "settings": scorer_config,
    }
    scorer_config_path = destination / "config" / "scorer_config.json"
    _write_json_exclusive(scorer_config_path, config_payload)

    files = directory_file_hashes(destination)
    dataset_inventory = {name: files.get(name, "") for name in DATA_FILES}
    if not all(_is_sha256(value) for value in dataset_inventory.values()):
        raise OfflineBundleError("dataset inventory is incomplete")
    code_inventory = {
        module: {
            "bundle_path": f"code/{relative}",
            "sha256": files.get(f"code/{relative}", ""),
        }
        for module, relative in SCORING_CODE_MODULES.items()
    }
    if not all(_is_sha256(dict(item).get("sha256")) for item in code_inventory.values()):
        raise OfflineBundleError("scoring code inventory is incomplete")
    source_contract = dict(source_report.get("raw_source_contract") or {})
    runtime_packages = _runtime_package_versions()
    manifest = {
        "schema_version": BUNDLE_SCHEMA,
        "engine": {
            "name": "nautilus_trader",
            "required_version": REQUIRED_ENGINE_VERSION,
        },
        "source_identity": source_identity,
        "active_manifest_path": "active_models.json",
        "artifact_root": ".",
        "dataset": {
            "pair": pair,
            "provider": provider,
            "anchor_timeframe": str(config.anchor_timeframe).strip().upper(),
            "all_pairs": list(all_pairs),
            "files": dataset_inventory,
            "dataset_hash": canonical_json_sha256(dataset_inventory),
            "causal_contract_row_count": int(len(contract_rows)),
            "bid_ask_bar_count": int(len(bid_ask_bars)),
        },
        "oos": {
            "training_cutoff": training_cutoff.isoformat(),
            "component_training_ends": training_bounds,
            "replay_start": pd.Timestamp(contract_rows["ts"].min()).isoformat(),
            "replay_end": pd.Timestamp(contract_rows["ts"].max()).isoformat(),
            "strictly_post_training": True,
        },
        "causal_source_contract": {
            "fingerprint": str(source_contract.get("fingerprint") or ""),
            "watermark": str(source_contract.get("watermark") or ""),
            "snapshot_attempts": int(source_report.get("raw_source_snapshot_attempts") or 0),
            "coverage": dict(source_report.get("coverage") or {}),
            "join_integrity": dict(source_report.get("join_integrity") or {}),
            "cross_pair_context": dict(source_report.get("cross_pair_context") or {}),
        },
        "source_reference_audit": {
            "required_runtime_artifact_count": int(len(artifact_sources)),
            "evidence_references": evidence_reference_audit,
            "unresolved_evidence_reference_count": sum(
                item["status"] != "copied" for item in evidence_reference_audit
            ),
        },
        "scoring": {
            "config_path": "config/scorer_config.json",
            "config_sha256": file_sha256(scorer_config_path),
            "code_inventory": code_inventory,
            "code_sha256": canonical_json_sha256(code_inventory),
            "production_scorer": "fxstack.live.scorer.LiveScorer.score",
            "runtime_packages": runtime_packages,
            "runtime_packages_sha256": canonical_json_sha256(runtime_packages),
        },
        "files": files,
        "bundle_payload_sha256": canonical_json_sha256(files),
        "authority": {
            "advisory_only": True,
            "activation_capability": False,
            "runtime_database_capability": False,
            "broker_capability": False,
            "network_capability": False,
        },
    }
    _write_json_exclusive(destination / "bundle_manifest.json", manifest)
    return manifest


def validate_bundle(bundle_root: str | Path) -> tuple[dict[str, Any], list[str]]:
    root = Path(bundle_root).resolve()
    errors: list[str] = []
    manifest_path = root / "bundle_manifest.json"
    try:
        manifest = read_json_object(manifest_path, label="offline bundle manifest")
    except OfflineBundleError:
        return {}, ["offline_bundle_manifest_invalid"]
    if str(manifest.get("schema_version") or "") != BUNDLE_SCHEMA:
        errors.append("offline_bundle_schema_invalid")
    authority = dict(manifest.get("authority") or {})
    if authority != {
        "advisory_only": True,
        "activation_capability": False,
        "runtime_database_capability": False,
        "broker_capability": False,
        "network_capability": False,
    }:
        errors.append("offline_bundle_authority_invalid")
    expected_files = {
        str(key): str(value).lower()
        for key, value in dict(manifest.get("files") or {}).items()
    }
    try:
        actual_files = directory_file_hashes(root)
    except OfflineBundleError:
        actual_files = {}
        errors.append("offline_bundle_file_inventory_invalid")
    if expected_files != actual_files:
        errors.append("offline_bundle_file_inventory_mismatch")
    if str(manifest.get("bundle_payload_sha256") or "").lower() != canonical_json_sha256(
        expected_files
    ):
        errors.append("offline_bundle_payload_hash_mismatch")
    dataset = dict(manifest.get("dataset") or {})
    dataset_inventory = {
        str(key): str(value).lower()
        for key, value in dict(dataset.get("files") or {}).items()
    }
    if set(dataset_inventory) != set(DATA_FILES):
        errors.append("offline_dataset_inventory_invalid")
    elif any(
        expected_files.get(path) != digest or actual_files.get(path) != digest
        for path, digest in dataset_inventory.items()
    ):
        errors.append("offline_dataset_inventory_mismatch")
    if str(dataset.get("dataset_hash") or "").lower() != canonical_json_sha256(dataset_inventory):
        errors.append("offline_dataset_hash_mismatch")
    source_identity = dict(manifest.get("source_identity") or {})
    errors.extend(
        f"offline_source_{field}_invalid"
        for field in (
            "model_identity_sha256",
            "artifact_set_sha256",
            "manifest_file_sha256",
        )
        if not _is_sha256(source_identity.get(field))
    )
    active_relative = str(manifest.get("active_manifest_path") or "")
    active_path = (root / active_relative).resolve()
    if not _is_within(active_path, root) or not active_path.is_file():
        errors.append("offline_active_manifest_missing")
    elif file_sha256(active_path) != str(source_identity.get("manifest_file_sha256") or "").lower():
        errors.append("offline_active_manifest_file_hash_mismatch")
    else:
        try:
            from fxstack.training.release_evidence import active_manifest_identity

            identity = _identity_dict(
                active_manifest_identity(
                    manifest_path=active_path,
                    pair=str(source_identity.get("pair") or ""),
                )
            )
            errors.extend(
                f"offline_source_{field}_mismatch"
                for field in (
                    "pair",
                    "bundle_run_id",
                    "model_set_id",
                    "model_identity_sha256",
                    "artifact_set_sha256",
                )
                if str(identity.get(field) or "")
                != str(source_identity.get(field) or "")
            )
        except Exception:
            errors.append("offline_active_manifest_identity_invalid")
    reference_audit = dict(manifest.get("source_reference_audit") or {})
    evidence_references = list(reference_audit.get("evidence_references") or [])
    unresolved_count = sum(
        not isinstance(item, Mapping) or str(item.get("status") or "") != "copied"
        for item in evidence_references
    )
    if int(reference_audit.get("required_runtime_artifact_count") or 0) <= 0:
        errors.append("offline_required_artifact_inventory_invalid")
    if unresolved_count != int(reference_audit.get("unresolved_evidence_reference_count") or 0):
        errors.append("offline_evidence_reference_audit_invalid")
    for item in evidence_references:
        if not isinstance(item, Mapping) or str(item.get("status") or "") != "copied":
            continue
        copied_path = (root / str(item.get("bundle_path") or "")).resolve()
        if not _is_within(copied_path, root) or not copied_path.exists():
            errors.append("offline_copied_evidence_reference_missing")
    oos = dict(manifest.get("oos") or {})
    cutoff: pd.Timestamp | None = None
    try:
        cutoff = _normalize_timestamp(oos.get("training_cutoff"), label="training_cutoff")
        replay_start = _normalize_timestamp(oos.get("replay_start"), label="replay_start")
        replay_end = _normalize_timestamp(oos.get("replay_end"), label="replay_end")
        if oos.get("strictly_post_training") is not True or not cutoff < replay_start <= replay_end:
            errors.append("offline_oos_bounds_invalid")
    except OfflineBundleError:
        errors.append("offline_oos_bounds_invalid")
    try:
        rows = pd.read_parquet(root / DATA_FILES[0])
        bars = pd.read_parquet(root / DATA_FILES[1])
        row_ts = pd.to_datetime(rows["ts"], utc=True, errors="coerce")
        bar_ts = pd.to_datetime(bars["ts"], utc=True, errors="coerce")
        if (
            rows.empty
            or bars.empty
            or len(rows) != int(dataset.get("causal_contract_row_count") or 0)
            or len(bars) != int(dataset.get("bid_ask_bar_count") or 0)
            or row_ts.isna().any()
            or bar_ts.isna().any()
            or not row_ts.equals(bar_ts)
            or cutoff is None
            or not bool((row_ts > cutoff).all())
        ):
            errors.append("offline_oos_rows_invalid")
    except Exception:
        errors.append("offline_oos_rows_invalid")
    scoring = dict(manifest.get("scoring") or {})
    code_inventory = {
        str(module): dict(item or {})
        for module, item in dict(scoring.get("code_inventory") or {}).items()
    }
    if set(code_inventory) != set(SCORING_CODE_MODULES):
        errors.append("offline_scoring_code_inventory_invalid")
    else:
        for module, relative in SCORING_CODE_MODULES.items():
            item = code_inventory[module]
            expected_path = f"code/{relative}"
            if (
                str(item.get("bundle_path") or "") != expected_path
                or str(item.get("sha256") or "").lower() != expected_files.get(expected_path)
            ):
                errors.append(f"offline_scoring_code_identity_mismatch:{module}")
    if str(scoring.get("code_sha256") or "").lower() != canonical_json_sha256(
        code_inventory
    ):
        errors.append("offline_scoring_code_hash_mismatch")
    runtime_packages = {
        str(key): str(value)
        for key, value in dict(scoring.get("runtime_packages") or {}).items()
    }
    if set(runtime_packages) != set(SCORING_RUNTIME_PACKAGES) or any(
        not value.strip() for value in runtime_packages.values()
    ):
        errors.append("offline_runtime_package_identity_invalid")
    if runtime_packages.get("nautilus_trader") != REQUIRED_ENGINE_VERSION:
        errors.append("offline_nautilus_package_identity_invalid")
    if str(scoring.get("runtime_packages_sha256") or "").lower() != canonical_json_sha256(
        runtime_packages
    ):
        errors.append("offline_runtime_package_hash_mismatch")
    config_path = (root / str(scoring.get("config_path") or "")).resolve()
    if (
        not _is_within(config_path, root)
        or not config_path.is_file()
        or file_sha256(config_path) != str(scoring.get("config_sha256") or "").lower()
    ):
        errors.append("offline_scorer_config_hash_mismatch")
    else:
        try:
            config_payload = read_json_object(config_path, label="scorer config")
            if str(config_payload.get("schema_version") or "") != SCORER_CONFIG_SCHEMA:
                raise OfflineBundleError("scorer config schema mismatch")
            _normalize_scorer_config(dict(config_payload.get("settings") or {}))
        except OfflineBundleError:
            errors.append("offline_scorer_config_invalid")
    return manifest, list(dict.fromkeys(errors))


__all__ = [
    "BUNDLE_SCHEMA",
    "DATA_FILES",
    "OfflineBundleBuildConfig",
    "OfflineBundleError",
    "REQUIRED_ENGINE_VERSION",
    "SCORER_CONFIG_FIELDS",
    "SCORING_RUNTIME_PACKAGES",
    "SCORING_CODE_MODULES",
    "build_offline_bundle",
    "canonical_json_sha256",
    "directory_file_hashes",
    "file_sha256",
    "read_json_object",
    "validate_bundle",
]
