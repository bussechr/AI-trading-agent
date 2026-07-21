from __future__ import annotations

import argparse
import contextlib
import importlib
import importlib.metadata
import json
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import pandas as pd

from fxstack.backtest.harness.contracts import (
    EXTERNAL_ECONOMIC_REPORT_VERSION,
    EXTERNAL_STRESS_REPORT_VERSION,
    REQUIRED_EXTERNAL_STRESS_SCENARIOS,
    ScenarioSpec,
    directory_sha256,
)
from fxstack.backtest.harness.nautilus_offline_bundle import (
    REQUIRED_ENGINE_VERSION,
    SCORING_CODE_MODULES,
    SCORING_RUNTIME_PACKAGES,
    OfflineBundleError,
    _normalize_scorer_config,
    canonical_json_sha256,
    directory_file_hashes,
    file_sha256,
    read_json_object,
    validate_bundle,
)
from fxstack.backtest.harness.nautilus_offline_engine import (
    ENGINE_OUTPUT_SCHEMA,
    OfflineEngineError,
    run_nautilus_scenario,
    run_real_engine_smoke,
    scenario_execution_parameters,
)
from fxstack.backtest.harness.stress import DEFAULT_PHASE3_SCENARIOS


ADAPTER_REPORT_ATTESTATION = "fxstack_nautilus_adapter_attestation_v1"
ADAPTER_RUN_SCHEMA = "fxstack_nautilus_offline_adapter_run_v1"
SCORING_OUTPUT_SCHEMA = "fxstack_production_scorer_replay_v1"
FORBIDDEN_MODULE_PREFIXES = (
    "fxstack.api",
    "fxstack.db",
    "fxstack.runtime.runner",
    "fxstack.runtime.service",
    "fxstack.live.bridge",
)


class OfflineAdapterError(RuntimeError):
    pass


@dataclass(frozen=True)
class HarnessContract:
    economic_report: Path
    stress_reports: Mapping[str, Path]
    harness_run_id: str
    pair: str
    dataset_hash: str
    engine_version: str
    input_bundle_sha256: str
    model_manifest: Path
    bundle_run_id: str
    model_set_id: str
    model_manifest_sha256: str
    artifact_set_sha256: str

    def linkage(self) -> dict[str, str]:
        return {
            "harness_run_id": self.harness_run_id,
            "engine": "nautilus",
            "engine_version": self.engine_version,
            "pair": self.pair,
            "dataset_hash": self.dataset_hash,
            "input_bundle_sha256": self.input_bundle_sha256,
            "bundle_run_id": self.bundle_run_id,
            "model_set_id": self.model_set_id,
            "model_manifest_sha256": self.model_manifest_sha256,
            "artifact_set_sha256": self.artifact_set_sha256,
        }


class _BoundScorerSettings:
    def __init__(self, values: Mapping[str, Any]) -> None:
        for key, value in _normalize_scorer_config(values).items():
            setattr(self, key, value)

    @property
    def blocked_entry_sessions(self) -> list[str]:
        out: list[str] = []
        for raw in str(self.blocked_entry_sessions_csv).split(","):
            item = raw.strip().lower()
            if item in {"", "none", "off", "disabled", "false", "0"}:
                continue
            out.append(item)
        return out

    @property
    def tier1_pairs(self) -> list[str]:
        return [item.strip().upper() for item in str(self.tier1_pairs_csv).split(",") if item.strip()]

    def pair_tier(self, pair: str) -> str:
        return "tier1" if str(pair).strip().upper() in set(self.tier1_pairs) else "tier2"


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _safe_relative(path: Path, root: Path) -> str:
    resolved = path.resolve()
    if not _is_within(resolved, root):
        raise OfflineAdapterError(f"evidence path escaped output root: {resolved}")
    return resolved.relative_to(root.resolve()).as_posix()


