from __future__ import annotations

import base64
from dataclasses import dataclass
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.runtime import mtvclc_runtime_release as runtime_release  # noqa: E402
from fxstack.runtime import mtvclc_validation_evidence_v3 as evidence_v3  # noqa: E402
from fxstack.runtime.scalp_engine_identity import (  # noqa: E402
    SCALP_ENGINE_COMPONENTS,
    SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS,
    production_scalp_engine_identity,
)


def _load_tool() -> Any:
    path = REPO_ROOT / "tools" / "mtvclc_runtime_release.py"
    spec = importlib.util.spec_from_file_location(
        "mtvclc_runtime_release_test_target", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


issuer = _load_tool()


def _load_base_sealer_test_helpers() -> Any:
    path = (
        REPO_ROOT
        / "fx-quant-stack"
        / "tests"
        / "test_seal_mt4_tick_volume_preregistration.py"
    )
    spec = importlib.util.spec_from_file_location(
        "mtvclc_runtime_release_base_sealer_helpers", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


base_sealer_test = _load_base_sealer_test_helpers()

NOW = 1_800_000_000.0
EVIDENCE_ISSUED = NOW - 300.0
EVIDENCE_EXPIRES = NOW + 10_000.0
GENERATION_ID = "mtvclc-runtime-generation-test"


def _write_canonical(path: Path, payload: dict[str, Any]) -> Path:
    path.write_bytes(evidence_v3.canonical_json_bytes(payload) + b"\n")
    return path


def _write_keys(
    root: Path, *, prefix: str, private_key: Ed25519PrivateKey
) -> tuple[Path, Path]:
    private_path = root / f"{prefix}-private.pem"
    public_path = root / f"{prefix}-public.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _file_identity(name: str, digest: str) -> dict[str, Any]:
    return {"filename": name, "sha256": digest, "size_bytes": 10}


def _cost_row(symbol: str, *, index: int) -> dict[str, Any]:
    spread = 1.0 + index / 100.0
    commission = 0.2
    financing = 0.0
    adverse = 1.0
    pre_conversion = spread + commission + financing + adverse
    pnl_currency = symbol[3:]
    conversion_applies = pnl_currency != "USD"
    conversion_rate = 0.005
    screen_rate = conversion_rate if conversion_applies else 0.0
    target = 4.0 * pre_conversion
    stop = 8.0 * pre_conversion
    break_even = (stop * (1.0 + screen_rate) + pre_conversion) / (
        target * (1.0 - screen_rate) + stop * (1.0 + screen_rate)
    )
    return {
        "p90_ig_spread_bps": spread,
        "commission_bps_per_round_trip": commission,
        "financing_bps_per_trade": financing,
        "fixed_adverse_execution_debit_bps": adverse,
        "pre_conversion_geometry_cost_bps": pre_conversion,
        "profit_loss_currency": pnl_currency,
        "account_currency": "USD",
        "conversion_rate_of_absolute_profit_or_loss": conversion_rate,
        "convert_on_close_charge_fraction_for_screen": screen_rate,
        "conversion_applies": conversion_applies,
        "conversion_adjusted_break_even_win_probability": break_even,
        "commission_status": "explicit_source_attested",
        "financing_status": "structurally_avoided_by_fixed_rollover_guard",
        "conversion_status": (
            "debit_absolute_profit_or_loss_when_account_currency_differs"
        ),
    }


def _valid_evidence(preregistration: dict[str, Any]) -> dict[str, Any]:
    rows = {
        symbol: _cost_row(symbol, index=index)
        for index, symbol in enumerate(evidence_v3.IG_MT4_SCALP_SYMBOLS)
    }
    row_hashes = {
        symbol: evidence_v3.canonical_sha256(row)
        for symbol, row in rows.items()
    }
    cells: list[dict[str, Any]] = []
    reservations = 100
    wins = 100
    bound = evidence_v3.wilson_one_sided_lower(wins=wins, trials=reservations)
    for symbol in evidence_v3.IG_MT4_SCALP_SYMBOLS:
        for side in ("BUY", "SELL"):
            cells.append(
                {
                    "config_id": evidence_v3.MTVCLC_CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "source_ready": True,
                    "reservations": reservations,
                    "wins": wins,
                    "independent_days": 100,
                    "full_target_rate": 1.0,
                    "win_probability_wilson_lower": bound,
                    "base_break_even_probability": rows[symbol][
                        "conversion_adjusted_break_even_win_probability"
                    ],
                    "mean_net_bps": 1.0,
                    "passes_fixed_cell_screen": True,
                }
            )
    artifacts = {
        "preregistration_body_sha256": preregistration[
            "preregistration_body_sha256"
        ],
        "preregistration_artifact_sha256": (
            runtime_release._preregistration_artifact_sha256(preregistration)
        ),
        "handoff_body_sha256": "3" * 64,
        "handoff_artifact_sha256": "4" * 64,
        "capture_inventory_sha256": "5" * 64,
        "evaluator_source_sha256": "6" * 64,
        "report_body_sha256": "7" * 64,
        "report_artifact_sha256": "8" * 64,
        "evidence_binding_sha256": "9" * 64,
        "reservation_ledger_sha256": "a" * 64,
        "outcome_ledger_sha256": "b" * 64,
        "cell_ledger_sha256": "c" * 64,
    }
    total_trades = reservations * 44
    return {
        "schema_version": evidence_v3.MTVCLC_VALIDATION_EVIDENCE_SCHEMA,
        "account_mode": evidence_v3.MTVCLC_ACCOUNT_MODE,
        "strategy": {
            "strategy_id": evidence_v3.MTVCLC_STRATEGY_ID,
            "strategy_version": evidence_v3.MTVCLC_STRATEGY_VERSION,
            "config_id": evidence_v3.MTVCLC_CONFIG_ID,
            "config_sha256": runtime_release.PRODUCTION_MTVCLC_CONFIG_SHA256,
            "source_contract_id": evidence_v3.MTVCLC_SOURCE_CONTRACT_ID,
            "activity_metric_id": evidence_v3.MTVCLC_ACTIVITY_METRIC_ID,
            "attempt_manifest_sha256": "d" * 64,
        },
        "scope": {
            "venue_id": evidence_v3.IG_MT4_VENUE_ID,
            "scope_version": evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
            "symbol_scope": list(evidence_v3.IG_MT4_SCALP_SYMBOLS),
            "cell_order": [
                {
                    "config_id": evidence_v3.MTVCLC_CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                }
                for symbol in evidence_v3.IG_MT4_SCALP_SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "execution_contract": dict(evidence_v3.EXPECTED_EXECUTION_CONTRACT),
        "attempt_accounting": dict(evidence_v3.EXPECTED_ATTEMPT_ACCOUNTING),
        "wilson_allocation": {
            "method": evidence_v3.WILSON_INTERVAL_METHOD,
            "family_confidence": evidence_v3.WIN_PROBABILITY_FAMILY_CONFIDENCE,
            "attempted_cells": evidence_v3.WILSON_FAMILY_ATTEMPTED_CELLS,
            "alpha_allocation": evidence_v3.WILSON_ALPHA_ALLOCATION,
        },
        "sealed_gates": dict(evidence_v3.EXPECTED_SEALED_GATES),
        "artifacts": artifacts,
        "costs": {
            "capture_bundle": {
                "capture_json": _file_identity("capture.json", "e" * 64),
                "capture_npz": _file_identity("capture.npz", "f" * 64),
                "capture_payload_sha256": "0" * 64,
                "capture_mode": "authenticated_same_source_db_history",
                "scope_version": evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
                "venue_id": evidence_v3.IG_MT4_VENUE_ID,
            },
            "fee_attestation": _file_identity("fees.json", "1" * 64),
            "cost_policy_sha256": "2" * 64,
            "cost_rows_sha256": evidence_v3.canonical_sha256(rows),
            "cost_row_sha256_by_symbol": row_hashes,
            "rows": rows,
        },
        "overall": {
            "source_scope_ready": True,
            "source_errors": [],
            "all_44_cells_pass": True,
            "total_trades": total_trades,
            "total_independent_utc_days": 100,
            "all_preregistered_success_gates_pass": True,
        },
        "cells": cells,
        "ledger_authentication": {
            "schema_version": evidence_v3.LEDGER_AUTHENTICATION_SCHEMA,
            "reservation_rows": total_trades,
            "outcome_rows": total_trades,
            "cell_rows": 44,
            "duplicate_reservation_keys": 0,
            "duplicate_outcome_keys": 0,
            "missing_outcomes": 0,
            "orphan_outcomes": 0,
            "inconsistent_pairs": 0,
            "empty_ledgers_rejected": True,
            "cell_summaries_recomputed_exclusively_from_ledgers": True,
            "screen_result_bundle_validated": True,
            "reservation_ledger_sha256": artifacts[
                "reservation_ledger_sha256"
            ],
            "outcome_ledger_sha256": artifacts["outcome_ledger_sha256"],
            "cell_ledger_sha256": artifacts["cell_ledger_sha256"],
            "screen_source_sha256": "3" * 64,
        },
        "authority": dict(evidence_v3.NO_RUNTIME_AUTHORITY),
    }


def _signed_evidence_bundle(
    *,
    evidence_private_key: Ed25519PrivateKey,
    preregistration: dict[str, Any],
) -> dict[str, Any]:
    evidence = _valid_evidence(preregistration)
    assert evidence_v3.mtvclc_evidence_error(evidence) == ""
    certificate: dict[str, Any] = {
        "schema_version": evidence_v3.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA,
        "generation_id": GENERATION_ID,
        "strategy_id": evidence_v3.MTVCLC_STRATEGY_ID,
        "strategy_version": evidence_v3.MTVCLC_STRATEGY_VERSION,
        "config_id": evidence_v3.MTVCLC_CONFIG_ID,
        "config_sha256": runtime_release.PRODUCTION_MTVCLC_CONFIG_SHA256,
        "evaluator_source_sha256": evidence["artifacts"][
            "evaluator_source_sha256"
        ],
        "venue_id": evidence_v3.IG_MT4_VENUE_ID,
        "account_mode": evidence_v3.MTVCLC_ACCOUNT_MODE,
        "scope_version": evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(evidence_v3.IG_MT4_SCALP_SYMBOLS),
        "issued_at_epoch": EVIDENCE_ISSUED,
        "expires_at_epoch": EVIDENCE_EXPIRES,
        "signing_key_id": evidence_v3.ed25519_public_key_id(
            evidence_private_key.public_key()
        ),
        "evidence": evidence,
        "evidence_sha256": evidence_v3.canonical_sha256(evidence),
        "authority": dict(evidence_v3.NO_RUNTIME_AUTHORITY),
    }
    certificate[evidence_v3.CERTIFICATE_SHA256_FIELD] = (
        evidence_v3.certificate_body_sha256(certificate)
    )
    certificate[evidence_v3.CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(
        evidence_private_key.sign(evidence_v3.canonical_json_bytes(certificate))
    ).decode("ascii")
    bundle: dict[str, Any] = {
        "schema_version": evidence_v3.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
        "certificate": certificate,
    }
    bundle[evidence_v3.BUNDLE_SHA256_FIELD] = evidence_v3.bundle_body_sha256(
        bundle
    )
    return bundle


def _engine_tree(root: Path) -> tuple[Path, Path]:
    package_root = root / "package"
    repository_root = root / "repository"
    for index, relative in enumerate(SCALP_ENGINE_COMPONENTS):
        selected_root = (
            repository_root
            if relative in SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS
            else package_root
        )
        path = selected_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "strategy/mtvclc.py":
            path.write_bytes(
                REPO_ROOT.joinpath(
                    "fx-quant-stack", "src", "fxstack", relative
                ).read_bytes()
            )
        else:
            path.write_text(
                f"# exact synthetic component {index}: {relative}\n",
                encoding="utf-8",
            )
    return package_root, repository_root


def _v5_preregistration(
    root: Path, *, package_root: Path, repository_root: Path
) -> tuple[Path, dict[str, Any]]:
    root.mkdir()
    (root / "sealed-inputs").mkdir()
    capture, npz, fee = base_sealer_test._inputs(root / "sealed-inputs")
    deployed = root / "deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(
        issuer.preregistration_v5.BRIDGE_EA_REPOSITORY_SOURCE_PATH.read_bytes()
    )
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"synthetic test-only compiled BridgeEA identity")
    source_snapshots, input_snapshots = issuer.preregistration_v5._input_snapshots(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
    )
    payload = issuer.preregistration_v5.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
        sealed_at=base_sealer_test.SEALED_AT,
    )
    engine = production_scalp_engine_identity(
        package_root=package_root,
        repository_root=repository_root,
    )
    identities = payload["source_identities"]
    runtime_context = identities["production_runtime_context"]
    runtime_context["engine_identity"] = json.loads(
        json.dumps(engine.to_dict(), allow_nan=False, sort_keys=True)
    )
    payload["runtime_policy_binding"] = (
        issuer.preregistration_v5._runtime_policy_binding(
            source_identities=identities,
            engine_identity=runtime_context["engine_identity"],
        )
    )
    payload.pop("preregistration_body_sha256", None)
    payload["preregistration_body_sha256"] = (
        issuer.preregistration_v5.base.canonical_sha256(payload)
    )
    assert issuer.preregistration_v5.validate_preregistration(payload)
    output_root = root / "published-preregistration"
    output_root.mkdir()
    path = issuer.preregistration_v5.atomic_publish(
        output_root=output_root,
        payload=payload,
        input_paths=(capture, npz, fee, deployed_source, deployed_ex4),
        source_snapshots=source_snapshots,
        input_snapshots=input_snapshots,
        clock=lambda: base_sealer_test.SEALED_AT.timestamp(),
    )
    assert path.read_bytes() == runtime_release._preregistration_artifact_bytes(
        payload
    )
    return path, payload


@dataclass(frozen=True, slots=True)
class CeremonyFixture:
    preregistration_path: Path
    evidence_bundle_path: Path
    evidence_public_key_path: Path
    release_public_key_path: Path
    release_private_key_path: Path
    package_root: Path
    repository_root: Path

    def common(self) -> dict[str, Any]:
        return {
            "preregistration_path": self.preregistration_path,
            "evidence_bundle_path": self.evidence_bundle_path,
            "evidence_public_key_path": self.evidence_public_key_path,
            "release_public_key_path": self.release_public_key_path,
            "package_root": self.package_root,
            "repository_root": self.repository_root,
        }


@pytest.fixture
def ceremony(tmp_path: Path) -> CeremonyFixture:
    evidence_private = Ed25519PrivateKey.generate()
    release_private = Ed25519PrivateKey.generate()
    evidence_private_path, evidence_public_path = _write_keys(
        tmp_path, prefix="evidence", private_key=evidence_private
    )
    release_private_path, release_public_path = _write_keys(
        tmp_path, prefix="release", private_key=release_private
    )
    assert evidence_private_path.is_file()
    package_root, repository_root = _engine_tree(tmp_path / "engine")
    preregistration_path, preregistration = _v5_preregistration(
        tmp_path / "prereg",
        package_root=package_root,
        repository_root=repository_root,
    )
    evidence_bundle_path = _write_canonical(
        tmp_path / "evidence-v3.json",
        _signed_evidence_bundle(
            evidence_private_key=evidence_private,
            preregistration=preregistration,
        ),
    )
    return CeremonyFixture(
        preregistration_path=preregistration_path,
        evidence_bundle_path=evidence_bundle_path,
        evidence_public_key_path=evidence_public_path,
        release_public_key_path=release_public_path,
        release_private_key_path=release_private_path,
        package_root=package_root,
        repository_root=repository_root,
    )


def _prepare(
    ceremony: CeremonyFixture,
    *,
    output: Path,
    now: float = NOW,
    previous_registry_path: Path | None = None,
    bootstrap_registry: bool = True,
) -> tuple[Path, dict[str, Any], Any]:
    return issuer.prepare_issuance_request(
        **ceremony.common(),
        previous_registry_path=previous_registry_path,
        bootstrap_registry=bootstrap_registry,
        validity_secs=1_800.0,
        output_path=output,
        now_epoch=now,
    )


def _issue(
    ceremony: CeremonyFixture,
    *,
    request: Path,
    output: Path,
    now: float = NOW + 1.0,
    previous_registry_path: Path | None = None,
    bootstrap_registry: bool = True,
) -> tuple[Path, dict[str, Any]]:
    return issuer.issue_runtime_release_bundle(
        **ceremony.common(),
        request_path=request,
        release_signing_key_path=ceremony.release_private_key_path,
        previous_registry_path=previous_registry_path,
        bootstrap_registry=bootstrap_registry,
        output_path=output,
        now_epoch=now,
    )


def test_prepare_and_explicit_issue_build_verifier_valid_bootstrap(
    ceremony: CeremonyFixture, tmp_path: Path
) -> None:
    request_path, request, engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )

    assert request_path.is_file()
    assert request["schema_version"].endswith(".v2")
    assert request["registry_mode"] == "bootstrap"
    assert request["authority"] == issuer.NO_ISSUANCE_AUTHORITY
    assert not any(request["authority"].values())
    assert request["engine_binding"]["engine_sha256"] == engine.engine_sha256
    assert request["evidence_bundle"][evidence_v3.BUNDLE_SHA256_FIELD] == (
        request["unsigned_certificate"]["evidence_binding"][
            "bundle_body_sha256"
        ]
    )
    assert request["unsigned_revocation_registry"]["registry_revision"] == 1
    assert request["unsigned_revocation_registry"][
        "previous_registry_sha256"
    ] == "0" * 64
    assert runtime_release.CERTIFICATE_SIGNATURE_FIELD not in request[
        "unsigned_certificate"
    ]
    assert runtime_release.REGISTRY_SIGNATURE_FIELD not in request[
        "unsigned_revocation_registry"
    ]
    assert ceremony.release_private_key_path.name not in (
        evidence_v3.canonical_json_bytes(request).decode("utf-8")
    )
    assert request["unsigned_certificate"]["execution_contract"] == (
        runtime_release.EXPECTED_IMMEDIATE_TRADE_CONTRACT
    )
    preregistration_binding = request["validated_preregistration_binding"]
    assert preregistration_binding == request["unsigned_certificate"][
        "validated_preregistration_binding"
    ]
    assert set(preregistration_binding) == {
        "schema_version",
        "preregistration",
        "preregistration_tool_revision",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "runtime_policy_binding",
        "sealed_engine_identity",
    }
    assert request["public_input_manifest"]["preregistration"]["sha256"] == (
        preregistration_binding["preregistration_artifact_sha256"]
    )

    output, bundle = _issue(
        ceremony, request=request_path, output=tmp_path / "release.json"
    )

    assert output.is_file()
    assert bundle["schema_version"].endswith(".v2")
    assert "preregistration" not in bundle
    assert set(bundle) == {
        "schema_version",
        "evidence_bundle",
        "certificate",
        "revocation_registry",
        runtime_release.BUNDLE_SHA256_FIELD,
    }
    assert bundle["certificate"]["authority"] == (
        runtime_release.RUNTIME_RELEASE_AUTHORITY
    )
    assert not bundle["certificate"]["authority"]["individual_trade_authorized"]
    assert not bundle["certificate"]["authority"]["broker_trade_authorized"]
    assert len(bundle["certificate"]["cost_binding"]["calibrations"]) == 22
    assert len(bundle["certificate"]["qualification_surface"]["cells"]) == 44


def test_issued_bundle_passes_public_runtime_verifier(
    ceremony: CeremonyFixture, tmp_path: Path
) -> None:
    request_path, request, engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )
    _output, bundle = _issue(
        ceremony, request=request_path, output=tmp_path / "release.json"
    )
    _release_loaded, release_public_key = issuer._load_public_key(
        ceremony.release_public_key_path, label="test_release"
    )
    _evidence_loaded, evidence_public_key = issuer._load_public_key(
        ceremony.evidence_public_key_path, label="test_evidence"
    )
    certificate = request["unsigned_certificate"]
    expectation = runtime_release.MTVCLCRuntimeReleaseExpectation(
        generation_id=certificate["generation_id"],
        config_sha256=certificate["config_sha256"],
        evaluator_source_sha256=certificate["evaluator_source_sha256"],
        engine_sha256=engine.engine_sha256,
        engine_component_sha256=engine.component_sha256,
    )

    verified = runtime_release.verify_mtvclc_runtime_release(
        bundle=bundle,
        release_public_key=release_public_key,
        evidence_public_key=evidence_public_key,
        expectation=expectation,
        now_epoch=NOW + 1.0,
    )

    assert verified.valid
    assert verified.authenticated
    assert verified.revocation_verified
    assert verified.release_bundle_sha256 == bundle[
        runtime_release.BUNDLE_SHA256_FIELD
    ]
    assert verified.maximum_account_currency_risk_per_trade == 1.0
    assert verified.symbol_scope == evidence_v3.IG_MT4_SCALP_SYMBOLS
    assert verified.preregistration_body_sha256 == bundle["certificate"][
        "validated_preregistration_binding"
    ]["preregistration_body_sha256"]
    assert len(verified.runtime_policy_binding_sha256) == 64
    assert len(verified.sealed_engine_identity_sha256) == 64


