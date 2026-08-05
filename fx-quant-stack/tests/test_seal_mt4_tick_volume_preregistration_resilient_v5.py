from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SEALER_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v5.py"
)
HANDOFF_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v5.py"
)
EVALUATOR_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v5.py"
)
RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release_v5.py"
COLLECTOR_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v4.py"
)
SCREEN_PATH = (
    REPO_ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation_replacement_v4.py"
)
VERIFIER_PATH = (
    REPO_ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "runtime"
    / "mtvclc_validation_evidence_v3.py"
)
BASE_TEST_PATH = Path(__file__).with_name(
    "test_seal_mt4_tick_volume_preregistration.py"
)


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


seal = _load("mtvclc_successor_v5_sealer_test", SEALER_PATH)
handoff = _load("mtvclc_successor_v5_handoff_test", HANDOFF_PATH)
evaluator = _load("mtvclc_successor_v5_evaluator_test", EVALUATOR_PATH)
release = _load("mtvclc_successor_v5_release_test", RELEASE_PATH)
collector = _load("mtvclc_successor_v4_collector_test", COLLECTOR_PATH)
screen = _load("mtvclc_successor_v4_screen_test", SCREEN_PATH)
verifier = _load("mtvclc_successor_v3_verifier_test", VERIFIER_PATH)
base_test = _load("mtvclc_successor_v5_base_helpers", BASE_TEST_PATH)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    sealed_inputs = tmp_path / "synthetic-sealed-inputs"
    sealed_inputs.mkdir()
    capture, npz, fee = base_test._inputs(sealed_inputs)
    deployed = tmp_path / "synthetic-deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(seal.BRIDGE_EA_REPOSITORY_SOURCE_PATH.read_bytes())
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"test-only synthetic compiled BridgeEA identity")
    return capture, npz, fee, deployed_source, deployed_ex4


def _payload(tmp_path: Path) -> dict:
    inputs = _inputs(tmp_path)
    return seal.build_preregistration(
        cost_capture_json=inputs[0],
        cost_capture_npz=inputs[1],
        fee_attestation=inputs[2],
        bridge_ea_deployed_source=inputs[3],
        bridge_ea_deployed_ex4=inputs[4],
        sealed_at=base_test.SEALED_AT,
    )


def _write_payload(tmp_path: Path, payload: dict) -> Path:
    digest = payload["preregistration_body_sha256"]
    path = tmp_path / f"mtvclc_gap_v3_preregistration_{digest}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def _rehash(payload: dict) -> None:
    body = dict(payload)
    body.pop("preregistration_body_sha256", None)
    payload["preregistration_body_sha256"] = seal.base.canonical_sha256(body)


