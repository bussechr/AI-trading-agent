from __future__ import annotations

from datetime import timedelta
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SEALER_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v4.py"
)
HANDOFF_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v4.py"
)
EVALUATOR_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v4.py"
)
RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release_v4.py"
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


seal = _load("mtvclc_runtime_bound_v4_sealer_test", SEALER_PATH)
handoff = _load("mtvclc_runtime_bound_v4_handoff_test", HANDOFF_PATH)
evaluator = _load("mtvclc_runtime_bound_v4_evaluator_test", EVALUATOR_PATH)
release = _load("mtvclc_runtime_bound_v4_release_test", RELEASE_PATH)
base_test = _load("mtvclc_runtime_bound_v4_base_helpers", BASE_TEST_PATH)


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


def _payload(tmp_path: Path) -> tuple[dict, tuple[Path, ...]]:
    inputs = _inputs(tmp_path)
    payload = seal.build_preregistration(
        cost_capture_json=inputs[0],
        cost_capture_npz=inputs[1],
        fee_attestation=inputs[2],
        bridge_ea_deployed_source=inputs[3],
        bridge_ea_deployed_ex4=inputs[4],
        sealed_at=base_test.SEALED_AT,
    )
    return payload, inputs


def _publication_snapshots(
    inputs: tuple[Path, ...],
):  # type: ignore[no-untyped-def]
    return seal._input_snapshots(
        cost_capture_json=inputs[0],
        cost_capture_npz=inputs[1],
        fee_attestation=inputs[2],
        bridge_ea_deployed_source=inputs[3],
        bridge_ea_deployed_ex4=inputs[4],
    )


def _rehash(payload: dict) -> None:
    body = dict(payload)
    body.pop("preregistration_body_sha256", None)
    payload["preregistration_body_sha256"] = seal.base.canonical_sha256(body)


def test_v4_bootstrap_exactly_binds_active_mtvclc_policy() -> None:
    policy = seal._implementation._production_strategy
    identities = seal.executed_source_identities()
    provenance = seal.execution_provenance_contract()

    assert seal._implementation._PRODUCTION_STRATEGY_SOURCE_IMAGE.path.name == (
        "mtvclc.py"
    )
    assert policy.MTVCLC_STRATEGY_ID == (
        "ig_mt4_tick_volume_close_location_continuation"
    )
    assert policy.MTVCLC_STRATEGY_VERSION == "mtvclc.v1"
    assert list(policy.MTVCLC_V1_SYMBOLS) == list(seal.IG_MT4_SCALP_SYMBOLS)
    assert "production_strategy_policy_source" in identities
    assert identities["production_strategy_policy_source"]["filename"] == (
        "mtvclc.py"
    )
    assert identities["sealer_v3_template_source"]["filename"].endswith(
        "_v3.py"
    )
    assert provenance["legacy_production_strategy_module_loaded"] is False
    assert provenance["active_runtime_policy_exact_source_executed"] is True
    assert provenance["literal_transform_count"] == 5
    assert "handoff_v3_template_source" in seal._source_paths()
    assert "evaluator_v3_template_source" in seal._source_paths()
    assert "release_v3_template_source" in seal._source_paths()
    assert seal._source_paths()["evaluator_source"].name.endswith("_v4.py")
    assert seal._source_paths()["release_source"].name.endswith("_v4.py")