def test_rotation_chains_and_revokes_previous_active_certificate(
    ceremony: CeremonyFixture, tmp_path: Path
) -> None:
    first_request, _request, _engine = _prepare(
        ceremony, output=tmp_path / "request-1.json"
    )
    first_release, first_bundle = _issue(
        ceremony,
        request=first_request,
        output=tmp_path / "release-1.json",
    )
    second_request, _request2, _engine2 = _prepare(
        ceremony,
        output=tmp_path / "request-2.json",
        now=NOW + 100.0,
        previous_registry_path=first_release,
        bootstrap_registry=False,
    )

    _second_release, second_bundle = _issue(
        ceremony,
        request=second_request,
        output=tmp_path / "release-2.json",
        now=NOW + 101.0,
        previous_registry_path=first_release,
        bootstrap_registry=False,
    )

    first_registry = first_bundle["revocation_registry"]
    second_registry = second_bundle["revocation_registry"]
    assert second_registry["registry_revision"] == 2
    assert second_registry["previous_registry_sha256"] == first_registry[
        runtime_release.REGISTRY_SHA256_FIELD
    ]
    assert second_registry["revoked_certificate_sha256s"] == [
        first_bundle["certificate"][runtime_release.CERTIFICATE_SHA256_FIELD]
    ]
    assert second_registry["revoked_certificate_sha256s"] == sorted(
        second_registry["revoked_certificate_sha256s"]
    )