def test_v5_counts_failed_v4_once_and_binds_immediate_market_policy(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    binding = payload["runtime_policy_binding"]
    window = payload["prospective_window"]
    t0 = seal.base._parse_utc_second(window["t0_utc_inclusive"], label="t0")
    end = seal.base._parse_utc_second(window["end_utc_exclusive"], label="end")

    assert seal.validate_preregistration(payload) is True
    assert payload["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_830,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_874,
    }
    assert len(payload["abandoned_preregistrations"]) == 6
    assert payload["abandoned_preregistrations"][-1] == {
        "preregistration_body_sha256": (
            "45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd"
        ),
        "artifact_file_sha256": (
            "7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55"
        ),
        "attempt_failure_sha256": (
            "56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78"
        ),
        "reason": "unresolved_first_cycle_reservation_after_process_interruption",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "tail_commitment_file_size_bytes": 15_303,
        "tail_commitment_file_sha256": (
            "d770d66bd8cb138b6eb142063aa106788cb2e8be9d4479660ed82c26569b9eb8"
        ),
        "tail_commitment_last_write_utc": "2026-08-04T00:59:20.687771Z",
        "immutable_guard_identity_sha256": (
            "f90fea1134d431feb3ab8a0021bf54159bc249a477490284534d9c444b8c5cf3"
        ),
        "attempted_cells_increment": 44,
        "signal_evaluation_performed": False,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "success_claim_evaluated": False,
    }
    assert end - t0 == timedelta(days=180)
    assert window["interim_signal_or_outcome_evaluation_forbidden"] is True
    assert window["early_success_forbidden"] is True
    assert window["no_optional_extension_or_restart_after_failure"] is True
    assert payload["scope"]["ordered_symbols"] == list(
        seal.IG_MT4_SCALP_SYMBOLS
    )
    assert len(payload["scope"]["cell_order"]) == 44
    assert payload["execution_contract"]["entry_type"] == "immediate_market"
    assert payload["execution_contract"]["pending_orders_forbidden"] is True
    assert binding["entry_type"] == "immediate_market"
    assert binding["immediate_market_trade"] is True
    assert binding["buy_price_basis"] == "authenticated_ask"
    assert binding["sell_price_basis"] == "authenticated_bid"
    assert binding["pending_orders_forbidden"] is True
    assert binding["pending_trades_forbidden"] is True
    assert binding["runtime_or_broker_authority_granted"] is False
    assert not any(payload["authority"].values())


def test_v5_capture_and_preservation_contracts_bind_gap_v5_guard(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    integrity = payload["capture_integrity_contract"]
    preservation = payload["preservation_filename_contract"]

    assert integrity["supervision_guard_identity_filename"] == (
        "collector-guard.identity.gap-v5.v1.json"
    )
    assert integrity["supervision_guard_identity_schema_version"] == (
        "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"
    )
    assert integrity["v5_supervision_guard_identity_required"] is True
    assert integrity["first_cycle_readiness_retry_seconds"] == 1.0
    assert integrity[
        "first_cycle_pre_t0_snapshot_retried_under_same_reservation"
    ] is True
    assert integrity["first_cycle_readiness_re_reservation_forbidden"] is True
    assert integrity[
        "first_cycle_readiness_proof_committed_in_every_cycle_part"
    ] is True
    assert preservation["schema_version"] == (
        "fxstack.scalp.mtvclc_preservation_filenames.v5"
    )
    assert preservation["guard_identity_filename"] == (
        "collector-guard.identity.gap-v5.v1.json"
    )
    assert preservation["guard_identity_schema_version"] == (
        "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"
    )
    assert preservation["preregistration_filename_template"].startswith(
        "mtvclc_gap_v3_preregistration_"
    )
    assert preservation["capture_root_name_template"].startswith(
        "mtvclc_prospective_capture_gap_v3_"
    )

    forged = json.loads(json.dumps(payload))
    forged["runtime_policy_binding"]["pending_orders_forbidden"] = False
    _rehash(forged)
    assert seal.validate_preregistration(forged) is False


def test_v5_handoff_evaluator_release_chain_is_public_and_v3_bound(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    artifact = _write_payload(tmp_path, payload)
    binding = handoff.load_preregistration(
        artifact,
        profile=handoff.PROFILE_GAP_V5,
    )

    assert binding.preregistration_body_sha256 == (
        payload["preregistration_body_sha256"]
    )
    assert binding.profile == handoff.PROFILE_GAP_V5
    assert handoff.collector.TOOL_PATH.name.endswith("_v4.py")
    assert evaluator.handoff.PROFILE_GAP_V3 == handoff.PROFILE_GAP_V5
    assert release.handoff.PROFILE_GAP_V3 == handoff.PROFILE_GAP_V5
    assert release.public.MTVCLC_VALIDATION_EVIDENCE_SCHEMA.endswith(".v3")
    assert release.public.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA.endswith(".v3")
    assert release.public.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA.endswith(".v3")
    assert release.public.WILSON_FAMILY_ATTEMPTED_CELLS == 4_874
    assert release.derivation_identity()["private_key_access_on_import"] is False
    assert release.derivation_identity()["authority_granted"] is False


@pytest.mark.parametrize(
    ("module", "raw_name", "assertion_name", "reason"),
    (
        (
            screen,
            "_V3_TEMPLATE_RAW",
            "_assert_pinned_v3_template",
            "v3_screen_template_identity_invalid",
        ),
        (
            verifier,
            "_V2_TEMPLATE_RAW",
            "_assert_pinned_v2_template",
            "v2_public_verifier_template_identity_invalid",
        ),
        (
            collector,
            "_V3_TEMPLATE_RAW",
            "_assert_pinned_v3_template",
            "v3_collector_template_identity_invalid",
        ),
        (
            seal,
            "_V3_TEMPLATE_RAW",
            "_assert_pinned_v3_template",
            "v3_sealer_template_identity_invalid",
        ),
        (
            handoff,
            "_V3_TEMPLATE_RAW",
            "_assert_pinned_v3_template",
            "v3_handoff_template_identity_invalid",
        ),
        (
            evaluator,
            "_V3_TEMPLATE_RAW",
            "_assert_pinned_v3_template",
            "v3_evaluator_template_identity_invalid",
        ),
        (
            release,
            "_V3_TEMPLATE_RAW",
            "_assert_pinned_v3_template",
            "v3_release_template_identity_invalid",
        ),
    ),
    ids=("screen", "verifier", "collector", "sealer", "handoff", "evaluator", "release"),
)
def test_every_dynamic_adapter_rejects_template_drift_before_exec(
    module: object,
    raw_name: str,
    assertion_name: str,
    reason: str,
) -> None:
    raw = getattr(module, raw_name)
    assertion = getattr(module, assertion_name)
    with pytest.raises(RuntimeError, match=reason):
        assertion(raw + b"drift")  # type: ignore[operator]


def test_v5_handoff_rejects_old_or_mixed_guard_topology(tmp_path: Path) -> None:
    required = {
        handoff.MANIFEST_FILENAME,
        handoff.DATA_WRITER_LOCK_FILENAME,
        handoff.START_EDGE_RECEIPT_FILENAME,
        handoff.TAIL_COMMITMENT_FILENAME,
        handoff.POST_WINDOW_FINALIZATION_FILENAME,
    }

    def topology(root: Path, *, include_v5: bool, include_v3: bool) -> None:
        root.mkdir()
        (root / handoff.CHUNK_DIRECTORY).mkdir()
        for name in required:
            (root / name).write_bytes(b"")
        if include_v5:
            (root / "collector-guard.identity.gap-v5.v1.json").write_bytes(b"")
        if include_v3:
            (root / "collector-guard.identity.gap-v3.v1.json").write_bytes(b"")

    old_only = tmp_path / "old-only"
    topology(old_only, include_v5=False, include_v3=True)
    with pytest.raises(handoff.HandoffRefusal, match="capture_final_topology_invalid"):
        handoff._validate_final_state(old_only, binding=None)  # type: ignore[arg-type]

    mixed = tmp_path / "mixed"
    topology(mixed, include_v5=True, include_v3=True)
    with pytest.raises(handoff.HandoffRefusal, match="capture_final_topology_invalid"):
        handoff._validate_final_state(mixed, binding=None)  # type: ignore[arg-type]


def test_v5_guard_identity_schema_is_mandatory(tmp_path: Path) -> None:
    payload = _payload(tmp_path)
    binding = handoff.load_preregistration(
        _write_payload(tmp_path, payload),
        profile=handoff.PROFILE_GAP_V5,
    )
    guard_body = {
        "schema_version": handoff.GUARD_IDENTITY_SCHEMA_VERSION,
        "collector_source_sha256": binding.collector_source_sha256,
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        **binding.producer_receipt_fields(),
        "collection_only": True,
        "activation_authorized": False,
        "runtime_authorized": False,
        "broker_trade_authorized": False,
    }
    guard = tmp_path / handoff.GUARD_IDENTITY_FILENAME
    guard.write_bytes(handoff.canonical_json_bytes(guard_body) + b"\n")
    assert handoff._validate_guard_identity(guard, binding=binding)

    guard_body["schema_version"] = (
        "fxstack.mtvclc_collector_guard_identity.gap_v3.v1"
    )
    guard.write_bytes(handoff.canonical_json_bytes(guard_body) + b"\n")
    with pytest.raises(
        handoff.HandoffRefusal,
        match="capture_guard_identity_contract_invalid",
    ):
        handoff._validate_guard_identity(guard, binding=binding)
