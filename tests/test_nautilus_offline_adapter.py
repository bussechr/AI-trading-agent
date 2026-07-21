from __future__ import annotations

import json
import os
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from fxstack.backtest.harness.contracts import (
    EXTERNAL_ECONOMIC_REPORT_VERSION,
    ScenarioSpec,
)
from fxstack.backtest.harness.nautilus_offline_adapter import (
    HarnessContract,
    _external_report,
    deny_network,
    sanitized_environment,
    validate_adapter_report,
)
from fxstack.backtest.harness.nautilus_offline_bundle import (
    BUNDLE_SCHEMA,
    REQUIRED_ENGINE_VERSION,
    SCORER_CONFIG_FIELDS,
    SCORING_CODE_MODULES,
    SCORING_RUNTIME_PACKAGES,
    OfflineBundleBuildConfig,
    OfflineBundleError,
    _normalize_scorer_config,
    build_offline_bundle,
    canonical_json_sha256,
    directory_file_hashes,
    file_sha256,
    validate_bundle,
)
from fxstack.backtest.harness.nautilus_offline_engine import (
    ENGINE_OUTPUT_SCHEMA,
    build_scenario_quote_records,
    scenario_execution_parameters,
)
from fxstack.backtest.harness.stress import DEFAULT_PHASE3_SCENARIOS


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
NAUTILUS_PYTHON = (
    REPOSITORY_ROOT
    / "fx-quant-stack"
    / ".venv_win_nautilus"
    / "Scripts"
    / "python.exe"
)


def scorer_settings() -> dict[str, object]:
    return {
        "min_swing_prob": 0.58,
        "min_entry_prob": 0.62,
        "min_trade_prob": 0.60,
        "max_allowed_spread_bps": 3.0,
        "min_expected_edge_bps": 3.0,
        "use_uncertainty_gate": True,
        "max_entry_uncertainty": 0.25,
        "blocked_entry_sessions_csv": "pacific",
        "strategy_engine_mode": "supervised_legacy",
        "structure_timing_enabled": True,
        "structure_timing_rescue_min_score": 0.66,
        "structure_timing_entry_rescue_margin": 0.05,
        "structure_timing_max_chase_risk": 0.78,
        "entry_hysteresis_margin_bps": 1.0,
        "enable_pair_quality_prior": False,
        "tier1_pairs_csv": "EURUSD,GBPUSD",
    }


def sample_bars(*, count: int = 100) -> pd.DataFrame:
    start = pd.Timestamp("2026-07-19T20:55:00Z")
    rows: list[dict[str, object]] = []
    for index in range(count):
        mid = 1.10 + (index * 0.00001)
        rows.append(
            {
                "pair": "EURUSD",
                "timeframe": "M5",
                "ts": start + pd.Timedelta(minutes=5 * index),
                "bid_open": mid - 0.00005,
                "bid_high": mid + 0.00015,
                "bid_low": mid - 0.00015,
                "bid_close": mid + 0.00005,
                "ask_open": mid + 0.00005,
                "ask_high": mid + 0.00025,
                "ask_low": mid - 0.00005,
                "ask_close": mid + 0.00015,
            }
        )
    return pd.DataFrame(rows)