def test_changed_engine_refuses_before_private_key_access(
    ceremony: CeremonyFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, _request, _engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )
    component = ceremony.package_root / "strategy" / "mtvclc.py"
    component.write_text("# changed after prepare\n", encoding="utf-8")
    private_accessed = False

    def forbidden_private_key(_path: str | Path) -> Any:
        nonlocal private_accessed
        private_accessed = True
        raise AssertionError("private key must not be opened")

    monkeypatch.setattr(issuer, "_load_private_key", forbidden_private_key)

    with pytest.raises(
        issuer.MTVCLCRuntimeReleaseRefusal,
        match="preregistration_production_engine_identity_mismatch",
    ):
        _issue(
            ceremony,
            request=request_path,
            output=tmp_path / "must-not-exist.json",
        )

    assert not private_accessed
    assert not (tmp_path / "must-not-exist.json").exists()


@pytest.mark.parametrize(
    ("mode", "reason"),
    (
        ("swapped", "preregistration_evidence_body_sha256_mismatch"),
        ("body", "preregistration_v5_validation_failed"),
        ("artifact", "preregistration_canonical_bytes_invalid"),
    ),
)
def test_preregistration_preflight_failures_never_open_private_key(
    ceremony: CeremonyFixture,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    reason: str,
) -> None:
    request_path, _request, _engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )
    payload = json.loads(
        ceremony.preregistration_path.read_text(encoding="utf-8")
    )
    if mode == "swapped":
        payload["sealed_at_utc"] = "2026-08-03T14:00:01Z"
        payload.pop("preregistration_body_sha256", None)
        payload["preregistration_body_sha256"] = (
            issuer.preregistration_v5.base.canonical_sha256(payload)
        )
        assert issuer.preregistration_v5.validate_preregistration(payload)
    elif mode == "body":
        payload["preregistration_body_sha256"] = "f" * 64
    alternate = tmp_path / (
        "mtvclc_gap_v3_preregistration_"
        f"{payload['preregistration_body_sha256']}.json"
    )
    if mode == "artifact":
        alternate.write_bytes(evidence_v3.canonical_json_bytes(payload) + b"\n")
    else:
        alternate.write_bytes(
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
        )
    private_accessed = False

    def forbidden_private_key(_path: str | Path) -> Any:
        nonlocal private_accessed
        private_accessed = True
        raise AssertionError("private key must not be opened")

    monkeypatch.setattr(issuer, "_load_private_key", forbidden_private_key)
    common = ceremony.common()
    common["preregistration_path"] = alternate

    with pytest.raises(issuer.MTVCLCRuntimeReleaseRefusal, match=reason):
        issuer.issue_runtime_release_bundle(
            **common,
            request_path=request_path,
            release_signing_key_path=ceremony.release_private_key_path,
            bootstrap_registry=True,
            output_path=tmp_path / "must-not-exist.json",
            now_epoch=NOW + 1.0,
        )

    assert not private_accessed
    assert not (tmp_path / "must-not-exist.json").exists()