def _write_json_exclusive(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _environment_is_sensitive(name: str) -> bool:
    upper = str(name).upper()
    if upper in {"FXSTACK_NAUTILUS_CMD", "FXSTACK_NAUTILUS_VERSION"}:
        return False
    explicit = {
        "DATABASE_URL",
        "MLFLOW_TRACKING_URI",
        "PGHOST",
        "PGPORT",
        "PGUSER",
        "PGPASSWORD",
        "PGDATABASE",
    }
    if upper in explicit:
        return True
    secret_tokens = ("API_KEY", "PASSWORD", "SECRET", "ACCESS_TOKEN", "AUTH_TOKEN")
    authority_tokens = ("BROKER", "BRIDGE", "MT4", "MT5", "DATABASE", "REGISTRY_WRITE")
    if any(token in upper for token in secret_tokens):
        return True
    return upper.startswith("FXSTACK_") and any(token in upper for token in authority_tokens)


@contextlib.contextmanager
def sanitized_environment() -> Iterator[list[str]]:
    removed = {
        name: value
        for name, value in list(os.environ.items())
        if _environment_is_sensitive(name)
    }
    for name in removed:
        os.environ.pop(name, None)
    try:
        yield sorted(removed)
    finally:
        os.environ.update(removed)


@contextlib.contextmanager
def deny_network() -> Iterator[list[str]]:
    attempts: list[str] = []
    original_create_connection = socket.create_connection
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def _denied(*args: Any, **kwargs: Any) -> Any:
        target = args[1] if len(args) > 1 else args[0] if args else "unknown"
        attempts.append(str(target))
        raise OfflineAdapterError("network access is forbidden in the offline Nautilus adapter")

    socket.create_connection = _denied
    socket.socket.connect = _denied
    socket.socket.connect_ex = _denied
    try:
        yield attempts
    finally:
        socket.create_connection = original_create_connection
        socket.socket.connect = original_connect
        socket.socket.connect_ex = original_connect_ex


def _reject_authority_files(bundle_root: Path) -> None:
    forbidden_names = {".env", "credentials.json", "secrets.json"}
    for path in bundle_root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name.lower()
        if name in forbidden_names or name.startswith(".env."):
            raise OfflineAdapterError(f"credential/environment file is forbidden in bundle: {path}")


def _reject_forbidden_modules() -> None:
    loaded = sorted(
        name
        for name in sys.modules
        if any(name == prefix or name.startswith(f"{prefix}.") for prefix in FORBIDDEN_MODULE_PREFIXES)
    )
    if loaded:
        raise OfflineAdapterError(
            "production authority modules are forbidden in the offline adapter: " + ",".join(loaded)
        )


def _require_output_boundary(*, bundle_root: Path, output_root: Path) -> None:
    repository_root = Path(__file__).resolve().parents[5]
    protected = (
        repository_root,
        Path(sys.prefix).resolve(),
        bundle_root,
    )
    for root in protected:
        if (
            output_root.resolve() == root.resolve()
            or _is_within(output_root, root)
            or _is_within(root, output_root)
        ):
            raise OfflineAdapterError(f"output overlaps protected bundle/repository/install root: {root}")
    if output_root.exists():
        if not output_root.is_dir() or any(output_root.iterdir()):
            raise OfflineAdapterError(f"adapter requires an empty fresh output directory: {output_root}")
    else:
        output_root.mkdir(parents=True, exist_ok=False)


def _runtime_package_identity(bundle_manifest: Mapping[str, Any]) -> dict[str, str]:
    scoring = dict(bundle_manifest.get("scoring") or {})
    expected = {
        str(key): str(value)
        for key, value in dict(scoring.get("runtime_packages") or {}).items()
    }
    if set(expected) != set(SCORING_RUNTIME_PACKAGES):
        raise OfflineAdapterError("bundle runtime-package identity is incomplete")
    actual: dict[str, str] = {}
    for package in SCORING_RUNTIME_PACKAGES:
        try:
            actual[package] = str(importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError as exc:
            raise OfflineAdapterError(f"required runtime package is missing: {package}") from exc
    if actual != expected:
        raise OfflineAdapterError(f"runtime package identity mismatch: expected={expected}, actual={actual}")
    if actual.get("nautilus_trader") != REQUIRED_ENGINE_VERSION:
        raise OfflineAdapterError("installed NautilusTrader version is not 1.230.0")
    return actual


def verify_scoring_code(
    *,
    bundle_root: Path,
    bundle_manifest: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    code_inventory = {
        str(module): dict(item or {})
        for module, item in dict(dict(bundle_manifest.get("scoring") or {}).get("code_inventory") or {}).items()
    }
    if set(code_inventory) != set(SCORING_CODE_MODULES):
        raise OfflineAdapterError("bundle scoring-code inventory is incomplete")
    verified: dict[str, dict[str, str]] = {}
    for module_name, expected_relative in SCORING_CODE_MODULES.items():
        item = code_inventory[module_name]
        bundle_path = (bundle_root / str(item.get("bundle_path") or "")).resolve()
        expected_bundle_path = (bundle_root / "code" / expected_relative).resolve()
        if bundle_path != expected_bundle_path or not _is_within(bundle_path, bundle_root):
            raise OfflineAdapterError(f"scoring code path mismatch: {module_name}")
        module = importlib.import_module(module_name)
        installed_path = Path(str(module.__file__ or "")).resolve()
        if installed_path.suffix == ".pyc" and installed_path.with_suffix(".py").is_file():
            installed_path = installed_path.with_suffix(".py")
        if not bundle_path.is_file() or not installed_path.is_file():
            raise OfflineAdapterError(f"scoring code file is missing: {module_name}")
        expected_hash = str(item.get("sha256") or "").lower()
        bundle_hash = file_sha256(bundle_path)
        installed_hash = file_sha256(installed_path)
        if not _is_sha256(expected_hash) or bundle_hash != expected_hash or installed_hash != expected_hash:
            raise OfflineAdapterError(f"scoring code bytes mismatch: {module_name}")
        verified[module_name] = {
            "bundle_path": bundle_path.relative_to(bundle_root).as_posix(),
            "installed_file": installed_path.name,
            "sha256": expected_hash,
        }
    if canonical_json_sha256(code_inventory) != str(
        dict(bundle_manifest.get("scoring") or {}).get("code_sha256") or ""
    ).lower():
        raise OfflineAdapterError("scoring-code inventory hash mismatch")
    return verified


def _validate_contract(
    *,
    bundle_root: Path,
    output_root: Path,
    bundle_manifest: Mapping[str, Any],
    contract: HarnessContract,
) -> None:
    source = dict(bundle_manifest.get("source_identity") or {})
    dataset = dict(bundle_manifest.get("dataset") or {})
    expected_manifest = (bundle_root / str(bundle_manifest.get("active_manifest_path") or "")).resolve()
    if contract.model_manifest.resolve() != expected_manifest:
        raise OfflineAdapterError("contract model manifest is not the immutable bundled manifest")
    expected = {
        "pair": str(source.get("pair") or "").upper(),
        "dataset_hash": str(dataset.get("dataset_hash") or ""),
        "engine_version": REQUIRED_ENGINE_VERSION,
        "bundle_run_id": str(source.get("bundle_run_id") or ""),
        "model_set_id": str(source.get("model_set_id") or ""),
        "model_manifest_sha256": str(source.get("model_identity_sha256") or ""),
        "artifact_set_sha256": str(source.get("artifact_set_sha256") or ""),
        "input_bundle_sha256": directory_sha256(bundle_root),
    }
    actual = {
        "pair": contract.pair,
        "dataset_hash": contract.dataset_hash,
        "engine_version": contract.engine_version,
        "bundle_run_id": contract.bundle_run_id,
        "model_set_id": contract.model_set_id,
        "model_manifest_sha256": contract.model_manifest_sha256,
        "artifact_set_sha256": contract.artifact_set_sha256,
        "input_bundle_sha256": contract.input_bundle_sha256,
    }
    for field, expected_value in expected.items():
        if str(actual[field]) != str(expected_value):
            raise OfflineAdapterError(f"harness contract linkage mismatch: {field}")
    if not contract.harness_run_id.strip():
        raise OfflineAdapterError("harness run ID is required")
    if set(contract.stress_reports) != set(REQUIRED_EXTERNAL_STRESS_SCENARIOS):
        raise OfflineAdapterError("every required external stress report path is required")
    paths = [contract.economic_report, *contract.stress_reports.values()]
    for path in paths:
        if not _is_within(path.resolve(), output_root):
            raise OfflineAdapterError(f"contract report path escaped output root: {path}")
        if path.exists():
            raise OfflineAdapterError(f"contract report path already exists: {path}")


def _artifact_path_within_bundle(raw_ref: Any, *, bundle_root: Path) -> Path:
    from fxstack.mlops.model_uri import resolve_model_artifact_path

    resolved = resolve_model_artifact_path(raw_ref, project_root=bundle_root).resolve()
    if not _is_within(resolved, bundle_root) or not resolved.exists():
        raise OfflineAdapterError(f"artifact escaped or is missing from bundle: {resolved}")
    return resolved


def _validate_all_artifacts(
    *,
    active_row: Mapping[str, Any],
    bundle_root: Path,
) -> dict[str, dict[str, str]]:
    from fxstack.belief.engine import validate_directional_belief_artifact_contract_read_only
    from fxstack.mlops.model_uri import artifact_ref_value, normalize_artifact_ref
    from fxstack.models.artifact_contract import validate_artifact_contract_read_only

    artifacts = dict(active_row.get("artifacts") or {})
    feature_schema = dict(
        dict(active_row.get("metadata") or {}).get("feature_schema")
        or active_row.get("feature_schema")
        or {}
    )
    expected_belief_contract = str(feature_schema.get("belief_contract") or "").strip()
    verified: dict[str, dict[str, str]] = {}
    seen: dict[str, str] = {}
    for component, raw_ref in artifacts.items():
        raw_path = artifact_ref_value(raw_ref)
        if not raw_path:
            continue
        normalized = normalize_artifact_ref(raw_ref)
        expected_digest = str(normalized.get("artifact_hash") or "").strip().lower() or None
        path = _artifact_path_within_bundle(raw_ref, bundle_root=bundle_root)
        key = str(path).lower()
        if key in seen:
            verified[str(component)] = {**verified[seen[key]], "alias_of": seen[key]}
            continue
        if str(component) == "directional_belief":
            validate_directional_belief_artifact_contract_read_only(
                path,
                expected_contract=expected_belief_contract,
                expected_digest=expected_digest,
            )
        else:
            validate_artifact_contract_read_only(
                path,
                label=f"offline_nautilus:{component}",
                expected_digest=expected_digest,
            )
        seen[key] = str(component)
        verified[str(component)] = {
            "bundle_path": path.relative_to(bundle_root).as_posix(),
            "artifact_hash": expected_digest or "",
        }
    if not verified:
        raise OfflineAdapterError("active model row has no verifiable local artifacts")
    return verified


def _load_exact_scorer(
    *,
    active_row: Mapping[str, Any],
    bundle_root: Path,
) -> tuple[Any, dict[str, Any]]:
    from fxstack.backtest.research_support import PolicyModelRouter, artifact_ref_value, safe_load_model
    from fxstack.live.scorer import LiveScorer
    from fxstack.models.intraday_xgb import IntradayXGB
    from fxstack.models.meta_filter import MetaFilterXGB
    from fxstack.models.regime_hmm import RegimeHMM
    from fxstack.models.swing_xgb import SwingXGB

    artifacts = dict(active_row.get("artifacts") or {})
    metadata = dict(active_row.get("metadata") or {})
    policies = dict(metadata.get("policies") or active_row.get("policies") or {})
    swing_policy = str(policies.get("swing") or "").strip().lower()
    intraday_policy = str(policies.get("intraday") or "").strip().lower()
    if swing_policy != "xgb_only" or intraday_policy != "xgb_only":
        raise OfflineAdapterError(
            "this exact active-bundle adapter requires the manifest's xgb_only policies"
        )
    refs = {
        "regime": artifact_ref_value(artifacts, "regime"),
        "swing_xgb": artifact_ref_value(artifacts, "swing_xgb", "swing"),
        "intraday_xgb": artifact_ref_value(artifacts, "intraday_xgb", "intraday"),
        "meta": artifact_ref_value(artifacts, "meta"),
    }
    classes = {
        "regime": RegimeHMM,
        "swing_xgb": SwingXGB,
        "intraday_xgb": IntradayXGB,
        "meta": MetaFilterXGB,
    }
    models: dict[str, Any] = {}
    diagnostics: dict[str, Any] = {}
    for component, model_cls in classes.items():
        model, error = safe_load_model(model_cls, refs[component], bundle_root)
        if model is None or error:
            raise OfflineAdapterError(f"failed loading exact active model {component}: {error}")
        models[component] = model
        diagnostics[component] = {
            "class": f"{model.__class__.__module__}.{model.__class__.__name__}",
            "feature_columns": list(getattr(model, "feature_columns", []) or []),
            "feature_column_count": len(list(getattr(model, "feature_columns", []) or [])),
        }
    swing_router = PolicyModelRouter(
        policy=swing_policy,
        family="swing",
        primary_name="swing_xgb",
        primary_model=models["swing_xgb"],
        fallback_name="swing_xgb",
        fallback_model=None,
    )
    intraday_router = PolicyModelRouter(
        policy=intraday_policy,
        family="intraday",
        primary_name="intraday_xgb",
        primary_model=models["intraday_xgb"],
        fallback_name="intraday_xgb",
        fallback_model=None,
    )
    scorer = LiveScorer(
        regime_model=models["regime"],
        swing_model=swing_router,
        intraday_model=intraday_router,
        meta_model=models["meta"],
    )
    return scorer, {
        "policies": {"swing": swing_policy, "intraday": intraday_policy},
        "models": diagnostics,
    }


def _context_input(frame: pd.DataFrame, *, model: Any, prefix: str) -> pd.DataFrame:
    from fxstack.live.scorer import LiveScorer

    required = list(getattr(model, "feature_columns", []) or [])
    if not required and str(getattr(model, "name", "")) == "regime_hmm":
        required = ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"]
    data: dict[str, Any] = {}
    missing: list[str] = []
    for column in required:
        prefixed = f"{prefix}{column}"
        if prefixed in frame.columns:
            data[column] = frame[prefixed]
        elif column in frame.columns:
            data[column] = frame[column]
        else:
            missing.append(column)
    if missing:
        raise OfflineAdapterError(f"missing {prefix} context columns: {','.join(missing)}")
    return LiveScorer._model_input(model, pd.DataFrame(data, index=frame.index))


@contextlib.contextmanager
def _bind_scorer_settings(settings: _BoundScorerSettings) -> Iterator[None]:
    scorer_module = importlib.import_module("fxstack.live.scorer")
    original = scorer_module.get_settings
    scorer_module.get_settings = lambda: settings
    try:
        yield
    finally:
        scorer_module.get_settings = original


def _signal_payload(signal: Any) -> dict[str, Any]:
    if hasattr(signal, "model_dump"):
        return dict(signal.model_dump(mode="json"))
    if hasattr(signal, "dict"):
        return dict(signal.dict())
    raise OfflineAdapterError("production scorer returned an unsupported signal object")


def score_production_intents(
    *,
    bundle_root: Path,
    output_root: Path,
    bundle_manifest: Mapping[str, Any],
    active_row: Mapping[str, Any],
) -> dict[str, Any]:
    rows_path = bundle_root / "data" / "causal_contract_rows.parquet"
    rows = pd.read_parquet(rows_path)
    if rows.empty:
        raise OfflineAdapterError("causal contract contains no scoring rows")
    config_path = (
        bundle_root / str(dict(bundle_manifest.get("scoring") or {}).get("config_path") or "")
    ).resolve()
    config_payload = read_json_object(config_path, label="offline scorer config")
    settings_values = _normalize_scorer_config(dict(config_payload.get("settings") or {}))
    settings = _BoundScorerSettings(settings_values)
    scorer, model_diagnostics = _load_exact_scorer(
        active_row=active_row,
        bundle_root=bundle_root,
    )
    scored: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []
    with _bind_scorer_settings(settings):
        for row_number, (_, row) in enumerate(rows.iterrows()):
            frame = pd.DataFrame([row])
            regime_row = _context_input(frame, model=scorer.regime_model, prefix="h4_")
            swing_model = scorer.swing_model.primary_model or scorer.swing_model.fallback_model
            swing_row = _context_input(frame, model=swing_model, prefix="d_")
            signal = scorer.score(
                regime_row=regime_row,
                swing_row=swing_row,
                intraday_row=frame,
                meta_row=frame,
                spread_bps=None,
                expected_edge_bps=None,
                spread_unit_source="causal_bid_ask_bar",
            )
            payload = _signal_payload(signal)
            payload["source_row_number"] = int(row_number)
            payload["signal_sha256"] = canonical_json_sha256(payload)
            scored.append(payload)
            if payload.get("allowed") is True:
                approved.append(
                    {
                        "source_row_number": int(row_number),
                        "ts": str(payload.get("ts") or row.get("ts") or ""),
                        "pair": str(payload.get("pair") or "").upper(),
                        "side": str(payload.get("side") or "").lower(),
                        "expected_edge_bps": float(payload.get("expected_edge_bps") or 0.0),
                        "spread_bps": float(payload.get("spread_bps") or 0.0),
                        "signal_sha256": str(payload["signal_sha256"]),
                    }
                )
    scored_path = output_root / "scoring" / "scored_signals.json"
    intents_path = output_root / "scoring" / "approved_intents.json"
    _write_json_exclusive(scored_path, scored)
    _write_json_exclusive(intents_path, approved)
    return {
        "schema_version": SCORING_OUTPUT_SCHEMA,
        "production_scorer": "fxstack.live.scorer.LiveScorer.score",
        "config_path": config_path.relative_to(bundle_root).as_posix(),
        "config_sha256": file_sha256(config_path),
        "scored_signals": {
            "path": _safe_relative(scored_path, output_root),
            "sha256": file_sha256(scored_path),
            "count": int(len(scored)),
        },
        "approved_intents": {
            "path": _safe_relative(intents_path, output_root),
            "sha256": file_sha256(intents_path),
            "count": int(len(approved)),
        },
        "approved": approved,
        "model_diagnostics": model_diagnostics,
    }


def _raw_evidence(
    *,
    raw_output: Mapping[str, Any],
    scenario_dir: Path,
    output_root: Path,
) -> dict[str, Any]:
    engine_output_path = scenario_dir / "engine_output.json"
    ledgers: dict[str, dict[str, str]] = {}
    for filename, digest in dict(raw_output.get("raw_ledger_inventory") or {}).items():
        path = scenario_dir / str(filename)
        ledgers[str(filename)] = {
            "path": _safe_relative(path, output_root),
            "sha256": str(digest).lower(),
        }
    return {
        "engine_output": {
            "path": _safe_relative(engine_output_path, output_root),
            "sha256": file_sha256(engine_output_path),
        },
        "raw_ledgers": ledgers,
    }


def _authority_failures(raw_output: Mapping[str, Any], *, scenario: ScenarioSpec) -> list[str]:
    failures: list[str] = []
    engine = dict(raw_output.get("engine") or {})
    counters = dict(raw_output.get("result_counters") or {})
    metrics = dict(raw_output.get("economic_metrics") or {})
    exercise = dict(raw_output.get("scenario_exercise") or {})
    input_info = dict(raw_output.get("input") or {})
    if engine.get("actual_engine") is not True or engine.get("synthetic") is not False:
        failures.append("actual_nautilus_engine_missing")
    if engine.get("fxstack_internal_simulator") is not False:
        failures.append("fxstack_internal_simulator_detected")
    if str(engine.get("version") or "") != REQUIRED_ENGINE_VERSION:
        failures.append("nautilus_engine_version_mismatch")
    if str(engine.get("class") or "") != "nautilus_trader.backtest.engine.BacktestEngine":
        failures.append("nautilus_engine_source_invalid")
    if not str(engine.get("run_id") or ""):
        failures.append("nautilus_engine_run_id_missing")
    if engine.get("database_configured") is not False:
        failures.append("engine_database_configured")
    if int(engine.get("external_data_clients") or 0) != 0 or int(
        engine.get("external_execution_clients") or 0
    ) != 0:
        failures.append("external_engine_client_configured")
    if int(input_info.get("production_intent_count") or 0) <= 0:
        failures.append("no_production_scorer_intents")
    if int(counters.get("total_orders") or 0) <= 0:
        failures.append("no_engine_orders")
    if int(metrics.get("fill_count") or 0) <= 0:
        failures.append("no_engine_fills")
    if int(metrics.get("trade_count") or 0) <= 0:
        failures.append("no_closed_engine_trades")
    if exercise.get("exercised") is not True:
        failures.append("scenario_parameters_not_exercised")
    if str(scenario.name) == "PartialFills" and int(metrics.get("partial_fill_count") or 0) <= 0:
        failures.append("partial_fill_scenario_produced_no_partial_fill")
    return failures


def _external_report(
    *,
    schema_version: str,
    scenario: ScenarioSpec,
    contract: HarnessContract,
    raw_output: Mapping[str, Any],
    scenario_dir: Path,
    output_root: Path,
    bundle_manifest: Mapping[str, Any],
    scoring_evidence: Mapping[str, Any],
    code_identity: Mapping[str, Any],
    package_identity: Mapping[str, str],
    artifact_identity: Mapping[str, Any],
    scrubbed_environment: Sequence[str],
    network_attempts: Sequence[str],
    immutable_bundle_unchanged: bool,
) -> dict[str, Any]:
    failures = _authority_failures(raw_output, scenario=scenario)
    if not immutable_bundle_unchanged:
        failures.append("immutable_bundle_changed_during_execution")
    if network_attempts:
        failures.append("network_access_attempted")
    reference_audit = dict(bundle_manifest.get("source_reference_audit") or {})
    if int(reference_audit.get("unresolved_evidence_reference_count") or 0) > 0:
        failures.append("bundle_evidence_references_unresolved")
    metrics = dict(raw_output.get("economic_metrics") or {})
    authoritative = not failures
    report: dict[str, Any] = {
        "schema_version": schema_version,
        **contract.linkage(),
        "status": "completed" if authoritative else "failed",
        "authoritative": authoritative,
        "advisory_only": not authoritative,
        "authority_failures": failures,
        "attestation_schema": ADAPTER_REPORT_ATTESTATION,
        "scenario_parameters": dict(raw_output.get("scenario_parameters") or {}),
        "scenario_parameters_sha256": str(
            raw_output.get("scenario_parameters_sha256") or ""
        ),
        "scenario_exercise": dict(raw_output.get("scenario_exercise") or {}),
        "engine_execution": {
            **dict(raw_output.get("engine") or {}),
            "result_counters": dict(raw_output.get("result_counters") or {}),
            **_raw_evidence(
                raw_output=raw_output,
                scenario_dir=scenario_dir,
                output_root=output_root,
            ),
        },
        "source_attestation": {
            "physically_separate_bundle": True,
            "offline": True,
            "network_guard": "python_socket_connect_denied_v1",
            "network_attempt_count": len(network_attempts),
            "database_configured": False,
            "broker_configured": False,
            "activation_capability": False,
            "registry_write_capability": False,
            "scrubbed_environment_names": list(scrubbed_environment),
            "bundle_unchanged": immutable_bundle_unchanged,
        },
        "bundle_evidence": {
            "schema_version": str(bundle_manifest.get("schema_version") or ""),
            "bundle_payload_sha256": str(bundle_manifest.get("bundle_payload_sha256") or ""),
            "oos": dict(bundle_manifest.get("oos") or {}),
            "causal_source_contract": dict(bundle_manifest.get("causal_source_contract") or {}),
            "source_reference_audit": dict(bundle_manifest.get("source_reference_audit") or {}),
            "source_identity": dict(bundle_manifest.get("source_identity") or {}),
        },
        "scoring_evidence": {
            key: value for key, value in scoring_evidence.items() if key != "approved"
        },
        "producer_identity": {
            "module": "fxstack.backtest.harness.nautilus_offline_adapter",
            "production_scorer": "fxstack.live.scorer.LiveScorer.score",
            "scoring_code": dict(code_identity),
            "runtime_packages": dict(package_identity),
            "artifacts": dict(artifact_identity),
        },
        "realized_pnl_usd": float(metrics.get("realized_pnl_usd") or 0.0),
        "unrealized_pnl_usd": float(metrics.get("unrealized_pnl_usd") or 0.0),
        "turnover_lots": float(metrics.get("turnover_lots") or 0.0),
        "max_drawdown_pct": float(metrics.get("max_drawdown_pct") or 0.0),
        "margin_utilization_peak": float(metrics.get("margin_utilization_peak") or 0.0),
        "trade_count": int(metrics.get("trade_count") or 0),
        "partial_fill_count": int(metrics.get("partial_fill_count") or 0),
        "latency_ms_p95": float(metrics.get("latency_ms_p95") or 0.0),
        "rejection_rate": float(metrics.get("rejection_rate") or 0.0),
        "notes": [
            "Actual NautilusTrader BacktestEngine output; no FXStack internal simulator.",
            "Reports remain advisory unless every authority check and trade-sufficiency check passes.",
        ],
    }
    if schema_version == EXTERNAL_STRESS_REPORT_VERSION:
        report["scenario"] = str(scenario.name)
    return report


def validate_adapter_report(
    report_path: str | Path,
    *,
    output_root: str | Path,
    require_authoritative: bool = True,
    expected_linkage: Mapping[str, str] | None = None,
) -> list[str]:
    path = Path(report_path).resolve()
    root = Path(output_root).resolve()
    errors: list[str] = []
    if not _is_within(path, root) or not path.is_file():
        return ["adapter_report_missing_or_external"]
    try:
        report = read_json_object(path, label="Nautilus adapter report")
    except OfflineBundleError:
        return ["adapter_report_invalid_json"]
    schema = str(report.get("schema_version") or "")
    if schema not in {EXTERNAL_ECONOMIC_REPORT_VERSION, EXTERNAL_STRESS_REPORT_VERSION}:
        errors.append("adapter_report_schema_invalid")
    if str(report.get("attestation_schema") or "") != ADAPTER_REPORT_ATTESTATION:
        errors.append("adapter_report_attestation_missing")
    linkage_fields = (
        "harness_run_id",
        "engine",
        "engine_version",
        "pair",
        "dataset_hash",
        "input_bundle_sha256",
        "bundle_run_id",
        "model_set_id",
        "model_manifest_sha256",
        "artifact_set_sha256",
    )
    for field in linkage_fields:
        if not str(report.get(field) or ""):
            errors.append(f"adapter_report_linkage_missing:{field}")
    for field, expected in dict(expected_linkage or {}).items():
        if str(report.get(field) or "") != str(expected):
            errors.append(f"adapter_report_linkage_mismatch:{field}")
    engine = dict(report.get("engine_execution") or {})
    if (
        engine.get("actual_engine") is not True
        or engine.get("synthetic") is not False
        or engine.get("fxstack_internal_simulator") is not False
        or str(engine.get("class") or "")
        != "nautilus_trader.backtest.engine.BacktestEngine"
        or str(engine.get("version") or "") != REQUIRED_ENGINE_VERSION
        or not str(engine.get("run_id") or "")
    ):
        errors.append("adapter_report_engine_source_invalid")
    if engine.get("database_configured") is not False:
        errors.append("adapter_report_engine_database_invalid")
    raw_ref = dict(engine.get("engine_output") or {})
    raw_path = (root / str(raw_ref.get("path") or "")).resolve()
    raw_output: dict[str, Any] = {}
    if (
        not _is_within(raw_path, root)
        or not raw_path.is_file()
        or file_sha256(raw_path) != str(raw_ref.get("sha256") or "").lower()
    ):
        errors.append("adapter_report_raw_engine_output_invalid")
    else:
        try:
            raw_output = read_json_object(raw_path, label="raw Nautilus engine output")
        except OfflineBundleError:
            errors.append("adapter_report_raw_engine_output_invalid")
    if raw_output:
        if str(raw_output.get("schema_version") or "") != ENGINE_OUTPUT_SCHEMA:
            errors.append("adapter_report_raw_engine_schema_invalid")
        if dict(raw_output.get("engine") or {}) != {
            key: engine.get(key) for key in dict(raw_output.get("engine") or {})
        }:
            errors.append("adapter_report_engine_attestation_mismatch")
        params = dict(raw_output.get("scenario_parameters") or {})
        if canonical_json_sha256(params) != str(report.get("scenario_parameters_sha256") or ""):
            errors.append("adapter_report_scenario_parameters_hash_mismatch")
        raw_inventory = {
            str(key): str(value).lower()
            for key, value in dict(raw_output.get("raw_ledger_inventory") or {}).items()
        }
        report_inventory = {
            str(key): dict(value or {})
            for key, value in dict(engine.get("raw_ledgers") or {}).items()
        }
        if set(raw_inventory) != set(report_inventory):
            errors.append("adapter_report_raw_ledger_inventory_mismatch")
        for filename, digest in raw_inventory.items():
            ref = report_inventory.get(filename, {})
            ledger_path = (root / str(ref.get("path") or "")).resolve()
            if (
                not _is_within(ledger_path, root)
                or not ledger_path.is_file()
                or str(ref.get("sha256") or "").lower() != digest
                or file_sha256(ledger_path) != digest
            ):
                errors.append(f"adapter_report_raw_ledger_invalid:{filename}")
        try:
            result_rows = read_json_object(raw_path.parent / "result.json", label="engine result")
            orders = json.loads((raw_path.parent / "orders.json").read_text(encoding="utf-8"))
            fills = json.loads((raw_path.parent / "fills.json").read_text(encoding="utf-8"))
            positions = json.loads((raw_path.parent / "positions.json").read_text(encoding="utf-8"))
            counters = dict(raw_output.get("result_counters") or {})
            if int(result_rows.get("total_orders") or 0) != int(counters.get("total_orders") or 0):
                errors.append("adapter_report_order_counter_mismatch")
            if int(result_rows.get("total_positions") or 0) != int(
                counters.get("total_positions") or 0
            ):
                errors.append("adapter_report_position_counter_mismatch")
            if int(counters.get("total_orders") or 0) != len(list(orders or [])):
                errors.append("adapter_report_order_ledger_count_mismatch")
            metrics = dict(raw_output.get("economic_metrics") or {})
            if int(metrics.get("fill_count") or 0) != len(list(fills or [])):
                errors.append("adapter_report_fill_ledger_count_mismatch")
            if int(metrics.get("position_ledger_count") or 0) != len(list(positions or [])):
                errors.append("adapter_report_position_ledger_count_mismatch")
        except (OSError, UnicodeError, json.JSONDecodeError, OfflineBundleError, TypeError):
            errors.append("adapter_report_raw_ledger_crosscheck_failed")
    source = dict(report.get("source_attestation") or {})
    if (
        source.get("offline") is not True
        or source.get("database_configured") is not False
        or source.get("broker_configured") is not False
        or source.get("activation_capability") is not False
        or source.get("bundle_unchanged") is not True
        or int(source.get("network_attempt_count") or 0) != 0
    ):
        errors.append("adapter_report_offline_attestation_invalid")
    scenario_name = "BaseCase" if schema == EXTERNAL_ECONOMIC_REPORT_VERSION else str(
        report.get("scenario") or ""
    )
    specs = {item.name: item for item in DEFAULT_PHASE3_SCENARIOS}
    if scenario_name not in specs:
        errors.append("adapter_report_scenario_invalid")
    else:
        expected_params = scenario_execution_parameters(specs[scenario_name])
        if dict(report.get("scenario_parameters") or {}) != expected_params:
            errors.append("adapter_report_scenario_parameters_invalid")
    if require_authoritative:
        if (
            report.get("authoritative") is not True
            or report.get("advisory_only") is not False
            or str(report.get("status") or "") != "completed"
            or list(report.get("authority_failures") or [])
            or int(report.get("trade_count") or 0) <= 0
        ):
            errors.append("adapter_report_not_authoritative")
    return list(dict.fromkeys(errors))


def run_external_backtest(
    *,
    bundle_root: Path,
    output_root: Path,
    contract: HarnessContract,
) -> dict[str, Any]:
    bundle_root = bundle_root.resolve()
    output_root = output_root.resolve()
    bundle_manifest, bundle_errors = validate_bundle(bundle_root)
    if bundle_errors:
        raise OfflineAdapterError("offline bundle validation failed: " + ",".join(bundle_errors))
    _reject_authority_files(bundle_root)
    _require_output_boundary(bundle_root=bundle_root, output_root=output_root)
    _validate_contract(
        bundle_root=bundle_root,
        output_root=output_root,
        bundle_manifest=bundle_manifest,
        contract=contract,
    )
    before_inventory = directory_file_hashes(bundle_root)
    active_payload = read_json_object(contract.model_manifest, label="bundled active manifest")
    active_row = dict(
        dict(active_payload.get("active_model_sets") or {}).get(contract.pair) or {}
    )
    if not active_row:
        raise OfflineAdapterError(f"bundled active manifest has no row for {contract.pair}")
    with sanitized_environment() as scrubbed_environment, deny_network() as network_attempts:
        _reject_forbidden_modules()
        code_identity = verify_scoring_code(
            bundle_root=bundle_root,
            bundle_manifest=bundle_manifest,
        )
        package_identity = _runtime_package_identity(bundle_manifest)
        artifact_identity = _validate_all_artifacts(
            active_row=active_row,
            bundle_root=bundle_root,
        )
        scoring = score_production_intents(
            bundle_root=bundle_root,
            output_root=output_root,
            bundle_manifest=bundle_manifest,
            active_row=active_row,
        )
        bars = pd.read_parquet(bundle_root / "data" / "bid_ask_bars.parquet")
        engine_outputs: dict[str, dict[str, Any]] = {}
        engine_errors: dict[str, str] = {}
        for scenario in DEFAULT_PHASE3_SCENARIOS:
            scenario_dir = output_root / "engine" / scenario.name
            try:
                engine_outputs[scenario.name] = run_nautilus_scenario(
                    bars=bars,
                    intents=list(scoring["approved"]),
                    scenario=scenario,
                    output_dir=scenario_dir,
                )
            except (OfflineEngineError, Exception) as exc:
                engine_errors[scenario.name] = f"{type(exc).__name__}:{exc}"
        _reject_forbidden_modules()
        immutable_bundle_unchanged = before_inventory == directory_file_hashes(bundle_root)
        reports: dict[str, dict[str, Any]] = {}
        for scenario in DEFAULT_PHASE3_SCENARIOS:
            if scenario.name not in engine_outputs:
                continue
            is_base = scenario.name == "BaseCase"
            report_path = (
                contract.economic_report
                if is_base
                else Path(contract.stress_reports[scenario.name])
            )
            report = _external_report(
                schema_version=(
                    EXTERNAL_ECONOMIC_REPORT_VERSION
                    if is_base
                    else EXTERNAL_STRESS_REPORT_VERSION
                ),
                scenario=scenario,
                contract=contract,
                raw_output=engine_outputs[scenario.name],
                scenario_dir=output_root / "engine" / scenario.name,
                output_root=output_root,
                bundle_manifest=bundle_manifest,
                scoring_evidence=scoring,
                code_identity=code_identity,
                package_identity=package_identity,
                artifact_identity=artifact_identity,
                scrubbed_environment=scrubbed_environment,
                network_attempts=network_attempts,
                immutable_bundle_unchanged=immutable_bundle_unchanged,
            )
            _write_json_exclusive(report_path, report)
            reports[scenario.name] = report
    expected_linkage = contract.linkage()
    validation_errors: dict[str, list[str]] = {}
    if contract.economic_report.is_file():
        validation_errors["BaseCase"] = validate_adapter_report(
            contract.economic_report,
            output_root=output_root,
            require_authoritative=True,
            expected_linkage=expected_linkage,
        )
    for name, path in contract.stress_reports.items():
        if path.is_file():
            validation_errors[name] = validate_adapter_report(
                path,
                output_root=output_root,
                require_authoritative=True,
                expected_linkage=expected_linkage,
            )
    all_authoritative = (
        not engine_errors
        and set(reports) == {item.name for item in DEFAULT_PHASE3_SCENARIOS}
        and all(not errors for errors in validation_errors.values())
        and len(validation_errors) == len(DEFAULT_PHASE3_SCENARIOS)
    )
    run_manifest = {
        "schema_version": ADAPTER_RUN_SCHEMA,
        "status": "completed" if all_authoritative else "failed",
        "authoritative": all_authoritative,
        "advisory_only": not all_authoritative,
        "linkage": expected_linkage,
        "bundle_root_name": bundle_root.name,
        "bundle_payload_sha256": str(bundle_manifest.get("bundle_payload_sha256") or ""),
        "scored_signal_count": int(dict(scoring.get("scored_signals") or {}).get("count") or 0),
        "approved_intent_count": int(dict(scoring.get("approved_intents") or {}).get("count") or 0),
        "engine_errors": engine_errors,
        "report_validation_errors": validation_errors,
        "report_sha256": {
            name: file_sha256(
                contract.economic_report
                if name == "BaseCase"
                else Path(contract.stress_reports[name])
            )
            for name in reports
        },
        "authority": {
            "activation_capability": False,
            "database_capability": False,
            "broker_capability": False,
            "registry_write_capability": False,
        },
    }
    _write_json_exclusive(output_root / "adapter_run_manifest.json", run_manifest)
    return run_manifest


def _stress_paths(values: Sequence[str], parser: argparse.ArgumentParser) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for raw in values:
        name, separator, path = str(raw).partition("=")
        if not separator or not name.strip() or not path.strip():
            parser.error("--fxstack-stress-report must be SCENARIO=PATH")
        out[name.strip()] = Path(path.strip()).resolve()
    return out


def _backtest_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("backtest", help="Run exact active-bundle offline replay")
    parser.add_argument("--bundle", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--fxstack-economic-report", required=True)
    parser.add_argument("--fxstack-harness-run-id", required=True)
    parser.add_argument("--fxstack-pair", required=True)
    parser.add_argument("--fxstack-dataset-hash", required=True)
    parser.add_argument("--fxstack-engine-version", required=True)
    parser.add_argument("--fxstack-input-bundle-sha256", required=True)
    parser.add_argument("--fxstack-model-manifest", required=True)
    parser.add_argument("--fxstack-bundle-run-id", required=True)
    parser.add_argument("--fxstack-model-set-id", required=True)
    parser.add_argument("--fxstack-model-manifest-sha256", required=True)
    parser.add_argument("--fxstack-artifact-set-sha256", required=True)
    parser.add_argument("--fxstack-stress-report", action="append", default=[])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Physically isolated active-model NautilusTrader economic adapter"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    _backtest_parser(subparsers)
    smoke = subparsers.add_parser("engine-smoke", help="Run a non-authoritative real-engine smoke")
    smoke.add_argument("--output", required=True)
    validate = subparsers.add_parser("validate-report", help="Validate raw-engine report attestation")
    validate.add_argument("--report", required=True)
    validate.add_argument("--output-root", required=True)
    validate.add_argument(
        "--require-authoritative",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "engine-smoke":
        with sanitized_environment(), deny_network():
            result = run_real_engine_smoke(Path(args.output))
        summary = {
            "schema_version": str(result.get("schema_version") or ""),
            "engine": dict(result.get("engine") or {}),
            "result_counters": dict(result.get("result_counters") or {}),
            "economic_metrics": dict(result.get("economic_metrics") or {}),
            "authoritative_external_evidence": False,
        }
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0 if int(summary["economic_metrics"].get("trade_count") or 0) > 0 else 2
    if args.command == "validate-report":
        errors = validate_adapter_report(
            args.report,
            output_root=args.output_root,
            require_authoritative=bool(args.require_authoritative),
        )
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2, sort_keys=True))
        return 0 if not errors else 2
    output_root = Path(args.out).resolve()
    stress_reports = _stress_paths(args.fxstack_stress_report, parser)
    contract = HarnessContract(
        economic_report=Path(args.fxstack_economic_report).resolve(),
        stress_reports=stress_reports,
        harness_run_id=str(args.fxstack_harness_run_id),
        pair=str(args.fxstack_pair).strip().upper(),
        dataset_hash=str(args.fxstack_dataset_hash),
        engine_version=str(args.fxstack_engine_version),
        input_bundle_sha256=str(args.fxstack_input_bundle_sha256),
        model_manifest=Path(args.fxstack_model_manifest).resolve(),
        bundle_run_id=str(args.fxstack_bundle_run_id),
        model_set_id=str(args.fxstack_model_set_id),
        model_manifest_sha256=str(args.fxstack_model_manifest_sha256),
        artifact_set_sha256=str(args.fxstack_artifact_set_sha256),
    )
    try:
        run_manifest = run_external_backtest(
            bundle_root=Path(args.bundle),
            output_root=output_root,
            contract=contract,
        )
    except Exception as exc:
        failure = {
            "schema_version": ADAPTER_RUN_SCHEMA,
            "status": "failed",
            "authoritative": False,
            "advisory_only": True,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if output_root.is_dir() and _is_within(output_root, Path("C:/tmp")):
            failure_path = output_root / "adapter_failure.json"
            if not failure_path.exists():
                _write_json_exclusive(failure_path, failure)
        print(json.dumps(failure, indent=2, sort_keys=True), file=sys.stderr)
        return 3
    print(json.dumps(run_manifest, indent=2, sort_keys=True))
    return 0 if run_manifest.get("authoritative") is True else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ADAPTER_REPORT_ATTESTATION",
    "HarnessContract",
    "OfflineAdapterError",
    "deny_network",
    "main",
    "run_external_backtest",
    "sanitized_environment",
    "score_production_intents",
    "validate_adapter_report",
    "verify_scoring_code",
]