def _minimal_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "bundle"
    (root / "data").mkdir(parents=True)
    (root / "config").mkdir(parents=True)
    active_manifest = {
        "schema_version": 1,
        "active_model_sets": {"EURUSD": {"model_set_id": "model-set"}},
    }
    (root / "active_models.json").write_text(
        json.dumps(active_manifest), encoding="utf-8"
    )
    timestamps = pd.Series(
        pd.to_datetime(["2026-07-18T00:05:00Z", "2026-07-18T00:10:00Z"], utc=True)
    )
    rows = pd.DataFrame({"ts": timestamps, "feature": [1.0, 2.0]})
    bars = pd.DataFrame(
        {
            "ts": timestamps,
            "bid_close": [1.1, 1.2],
            "ask_close": [1.1001, 1.2001],
        }
    )
    rows.to_parquet(root / "data" / "causal_contract_rows.parquet", index=False)
    bars.to_parquet(root / "data" / "bid_ask_bars.parquet", index=False)
    (root / "config" / "scorer_config.json").write_text(
        json.dumps(
            {
                "schema_version": "fxstack_offline_scorer_config_v1",
                "settings": scorer_settings(),
            }
        ),
        encoding="utf-8",
    )
    for index, relative in enumerate(SCORING_CODE_MODULES.values()):
        path = root / "code" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# immutable scoring source {index}\n", encoding="utf-8")
    files = directory_file_hashes(root)
    dataset_files = {
        "data/causal_contract_rows.parquet": files["data/causal_contract_rows.parquet"],
        "data/bid_ask_bars.parquet": files["data/bid_ask_bars.parquet"],
    }
    code_inventory = {
        module: {
            "bundle_path": f"code/{relative}",
            "sha256": files[f"code/{relative}"],
        }
        for module, relative in SCORING_CODE_MODULES.items()
    }
    runtime_packages = {
        package: REQUIRED_ENGINE_VERSION if package == "nautilus_trader" else "test-version"
        for package in SCORING_RUNTIME_PACKAGES
    }
    source_identity = {
        "pair": "EURUSD",
        "bundle_run_id": "bundle-run",
        "model_set_id": "model-set",
        "model_identity_sha256": "1" * 64,
        "artifact_set_sha256": "2" * 64,
        "manifest_file_sha256": file_sha256(root / "active_models.json"),
    }
    manifest = {
        "schema_version": BUNDLE_SCHEMA,
        "engine": {"name": "nautilus_trader", "required_version": REQUIRED_ENGINE_VERSION},
        "source_identity": source_identity,
        "active_manifest_path": "active_models.json",
        "artifact_root": ".",
        "dataset": {
            "pair": "EURUSD",
            "provider": "test",
            "anchor_timeframe": "M5",
            "all_pairs": ["EURUSD"],
            "files": dataset_files,
            "dataset_hash": canonical_json_sha256(dataset_files),
            "causal_contract_row_count": 2,
            "bid_ask_bar_count": 2,
        },
        "oos": {
            "training_cutoff": "2026-07-18T00:00:00+00:00",
            "component_training_ends": {"regime": "2026-07-18T00:00:00+00:00"},
            "replay_start": "2026-07-18T00:05:00+00:00",
            "replay_end": "2026-07-18T00:10:00+00:00",
            "strictly_post_training": True,
        },
        "causal_source_contract": {},
        "source_reference_audit": {
            "required_runtime_artifact_count": 1,
            "evidence_references": [],
            "unresolved_evidence_reference_count": 0,
        },
        "scoring": {
            "config_path": "config/scorer_config.json",
            "config_sha256": files["config/scorer_config.json"],
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
    (root / "bundle_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    identity = SimpleNamespace(
        pair="EURUSD",
        bundle_run_id="bundle-run",
        model_set_id="model-set",
        model_manifest_sha256="1" * 64,
        artifact_set_sha256="2" * 64,
    )
    monkeypatch.setattr(
        "fxstack.training.release_evidence.active_manifest_identity",
        lambda **_: identity,
    )
    return root


def test_scorer_config_requires_the_exact_bound_allowlist() -> None:
    normalized = _normalize_scorer_config(scorer_settings())
    assert set(normalized) == set(SCORER_CONFIG_FIELDS)
    incomplete = scorer_settings()
    incomplete.pop("min_trade_prob")
    with pytest.raises(Exception, match="fields mismatch"):
        _normalize_scorer_config(incomplete)
    extra = {**scorer_settings(), "database_url": "postgres://forbidden"}
    with pytest.raises(Exception, match="fields mismatch"):
        _normalize_scorer_config(extra)


def test_every_scenario_has_distinct_actual_engine_parameters_and_quote_effects() -> None:
    specs = {scenario.name: scenario for scenario in DEFAULT_PHASE3_SCENARIOS}
    params = {name: scenario_execution_parameters(spec) for name, spec in specs.items()}
    assert len({canonical_json_sha256(item) for item in params.values()}) == 7
    assert params["WideSpread"]["quote_transform"]["spread_multiplier"] == 1.75
    assert params["SlippageShock"]["fill_model"]["prob_slippage"] == 0.10
    assert params["LatencyShock"]["latency_model"]["insert_latency_nanos"] == 750_000_000
    assert params["PartialFills"]["liquidity_model"]["partial_fill_probability"] == 0.35
    assert params["QuoteGap"]["quote_transform"]["quote_gap_probability"] == 0.10
    assert params["SessionCutover"]["quote_transform"]["session_cutover_penalty_bps"] == 1.5

    bars = sample_bars()
    records: dict[str, list[dict[str, object]]] = {}
    counters: dict[str, dict[str, object]] = {}
    for name, spec in specs.items():
        records[name], counters[name] = build_scenario_quote_records(bars=bars, scenario=spec)
    base_spread = float(records["BaseCase"][0]["ask"]) - float(records["BaseCase"][0]["bid"])
    wide_spread = float(records["WideSpread"][0]["ask"]) - float(records["WideSpread"][0]["bid"])
    assert wide_spread > base_spread
    assert int(counters["QuoteGap"]["quote_gap_drop_count"]) > 0
    assert int(counters["PartialFills"]["partial_liquidity_quote_count"]) > 0
    assert int(counters["SessionCutover"]["session_cutover_transform_count"]) > 0


def test_bundle_validator_rejects_tampering_and_invalid_oos_bounds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _minimal_bundle(tmp_path, monkeypatch)
    _, errors = validate_bundle(bundle)
    assert errors == []

    manifest_path = bundle / "bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["oos"]["replay_start"] = manifest["oos"]["training_cutoff"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _, errors = validate_bundle(bundle)
    assert "offline_oos_bounds_invalid" in errors

    manifest["oos"]["replay_start"] = "2026-07-18T00:05:00+00:00"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    bars_path = bundle / "data" / "bid_ask_bars.parquet"
    bars = pd.read_parquet(bars_path)
    bars.loc[0, "bid_close"] = 9.99
    bars.to_parquet(bars_path, index=False)
    _, errors = validate_bundle(bundle)
    assert "offline_bundle_file_inventory_mismatch" in errors
    assert "offline_dataset_inventory_mismatch" in errors


def test_bundle_builder_fails_identity_preflight_without_partial_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    raw_store = repository / "raw"
    raw_store.mkdir(parents=True)
    (repository / "registry.json").write_text("{}", encoding="utf-8")
    active_manifest = repository / "active_models.json"
    active_manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "model-set",
                        "registry_path": "registry.json",
                        "artifacts": {},
                        "metadata": {},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    incomplete = SimpleNamespace(
        pair="EURUSD",
        bundle_run_id="bundle-run",
        model_set_id="model-set",
        model_manifest_sha256="",
        artifact_set_sha256="",
    )
    monkeypatch.setattr(
        "fxstack.training.release_evidence.active_manifest_identity",
        lambda **_: incomplete,
    )
    destination = tmp_path / "fresh-bundle"
    with pytest.raises(OfflineBundleError, match="semantic identity is incomplete"):
        build_offline_bundle(
            OfflineBundleBuildConfig(
                repository_root=repository,
                active_manifest_path=active_manifest,
                raw_store_root=raw_store,
                destination=destination,
                pair="EURUSD",
                provider="test",
                all_pairs=("EURUSD",),
                replay_end="2026-07-20T00:00:00Z",
                scorer_config=scorer_settings(),
            )
        )
    assert not destination.exists()


def test_network_and_credential_guards_are_process_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FXSTACK_BROKER_PASSWORD", "must-not-be-readable")
    with sanitized_environment() as removed:
        assert "FXSTACK_BROKER_PASSWORD" in removed
        assert "FXSTACK_BROKER_PASSWORD" not in os.environ
    assert os.environ["FXSTACK_BROKER_PASSWORD"] == "must-not-be-readable"
    with deny_network() as attempts:
        with pytest.raises(Exception, match="network access is forbidden"):
            socket.create_connection(("127.0.0.1", 9), timeout=0.01)
    assert attempts


def test_fake_external_report_is_rejected(tmp_path: Path) -> None:
    report = tmp_path / "economic.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": EXTERNAL_ECONOMIC_REPORT_VERSION,
                "attestation_schema": "fxstack_nautilus_adapter_attestation_v1",
                "harness_run_id": "forged",
                "engine": "nautilus",
                "engine_version": REQUIRED_ENGINE_VERSION,
                "pair": "EURUSD",
                "dataset_hash": "dataset",
                "input_bundle_sha256": "a" * 64,
                "bundle_run_id": "bundle",
                "model_set_id": "model",
                "model_manifest_sha256": "b" * 64,
                "artifact_set_sha256": "c" * 64,
                "status": "completed",
                "authoritative": True,
                "advisory_only": False,
                "authority_failures": [],
                "trade_count": 99,
                "engine_execution": {
                    "actual_engine": True,
                    "synthetic": True,
                    "fxstack_internal_simulator": False,
                    "class": "nautilus_trader.backtest.engine.BacktestEngine",
                    "version": REQUIRED_ENGINE_VERSION,
                    "run_id": "forged-run",
                    "database_configured": False,
                },
                "source_attestation": {
                    "offline": True,
                    "database_configured": False,
                    "broker_configured": False,
                    "activation_capability": False,
                    "bundle_unchanged": True,
                    "network_attempt_count": 0,
                },
                "scenario_parameters": scenario_execution_parameters(
                    ScenarioSpec(name="BaseCase")
                ),
            }
        ),
        encoding="utf-8",
    )
    errors = validate_adapter_report(report, output_root=tmp_path, require_authoritative=True)
    assert "adapter_report_engine_source_invalid" in errors
    assert "adapter_report_raw_engine_output_invalid" in errors