def test_tampered_request_refuses_before_private_key_access(
    ceremony: CeremonyFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, request, _engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )
    request_path.chmod(0o600)
    request["unsigned_certificate"]["execution_contract"][
        "pending_trades_forbidden"
    ] = False
    _write_canonical(request_path, request)
    private_accessed = False

    def forbidden_private_key(_path: str | Path) -> Any:
        nonlocal private_accessed
        private_accessed = True
        raise AssertionError("private key must not be opened")

    monkeypatch.setattr(issuer, "_load_private_key", forbidden_private_key)

    with pytest.raises(
        issuer.MTVCLCRuntimeReleaseRefusal,
        match="issuance_request_hash_invalid",
    ):
        _issue(
            ceremony,
            request=request_path,
            output=tmp_path / "must-not-exist.json",
        )

    assert not private_accessed


def test_changed_evidence_refuses_before_private_key_access(
    ceremony: CeremonyFixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_path, _request, _engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )
    evidence_bundle = json.loads(
        ceremony.evidence_bundle_path.read_text(encoding="utf-8")
    )
    evidence_bundle["certificate"]["evidence"]["cells"][0]["wins"] = 99
    _write_canonical(ceremony.evidence_bundle_path, evidence_bundle)
    private_accessed = False

    def forbidden_private_key(_path: str | Path) -> Any:
        nonlocal private_accessed
        private_accessed = True
        raise AssertionError("private key must not be opened")

    monkeypatch.setattr(issuer, "_load_private_key", forbidden_private_key)

    with pytest.raises(
        issuer.MTVCLCRuntimeReleaseRefusal,
        match="evidence_verification_failed",
    ):
        _issue(
            ceremony,
            request=request_path,
            output=tmp_path / "must-not-exist.json",
        )

    assert not private_accessed


