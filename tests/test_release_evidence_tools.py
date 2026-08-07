from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.backtest.harness import run_lean_harness, run_nautilus_harness
from fxstack.backtest.harness.contracts import (
    EXTERNAL_ECONOMIC_REPORT_VERSION,
    EXTERNAL_STRESS_REPORT_VERSION,
    REQUIRED_EXTERNAL_STRESS_SCENARIOS,
)
from fxstack.training.release_evidence import (
    active_manifest_identity,
    economic_sufficiency,
    file_sha256,
    validate_economic_evidence,
)
from tools.assemble_phase5_economic_evidence import assemble_economic_evidence


def _active_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "active_models.json"
    path.write_text(
        json.dumps(
            {
                "active_model_sets": {
                    "EURUSD": {
                        "model_set_id": "model-eurusd-1",
                        "metadata": {"bundle_run_id": "bundle-eurusd-1"},
                        "artifacts": {
                            "meta": {"content_sha256": "a" * 64},
                            "intraday": {"content_sha256": "b" * 64},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def _command_values(command: list[str], flag: str) -> list[str]:
    return [
        command[index + 1]
        for index, token in enumerate(command[:-1])
        if token == flag
    ]


def _external_engine_process(*, engine: str):
    def _run(command: list[str], **_kwargs: object) -> object:
        linkage = {
            "harness_run_id": _command_values(command, "--fxstack-harness-run-id")[0],
            "engine": engine,
            "engine_version": _command_values(command, "--fxstack-engine-version")[0],
            "pair": _command_values(command, "--fxstack-pair")[0],
            "dataset_hash": _command_values(command, "--fxstack-dataset-hash")[0],
            "input_bundle_sha256": _command_values(command, "--fxstack-input-bundle-sha256")[0],
            "bundle_run_id": _command_values(command, "--fxstack-bundle-run-id")[0],
            "model_set_id": _command_values(command, "--fxstack-model-set-id")[0],
            "model_manifest_sha256": _command_values(command, "--fxstack-model-manifest-sha256")[0],
            "artifact_set_sha256": _command_values(command, "--fxstack-artifact-set-sha256")[0],
        }
        economic_report = Path(_command_values(command, "--fxstack-economic-report")[0])
        economic_report.write_text(
            json.dumps(
                {
                    "schema_version": EXTERNAL_ECONOMIC_REPORT_VERSION,
                    **linkage,
                    "status": "completed",
                    "realized_pnl_usd": 150.0,
                    "unrealized_pnl_usd": 0.0,
                    "max_drawdown_pct": 2.0,
                    "turnover_lots": 1.25,
                    "trade_count": 10,
                    "partial_fill_count": 0,
                    "latency_ms_p95": 5.0,
                    "rejection_rate": 0.0,
                }
            ),
            encoding="utf-8",
        )
        for raw in _command_values(command, "--fxstack-stress-report"):
            scenario, path_text = raw.split("=", 1)
            Path(path_text).write_text(
                json.dumps(
                    {
                        "schema_version": EXTERNAL_STRESS_REPORT_VERSION,
                        **linkage,
                        "scenario": scenario,
                        "status": "completed",
                        "realized_pnl_usd": 100.0,
                        "unrealized_pnl_usd": 0.0,
                        "max_drawdown_pct": 4.0,
                        "turnover_lots": 1.5,
                        "trade_count": 10,
                        "partial_fill_count": 1,
                        "latency_ms_p95": 15.0,
                        "rejection_rate": 0.01,
                    }
                ),
                encoding="utf-8",
            )
        return type(
            "CompletedHarness",
            (),
            {"returncode": 0, "stdout": "completed", "stderr": ""},
        )()

    return _run


def _run_external_harness(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: object,
    subprocess_target: str,
    engine: str,
) -> tuple[Path, Path, Path, object]:
    manifest_path = _active_manifest(tmp_path)
    output_dir = tmp_path / f"{engine}_output"
    economic_report = output_dir / f"{engine}_economic.json"
    monkeypatch.setenv(f"FXSTACK_{engine.upper()}_VERSION", f"{engine}-test-1")
    monkeypatch.setattr(subprocess_target, _external_engine_process(engine=engine))
    harness = runner(
        bundle_dir=tmp_path,
        output_dir=output_dir,
        pair="EURUSD",
        dataset_hash="dataset-sha-1",
        model_manifest_path=manifest_path,
        economic_report_path=economic_report,
        execute=True,
    )
    harness_path = output_dir / f"{engine}_manifest.json"
    harness_path.write_text(json.dumps(harness.to_dict()), encoding="utf-8")
    return manifest_path, harness_path, economic_report, harness


def test_internal_lifecycle_replay_cannot_mint_authoritative_economic_evidence(tmp_path: Path) -> None:
    manifest = _active_manifest(tmp_path)
    expected = active_manifest_identity(manifest_path=manifest, pair="EURUSD")
    identity = expected.to_dict()
    identity.update(
        {
            "evidence_kind": "economic_validation",
            "source_kind": "internal_lifecycle_backtest",
            "advisory_only": True,
        }
    )
    payload = {
        "schema_version": "phase5_release_evidence_identity_v1",
        "evidence_identity": identity,
        "status": "complete",
        "economic_report": {
            "engine": "internal",
            "pair": "EURUSD",
            "status": "completed",
            "realized_pnl_usd": 125.0,
            "max_drawdown_pct": 3.0,
            "turnover_lots": 2.0,
            "trade_count": 8,
        },
        "stress_summary": {"status": "passed", "scenario_count": 1},
    }
    evidence = tmp_path / "economic_evidence.json"
    evidence.write_text(json.dumps(payload), encoding="utf-8")
    validation = validate_economic_evidence(
        path=evidence,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )

    assert validation.valid is False
    assert "economic_evidence_advisory_only" in validation.errors
    assert "economic_source_not_authoritative" in validation.errors


@pytest.mark.parametrize(
    ("runner", "subprocess_target", "engine"),
    [
        (run_lean_harness, "fxstack.backtest.harness.lean.subprocess.run", "lean"),
        (run_nautilus_harness, "fxstack.backtest.harness.nautilus.subprocess.run", "nautilus"),
    ],
)
def test_executed_external_harness_manifest_is_directly_assemblable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: object,
    subprocess_target: str,
    engine: str,
) -> None:
    manifest_path, harness_path, economic_report, harness = _run_external_harness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runner=runner,
        subprocess_target=subprocess_target,
        engine=engine,
    )
    output = tmp_path / f"{engine}_bound.json"

    assembled = assemble_economic_evidence(
        pair="EURUSD",
        model_manifest_path=manifest_path,
        harness_manifest_path=harness_path,
        economic_report_path=economic_report,
        output_path=output,
    )

    expected = active_manifest_identity(manifest_path=manifest_path, pair="EURUSD")
    assert harness.status == "completed"
    assert harness.artifacts["economic_report"] == str(economic_report.resolve())
    assert harness.metadata["bundle_run_id"] == expected.bundle_run_id
    assert harness.metadata["model_set_id"] == expected.model_set_id
    assert harness.metadata["model_manifest_sha256"] == expected.model_manifest_sha256
    assert harness.metadata["artifact_set_sha256"] == expected.artifact_set_sha256
    assert harness.metadata["artifact_sha256"]["economic_report"] == file_sha256(economic_report)
    assert set(harness.artifacts["stress_reports"]) == set(REQUIRED_EXTERNAL_STRESS_SCENARIOS)
    assert assembled["evidence_identity"]["source_kind"] == "independent_execution_harness"
    validation = validate_economic_evidence(
        path=output,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )
    assert validation.valid is True, validation.errors


def test_planned_harness_manifest_cannot_be_assembled(tmp_path: Path) -> None:
    manifest_path = _active_manifest(tmp_path)
    economic_report = tmp_path / "planned_economic.json"
    economic_report.write_text(
        json.dumps(
            {
                "engine": "lean",
                "pair": "EURUSD",
                "status": "completed",
                "realized_pnl_usd": 100.0,
                "max_drawdown_pct": 2.0,
                "turnover_lots": 1.0,
                "trade_count": 8,
            }
        ),
        encoding="utf-8",
    )
    planned = run_lean_harness(
        bundle_dir=tmp_path,
        output_dir=tmp_path / "planned_lean",
        pair="EURUSD",
        execute=False,
    )
    planned_path = tmp_path / "planned_manifest.json"
    planned_path.write_text(json.dumps(planned.to_dict()), encoding="utf-8")

    with pytest.raises(ValueError, match="harness_not_completed"):
        assemble_economic_evidence(
            pair="EURUSD",
            model_manifest_path=manifest_path,
            harness_manifest_path=planned_path,
            economic_report_path=economic_report,
            output_path=tmp_path / "must_not_exist.json",
        )


@pytest.mark.parametrize(
    ("runner", "subprocess_target", "engine"),
    [
        (run_lean_harness, "fxstack.backtest.harness.lean.subprocess.run", "lean"),
        (run_nautilus_harness, "fxstack.backtest.harness.nautilus.subprocess.run", "nautilus"),
    ],
)
def test_zero_exit_without_fresh_engine_report_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: object,
    subprocess_target: str,
    engine: str,
) -> None:
    manifest_path = _active_manifest(tmp_path)
    output_dir = tmp_path / f"{engine}_empty_output"
    report_path = output_dir / "economic.json"
    monkeypatch.setenv(f"FXSTACK_{engine.upper()}_VERSION", f"{engine}-test-1")
    monkeypatch.setattr(
        subprocess_target,
        lambda *args, **kwargs: type(
            "CompletedHarness",
            (),
            {"returncode": 0, "stdout": "completed", "stderr": ""},
        )(),
    )

    with pytest.raises(ValueError, match="did not create a nonempty economic report"):
        runner(
            bundle_dir=tmp_path,
            output_dir=output_dir,
            pair="EURUSD",
            dataset_hash="dataset-sha-1",
            model_manifest_path=manifest_path,
            economic_report_path=report_path,
            execute=True,
        )


@pytest.mark.parametrize(
    ("runner", "engine"),
    [
        (run_lean_harness, "lean"),
        (run_nautilus_harness, "nautilus"),
    ],
)
def test_preexisting_engine_report_or_output_dir_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: object,
    engine: str,
) -> None:
    manifest_path = _active_manifest(tmp_path)
    output_dir = tmp_path / f"{engine}_preexisting_output"
    output_dir.mkdir()
    report_path = output_dir / "economic.json"
    report_path.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(f"FXSTACK_{engine.upper()}_VERSION", f"{engine}-test-1")

    with pytest.raises(ValueError, match="fresh output_dir"):
        runner(
            bundle_dir=tmp_path,
            output_dir=output_dir,
            pair="EURUSD",
            dataset_hash="dataset-sha-1",
            model_manifest_path=manifest_path,
            economic_report_path=report_path,
            execute=True,
        )


def test_normalized_economic_evidence_reopens_source_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest, harness, report, _run = _run_external_harness(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        runner=run_lean_harness,
        subprocess_target="fxstack.backtest.harness.lean.subprocess.run",
        engine="lean",
    )
    output = tmp_path / "bound.json"
    assemble_economic_evidence(
        pair="EURUSD",
        model_manifest_path=manifest,
        harness_manifest_path=harness,
        economic_report_path=report,
        output_path=output,
    )
    source_payload = json.loads(report.read_text(encoding="utf-8"))
    source_payload["realized_pnl_usd"] = 9999.0
    report.write_text(json.dumps(source_payload), encoding="utf-8")
    expected = active_manifest_identity(manifest_path=manifest, pair="EURUSD")

    validation = validate_economic_evidence(
        path=output,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )

    assert validation.valid is False
    assert "economic_source_artifact_hash_mismatch:economic_report" in validation.errors
    assert "economic_report_hash_mismatch" in validation.errors


def test_hand_authored_normalized_evidence_cannot_claim_external_source(tmp_path: Path) -> None:
    manifest = _active_manifest(tmp_path)
    expected = active_manifest_identity(manifest_path=manifest, pair="EURUSD")
    identity = expected.to_dict()
    identity.update(
        {
            "evidence_kind": "economic_validation",
            "source_kind": "independent_execution_harness",
            "advisory_only": False,
        }
    )
    forged = tmp_path / "forged.json"
    forged.write_text(
        json.dumps(
            {
                "status": "complete",
                "evidence_identity": identity,
                "engine": "lean",
                "pair": "EURUSD",
                "realized_pnl_usd": 500.0,
                "max_drawdown_pct": 1.0,
                "turnover_lots": 10.0,
                "trade_count": 100,
                "stress_summary": {
                    "status": "passed",
                    "scenario_count": 6,
                    "worst_realized_pnl_usd": 400.0,
                    "worst_drawdown_pct": 2.0,
                },
            }
        ),
        encoding="utf-8",
    )

    validation = validate_economic_evidence(
        path=forged,
        expected_pair=expected.pair,
        expected_bundle_run_id=expected.bundle_run_id,
        expected_model_set_id=expected.model_set_id,
        expected_model_manifest_sha256=expected.model_manifest_sha256,
        expected_artifact_set_sha256=expected.artifact_set_sha256,
    )

    assert validation.valid is False
    assert "economic_source_artifact_missing:harness_manifest" in validation.errors


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trade_count", 0),
        ("turnover_lots", 0.0),
        ("max_drawdown_pct", 25.0),
        ("realized_pnl_usd", float("nan")),
    ],
)
def test_economic_sufficiency_requires_executed_finite_economics(field: str, value: object) -> None:
    report = {
        "realized_pnl_usd": 100.0,
        "max_drawdown_pct": 2.0,
        "turnover_lots": 1.0,
        "trade_count": 8,
        "stress_summary": {
            "scenario_count": 2,
            "worst_realized_pnl_usd": 50.0,
            "worst_drawdown_pct": 4.0,
        },
    }
    report[field] = value

    passed, _metrics = economic_sufficiency(report)

    assert passed is False


def test_economic_sufficiency_enforces_worst_stress_drawdown() -> None:
    passed, _metrics = economic_sufficiency(
        {
            "realized_pnl_usd": 100.0,
            "max_drawdown_pct": 2.0,
            "turnover_lots": 1.0,
            "trade_count": 8,
            "stress_summary": {
                "scenario_count": 2,
                "worst_realized_pnl_usd": 50.0,
                "worst_drawdown_pct": 25.0,
            },
        }
    )

    assert passed is False