@pytest.mark.skipif(not NAUTILUS_PYTHON.is_file(), reason="isolated Nautilus 1.230.0 env missing")
def test_real_nautilus_smoke_has_native_run_and_hash_bound_ledgers(tmp_path: Path) -> None:
    scenario_dir = tmp_path / "engine" / "BaseCase"
    process = subprocess.run(
        [
            str(NAUTILUS_PYTHON),
            "-m",
            "fxstack.backtest.harness.nautilus_offline_adapter",
            "engine-smoke",
            "--output",
            str(scenario_dir),
        ],
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert process.returncode == 0, process.stderr
    raw_path = scenario_dir / "engine_output.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == ENGINE_OUTPUT_SCHEMA
    assert raw["engine"]["version"] == REQUIRED_ENGINE_VERSION
    assert raw["engine"]["class"] == "nautilus_trader.backtest.engine.BacktestEngine"
    assert raw["engine"]["actual_engine"] is True
    assert raw["engine"]["synthetic"] is False
    assert raw["engine"]["fxstack_internal_simulator"] is False
    assert raw["engine"]["run_id"]
    assert len(raw["engine"]["module_sha256"]) == 64
    assert raw["result_counters"]["total_orders"] == 2
    assert raw["economic_metrics"]["fill_count"] == 2
    assert raw["economic_metrics"]["trade_count"] == 1
    for filename, digest in raw["raw_ledger_inventory"].items():
        ledger = scenario_dir / filename
        assert ledger.is_file()
        assert file_sha256(ledger) == digest

    contract = HarnessContract(
        economic_report=tmp_path / "economic.json",
        stress_reports={},
        harness_run_id="smoke-harness",
        pair="EURUSD",
        dataset_hash="smoke-dataset",
        engine_version=REQUIRED_ENGINE_VERSION,
        input_bundle_sha256="a" * 64,
        model_manifest=tmp_path / "unused-active-models.json",
        bundle_run_id="smoke-bundle",
        model_set_id="smoke-model",
        model_manifest_sha256="b" * 64,
        artifact_set_sha256="c" * 64,
    )
    report = _external_report(
        schema_version=EXTERNAL_ECONOMIC_REPORT_VERSION,
        scenario=ScenarioSpec(name="BaseCase"),
        contract=contract,
        raw_output=raw,
        scenario_dir=scenario_dir,
        output_root=tmp_path,
        bundle_manifest={
            "schema_version": BUNDLE_SCHEMA,
            "bundle_payload_sha256": "d" * 64,
            "oos": {"strictly_post_training": True},
            "causal_source_contract": {},
            "source_identity": {},
        },
        scoring_evidence={
            "production_scorer": "fxstack.live.scorer.LiveScorer.score",
            "scored_signals": {"count": 1},
            "approved_intents": {"count": 1},
        },
        code_identity={},
        package_identity={"nautilus_trader": REQUIRED_ENGINE_VERSION},
        artifact_identity={},
        scrubbed_environment=[],
        network_attempts=[],
        immutable_bundle_unchanged=True,
    )
    contract.economic_report.write_text(json.dumps(report), encoding="utf-8")
    assert validate_adapter_report(
        contract.economic_report,
        output_root=tmp_path,
        require_authoritative=True,
        expected_linkage=contract.linkage(),
    ) == []