def test_same_release_and_evidence_key_refuses_in_prepare(
    ceremony: CeremonyFixture, tmp_path: Path
) -> None:
    with pytest.raises(
        issuer.MTVCLCRuntimeReleaseRefusal,
        match="release_and_evidence_keys_not_distinct",
    ):
        issuer.prepare_issuance_request(
            preregistration_path=ceremony.preregistration_path,
            evidence_bundle_path=ceremony.evidence_bundle_path,
            evidence_public_key_path=ceremony.evidence_public_key_path,
            release_public_key_path=ceremony.evidence_public_key_path,
            package_root=ceremony.package_root,
            repository_root=ceremony.repository_root,
            bootstrap_registry=True,
            output_path=tmp_path / "must-not-exist.json",
            now_epoch=NOW,
        )


def test_issue_rejects_mismatched_release_private_key(
    ceremony: CeremonyFixture, tmp_path: Path
) -> None:
    request_path, _request, _engine = _prepare(
        ceremony, output=tmp_path / "request.json"
    )
    wrong_private, _wrong_public = _write_keys(
        tmp_path,
        prefix="wrong-release",
        private_key=Ed25519PrivateKey.generate(),
    )

    with pytest.raises(
        issuer.MTVCLCRuntimeReleaseRefusal,
        match="issuance_release_keypair_mismatch",
    ):
        issuer.issue_runtime_release_bundle(
            **ceremony.common(),
            request_path=request_path,
            release_signing_key_path=wrong_private,
            bootstrap_registry=True,
            output_path=tmp_path / "must-not-exist.json",
            now_epoch=NOW + 1.0,
        )


def test_prepare_refuses_release_expiry_beyond_evidence(
    ceremony: CeremonyFixture, tmp_path: Path
) -> None:
    with pytest.raises(
        issuer.MTVCLCRuntimeReleaseRefusal,
        match="runtime_release_time_window_invalid",
    ):
        issuer.prepare_issuance_request(
            **ceremony.common(),
            bootstrap_registry=True,
            validity_secs=EVIDENCE_EXPIRES - NOW + 1.0,
            output_path=tmp_path / "must-not-exist.json",
            now_epoch=NOW,
        )