def test_v4_synthetic_declaration_preserves_fixed_prospective_contract(
    tmp_path: Path,
) -> None:
    payload, _inputs_value = _payload(tmp_path)
    binding = payload["runtime_policy_binding"]
    runtime = payload["source_identities"]["production_runtime_context"]
    window = payload["prospective_window"]
    t0 = seal.base._parse_utc_second(window["t0_utc_inclusive"], label="t0")
    end = seal.base._parse_utc_second(window["end_utc_exclusive"], label="end")

    assert seal.validate_preregistration(payload) is True
    assert payload["preregistration_tool_revision"] == seal.V4_TOOL_REVISION
    assert payload["collector_wire_profile"] == "gap_v3_source_pinned"
    assert payload["scope"]["ordered_symbols"] == list(
        seal.IG_MT4_SCALP_SYMBOLS
    )
    assert len(payload["scope"]["cell_order"]) == 44
    assert payload["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    assert end - t0 == timedelta(days=180)
    assert window["interim_signal_or_outcome_evaluation_forbidden"] is True
    assert window["early_success_forbidden"] is True
    assert payload["execution_contract"]["entry_type"] == "immediate_market"
    assert payload["execution_contract"]["pending_orders_forbidden"] is True
    assert binding["ordered_symbols"] == list(seal.IG_MT4_SCALP_SYMBOLS)
    assert binding["ordered_cell_count"] == 44
    assert binding["evaluator_entrypoint"] == "evaluate_mtvclc"
    assert binding["buy_price_basis"] == "authenticated_ask"
    assert binding["sell_price_basis"] == "authenticated_bid"
    assert binding["runtime_or_broker_authority_granted"] is False
    assert runtime["active_strategy_family_context"] == binding["strategy_id"]
    assert runtime["active_strategy_version_context"] == binding[
        "strategy_version"
    ]
    assert runtime["active_policy_config_sha256_context"] == binding[
        "config_sha256"
    ]
    assert not any(payload["authority"].values())
    assert "scalp_dislocation" not in json.dumps(payload, sort_keys=True)
    assert all(
        not (
            isinstance(value, dict)
            and value.get("filename") == "scalp_dislocation.py"
        )
        for value in payload["source_identities"].values()
    )


def test_v4_policy_or_source_tampering_refuses_after_rehash(tmp_path: Path) -> None:
    payload, _inputs_value = _payload(tmp_path)

    forged_policy = json.loads(json.dumps(payload))
    forged_policy["runtime_policy_binding"]["strategy_version"] = "forged"
    _rehash(forged_policy)
    assert seal.validate_preregistration(forged_policy) is False

    forged_source = json.loads(json.dumps(payload))
    forged_source["source_identities"]["production_strategy_policy_source"][
        "filename"
    ] = "scalp_dislocation.py"
    _rehash(forged_source)
    assert seal.validate_preregistration(forged_source) is False

    forged_authority = json.loads(json.dumps(payload))
    forged_authority["authority"]["runtime_authorized"] = True
    _rehash(forged_authority)
    assert seal.validate_preregistration(forged_authority) is False


def test_v4_handoff_revalidates_runtime_bound_declaration_only(
    tmp_path: Path,
) -> None:
    payload, _inputs_value = _payload(tmp_path)
    claimed = payload["preregistration_body_sha256"]
    artifact = tmp_path / f"mtvclc_gap_v3_preregistration_{claimed}.json"
    artifact.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    binding = handoff.load_preregistration(
        artifact,
        profile=handoff.PROFILE_GAP_V4,
    )

    assert binding.preregistration_body_sha256 == claimed
    assert binding.profile == handoff.PROFILE_GAP_V4
    assert handoff.sealer.validate_preregistration(payload) is True
    assert handoff.derivation_identity()["outcome_evaluation_performed"] is False
    assert handoff.derivation_identity()["authority_granted"] is False
    assert "handoff_v3_template_source" in handoff.executed_source_identities()


def test_v4_post_window_chain_is_public_bound_but_not_executed() -> None:
    evaluator_identity = evaluator.derivation_identity()
    release_identity = release.derivation_identity()

    handoff._assert_executed_sources_unchanged()
    evaluator._assert_executed_sources_unchanged()
    release._assert_executed_sources_unchanged()

    assert evaluator.handoff.PROFILE_GAP_V3 == handoff.PROFILE_GAP_V4
    assert evaluator_identity["v4_handoff_filename"] == HANDOFF_PATH.name
    assert evaluator_identity["early_evaluation_allowed"] is False
    assert evaluator_identity["authority_granted"] is False
    assert release.handoff.PROFILE_GAP_V3 == handoff.PROFILE_GAP_V4
    assert release_identity["v4_handoff_filename"] == HANDOFF_PATH.name
    assert release_identity["v4_evaluator_filename"] == EVALUATOR_PATH.name
    assert release_identity["private_key_access_on_import"] is False
    assert release_identity["authority_granted"] is False


def test_v4_publication_rechecks_every_fee_source_document_before_publish(
    tmp_path: Path,
) -> None:
    payload, inputs = _payload(tmp_path)
    output = tmp_path / "sealed-output"
    output.mkdir()
    sources, snapshots = _publication_snapshots(inputs)
    sealed_documents = {
        row["role"]: row
        for row in payload["source_identities"]["fee_attestation"][
            "source_documents"
        ]
    }
    for role in seal.base.SOURCE_DOCUMENT_URLS:
        name = seal._implementation._fee_source_document_snapshot_key(role)
        assert sealed_documents[role]["sha256"] == snapshots[name].sha256
        assert sealed_documents[role]["size_bytes"] == snapshots[name].size_bytes

    role = next(iter(seal.base.SOURCE_DOCUMENT_URLS))
    name = seal._implementation._fee_source_document_snapshot_key(role)
    source_document = snapshots[name].path
    source_document.write_bytes(b"fee source drift before publication")

    with pytest.raises(
        seal.GapV4PreregistrationRefusal,
        match=f"sealed_input_changed:{name}",
    ):
        seal.atomic_publish(
            output_root=output,
            payload=payload,
            input_paths=inputs,
            source_snapshots=sources,
            input_snapshots=snapshots,
            clock=lambda: seal._implementation._parse_t0_epoch(payload) - 700.0,
        )
    assert list(output.iterdir()) == []


def test_v4_publication_removes_artifact_when_fee_source_changes_after_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload, inputs = _payload(tmp_path)
    output = tmp_path / "sealed-output"
    output.mkdir()
    sources, snapshots = _publication_snapshots(inputs)
    role = next(iter(seal.base.SOURCE_DOCUMENT_URLS))
    name = seal._implementation._fee_source_document_snapshot_key(role)
    source_document = snapshots[name].path
    original_recheck = seal._implementation._recheck_all
    call_count = 0

    def drift_on_post_publish_recheck(source_rows, input_rows):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            source_document.write_bytes(b"fee source drift after publication")
        return original_recheck(source_rows, input_rows)

    monkeypatch.setattr(
        seal._implementation,
        "_recheck_all",
        drift_on_post_publish_recheck,
    )
    with pytest.raises(
        seal.GapV4PreregistrationRefusal,
        match=f"sealed_input_changed:{name}",
    ):
        seal.atomic_publish(
            output_root=output,
            payload=payload,
            input_paths=inputs,
            source_snapshots=sources,
            input_snapshots=snapshots,
            clock=lambda: seal._implementation._parse_t0_epoch(payload) - 700.0,
        )

    assert call_count == 2
    assert list(output.iterdir()) == []
