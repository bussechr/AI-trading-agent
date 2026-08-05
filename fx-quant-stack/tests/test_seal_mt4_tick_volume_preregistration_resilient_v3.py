from __future__ import annotations

import importlib.util
import json
import marshal
import os
import struct
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v3.py"
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


seal = _load("seal_mt4_tick_volume_preregistration_resilient_v3_test", TOOL_PATH)
base_test = _load("gap_v3_corrected_base_sealer_helpers", BASE_TEST_PATH)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    sealed_inputs = tmp_path / "sealed-inputs"
    sealed_inputs.mkdir()
    capture, npz, fee = base_test._inputs(sealed_inputs)
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(seal.BRIDGE_EA_REPOSITORY_SOURCE_PATH.read_bytes())
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"test-only compiled BridgeEA identity")
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


def _rehash(payload: dict) -> None:
    body = dict(payload)
    body.pop("preregistration_body_sha256", None)
    payload["preregistration_body_sha256"] = seal.base.canonical_sha256(body)


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


def _forged_timestamp_pyc(tmp_path: Path) -> Path:
    source = tmp_path / "forged_dependency.py"
    source.write_bytes(b'MARKER = "descriptor-source"\n')
    fixed_mtime = 1_700_000_000
    os.utime(source, (fixed_mtime, fixed_mtime))
    source_stat = source.stat()
    cached = Path(importlib.util.cache_from_source(str(source)))
    cached.parent.mkdir()
    malicious = compile(
        'MARKER = "timestamp-size-valid-pyc"\n',
        str(source),
        "exec",
    )
    cached.write_bytes(
        importlib.util.MAGIC_NUMBER
        + struct.pack(
            "<III",
            0,
            int(source_stat.st_mtime) & 0xFFFFFFFF,
            source_stat.st_size & 0xFFFFFFFF,
        )
        + marshal.dumps(malicious)
    )
    return source


def test_exact_source_execution_ignores_timestamp_size_valid_forged_pyc(
    tmp_path: Path,
) -> None:
    source = _forged_timestamp_pyc(tmp_path)
    conventional = _load("gap_v3_forged_pyc_control", source)
    assert conventional.MARKER == "timestamp-size-valid-pyc"

    image = seal._read_executed_source(source, reason="test_source_invalid")
    sentinel_name = "_gap_v3_exact_source_pyc_regression"
    sentinel = ModuleType(sentinel_name)
    sys.modules[sentinel_name] = sentinel
    try:
        exact = seal._execute_exact_source(image, module_name=sentinel_name)
        assert exact.MARKER == "descriptor-source"
        assert sys.modules[sentinel_name] is sentinel
    finally:
        sys.modules.pop(sentinel_name, None)


def test_exact_dependency_injection_restores_existing_modules(tmp_path: Path) -> None:
    source = tmp_path / "injected_dependency.py"
    source.write_bytes(b"from provenance.dep import VALUE\nMARKER = VALUE\n")
    image = seal._read_executed_source(source, reason="test_source_invalid")
    package = ModuleType("provenance")
    prior_dependency = ModuleType("provenance.dep")
    prior_dependency.VALUE = "prior"
    package.dep = prior_dependency
    exact_dependency = ModuleType("provenance.dep")
    exact_dependency.VALUE = "exact-snapshot"
    prior_package_slot = sys.modules.get("provenance")
    prior_dependency_slot = sys.modules.get("provenance.dep")
    sys.modules["provenance"] = package
    sys.modules["provenance.dep"] = prior_dependency
    try:
        executed = seal._execute_exact_source(
            image,
            module_name="_gap_v3_exact_injection_regression",
            injected_modules={"provenance.dep": exact_dependency},
        )
        assert executed.MARKER == "exact-snapshot"
        assert sys.modules["provenance"] is package
        assert sys.modules["provenance.dep"] is prior_dependency
        assert package.dep is prior_dependency
        assert "_gap_v3_exact_injection_regression" not in sys.modules
    finally:
        if prior_package_slot is None:
            sys.modules.pop("provenance", None)
        else:
            sys.modules["provenance"] = prior_package_slot
        if prior_dependency_slot is None:
            sys.modules.pop("provenance.dep", None)
        else:
            sys.modules["provenance.dep"] = prior_dependency_slot


def test_execution_provenance_identity_set_is_complete(tmp_path: Path) -> None:
    payload, _inputs_value = _payload(tmp_path)
    identities = payload["source_identities"]
    contract = payload["execution_provenance_contract"]
    required = set(seal.REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS)

    assert set(contract["required_local_executable_source_identities"]) == required
    assert required <= set(seal._source_paths())
    assert required <= set(identities)
    assert set(seal.executed_source_identities()) <= required
    assert all(
        identities[label] == identity
        for label, identity in seal.executed_source_identities().items()
    )
    assert contract["workspace_pyc_execution_allowed"] is False
    assert contract["temporary_sys_modules_bindings_restored"] is True

    for omitted in (
        "legacy_handoff_support_source",
        "legacy_evaluator_support_source",
        "legacy_release_support_source",
        "public_verifier_v1_support_source",
    ):
        forged = json.loads(json.dumps(payload))
        forged["source_identities"].pop(omitted)
        _rehash(forged)
        assert seal.validate_preregistration(forged) is False


def test_corrected_lineage_and_exact_collector_handshake(tmp_path: Path) -> None:
    payload, _inputs_value = _payload(tmp_path)

    assert seal.validate_preregistration(payload) is True
    assert payload["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    assert payload["abandoned_preregistrations"][-1] == (
        seal.FAILED_CROSSED_T0_ATTEMPT
    )
    assert payload["abandoned_preregistrations"] == (
        seal._expected_abandoned_attempts()
    )
    assert payload["abandoned_preregistrations"][-1][
        "attempted_cells_increment"
    ] == 44
    assert payload["strategy"]["attempt_manifest"] == seal.screen.attempt_manifest()
    assert payload["fixed_success_gates"]["cell_win_probability_interval"] == (
        "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
    )
    assert payload["scope"]["ordered_symbols"] == list(seal.IG_MT4_SCALP_SYMBOLS)
    assert "XRPUSD" not in payload["scope"]["ordered_symbols"]
    assert payload["execution_contract"]["entry_type"] == "immediate_market"
    assert payload["execution_contract"]["pending_orders_forbidden"] is True
    assert not any(payload["authority"].values())
    capture_contract = payload["capture_integrity_contract"]
    assert capture_contract["tail_commitment_filename"] == (
        "capture-tail-commitments.v2.jsonl"
    )
    assert capture_contract["tail_commitment_schema_version"] == (
        "fxstack.external_ig_mt4_m1_tail_commitment.v2"
    )
    assert capture_contract["pre_get_cycle_reservation_schema_version"] == (
        "fxstack.external_ig_mt4_m1_cycle_reservation.v1"
    )
    assert capture_contract[
        "reservation_is_fsynced_through_tail_wal_before_first_get"
    ] is True
    assert capture_contract[
        "post_window_finalization_is_integrity_only_and_network_free"
    ] is True
    assert seal.base.canonical_sha256(capture_contract) == (
        seal.PINNED_CAPTURE_INTEGRITY_CONTRACT_SHA256
    )
    assert capture_contract[
        "late_gap_main_chain_deletion_truncation_or_rollback_relative_to_"
        "tail_commitment_refuses"
    ] is True
    assert capture_contract[
        "coordinated_tail_commitment_and_data_rollback_detection_claimed"
    ] is False
    assert capture_contract["windows_directory_fsync_claimed"] is False
    producer = payload["upstream_producer_software"]
    producer_body = dict(producer)
    producer_hash = producer_body.pop("producer_software_body_sha256")
    assert producer["schema_version"] == (
        "fxstack.scalp.mtvclc_upstream_producer_software.v1"
    )
    assert producer["repository_source"] == payload["source_identities"][
        "production_engine_component:MQL4/Experts/BridgeEA.mq4"
    ]
    assert producer["deployed_source"] == payload["source_identities"][
        "bridge_ea_deployed_source"
    ]
    assert producer["deployed_ex4"] == payload["source_identities"][
        "bridge_ea_deployed_ex4"
    ]
    assert producer["repository_source"] == producer["deployed_source"]
    assert producer["collector_cycle_revalidation_claimed"] is (
        seal.COLLECTOR_CYCLE_REVALIDATION_CLAIMED
    )
    assert producer["runtime_or_broker_authority_derived_from_identity"] is False
    assert producer_hash == seal.base.canonical_sha256(producer_body)
    assert all(
        set(producer[label]) == {"filename", "sha256", "size_bytes"}
        for label in ("repository_source", "deployed_source", "deployed_ex4")
    )

    t0 = seal.base._parse_utc_second(
        payload["prospective_window"]["t0_utc_inclusive"], label="t0"
    )
    assert t0.second == 0 and t0.microsecond == 0

    target = tmp_path / (
        "mtvclc_gap_v3_preregistration_"
        f"{payload['preregistration_body_sha256']}.json"
    )
    target.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    collector = seal._execute_collector(
        seal.stable_snapshot(
            seal.COLLECTOR_PATH,
            reason="collector_source_invalid",
        )
    )
    assert payload["abandoned_preregistrations"] == (
        collector.expected_abandoned_preregistrations()
    )
    loaded = collector.load_preregistration(target)
    assert loaded.preregistration_body_sha256 == payload[
        "preregistration_body_sha256"
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "crossed_increment_zero",
        "lineage_prefix_truncated",
        "family_4874",
        "pending_permitted",
        "xrp_added",
        "collector_identity_forged",
        "producer_identity_forged",
        "producer_body_hash_forged",
        "producer_cycle_claim_forged",
        "tail_contract_forged",
        "coordinated_rollback_overclaimed",
    ],
)
def test_rehashed_lineage_scope_or_source_forgery_refuses(
    tmp_path: Path, mutation: str
) -> None:
    payload, _inputs_value = _payload(tmp_path)
    if mutation == "crossed_increment_zero":
        payload["abandoned_preregistrations"][-1]["attempted_cells_increment"] = 0
    elif mutation == "lineage_prefix_truncated":
        payload["abandoned_preregistrations"].pop(0)
    elif mutation == "family_4874":
        payload["attempt_accounting"]["prior_attempted_cells_lower_bound"] = 4_830
        payload["attempt_accounting"]["cumulative_attempted_cells_lower_bound"] = 4_874
    elif mutation == "pending_permitted":
        payload["execution_contract"]["pending_orders_forbidden"] = False
    elif mutation == "xrp_added":
        payload["scope"]["ordered_symbols"].append("XRPUSD")
    elif mutation == "collector_identity_forged":
        payload["source_identities"]["collector_source"]["sha256"] = "f" * 64
    elif mutation == "producer_identity_forged":
        payload["upstream_producer_software"]["deployed_ex4"]["sha256"] = "f" * 64
    elif mutation == "producer_body_hash_forged":
        payload["upstream_producer_software"]["producer_software_body_sha256"] = (
            "f" * 64
        )
    elif mutation == "producer_cycle_claim_forged":
        producer = payload["upstream_producer_software"]
        producer["collector_cycle_revalidation_claimed"] = not (
            seal.COLLECTOR_CYCLE_REVALIDATION_CLAIMED
        )
        body = dict(producer)
        body.pop("producer_software_body_sha256", None)
        producer["producer_software_body_sha256"] = seal.base.canonical_sha256(body)
    elif mutation == "tail_contract_forged":
        payload["capture_integrity_contract"]["tail_commitment_filename"] = (
            "unsealed-tail.jsonl"
        )
    elif mutation == "coordinated_rollback_overclaimed":
        payload["capture_integrity_contract"][
            "coordinated_tail_commitment_and_data_rollback_detection_claimed"
        ] = True
    else:  # pragma: no cover
        raise AssertionError(mutation)
    _rehash(payload)
    assert seal.validate_preregistration(payload) is False


def test_build_refuses_deployed_bridge_ea_source_mismatch(tmp_path: Path) -> None:
    inputs = list(_inputs(tmp_path))
    inputs[3].write_bytes(inputs[3].read_bytes() + b"\n// deployment drift\n")

    with pytest.raises(
        seal.GapV3PreregistrationRefusal,
        match="bridge_ea_deployment_identity_mismatch",
    ):
        seal.build_preregistration(
            cost_capture_json=inputs[0],
            cost_capture_npz=inputs[1],
            fee_attestation=inputs[2],
            bridge_ea_deployed_source=inputs[3],
            bridge_ea_deployed_ex4=inputs[4],
            sealed_at=base_test.SEALED_AT,
        )


def test_build_refuses_repository_file_masquerading_as_deployed_source(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)

    with pytest.raises(
        seal.GapV3PreregistrationRefusal,
        match="bridge_ea_deployment_identity_mismatch",
    ):
        seal.build_preregistration(
            cost_capture_json=inputs[0],
            cost_capture_npz=inputs[1],
            fee_attestation=inputs[2],
            bridge_ea_deployed_source=seal.BRIDGE_EA_REPOSITORY_SOURCE_PATH,
            bridge_ea_deployed_ex4=inputs[4],
            sealed_at=base_test.SEALED_AT,
        )


def test_stable_snapshot_recheck_rejects_byte_change(tmp_path: Path) -> None:
    source = tmp_path / "stable.txt"
    source.write_bytes(b"first")
    snapshot = seal.stable_snapshot(source, reason="source_invalid")
    source.write_bytes(b"later")

    with pytest.raises(
        seal.GapV3PreregistrationRefusal, match="source_changed"
    ):
        seal._assert_same_snapshot(snapshot, reason="source_changed")


def test_atomic_publication_final_clock_crossing_leaves_no_artifact(
    tmp_path: Path,
) -> None:
    payload, inputs = _payload(tmp_path)
    output = tmp_path / "sealed-output"
    output.mkdir()
    sources, snapshots = _publication_snapshots(inputs)
    t0_epoch = seal._parse_t0_epoch(payload)
    clock_values = iter(
        (
            t0_epoch - 700.0,
            t0_epoch - 650.0,
            t0_epoch - 599.0,
        )
    )

    with pytest.raises(
        seal.GapV3PreregistrationRefusal,
        match="publication_lead_time_insufficient",
    ):
        seal.atomic_publish(
            output_root=output,
            payload=payload,
            input_paths=inputs,
            source_snapshots=sources,
            input_snapshots=snapshots,
            clock=lambda: next(clock_values),
        )
    assert list(output.iterdir()) == []


def test_atomic_publication_uses_new_v3_filename_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    payload, inputs = _payload(tmp_path)
    output = tmp_path / "sealed-output"
    output.mkdir()
    sources, snapshots = _publication_snapshots(inputs)
    t0_epoch = seal._parse_t0_epoch(payload)
    target = seal.atomic_publish(
        output_root=output,
        payload=payload,
        input_paths=inputs,
        source_snapshots=sources,
        input_snapshots=snapshots,
        clock=lambda: t0_epoch - 700.0,
    )
    try:
        assert target.name == (
            "mtvclc_gap_v3_preregistration_"
            f"{payload['preregistration_body_sha256']}.json"
        )
        assert json.loads(target.read_text(encoding="utf-8")) == payload
        with pytest.raises(
            seal.GapV3PreregistrationRefusal, match="output_already_exists"
        ):
            seal.atomic_publish(
                output_root=output,
                payload=payload,
                input_paths=inputs,
                source_snapshots=sources,
                input_snapshots=snapshots,
                clock=lambda: t0_epoch - 700.0,
            )
    finally:
        os.chmod(target, 0o600)


def test_atomic_publication_rechecks_bound_producer_snapshots(tmp_path: Path) -> None:
    payload, inputs = _payload(tmp_path)
    output = tmp_path / "sealed-output"
    output.mkdir()
    sources, snapshots = _publication_snapshots(inputs)
    inputs[4].write_bytes(b"drifted compiled BridgeEA identity")

    with pytest.raises(
        seal.GapV3PreregistrationRefusal,
        match="sealed_input_changed:bridge_ea_deployed_ex4",
    ):
        seal.atomic_publish(
            output_root=output,
            payload=payload,
            input_paths=inputs,
            source_snapshots=sources,
            input_snapshots=snapshots,
            clock=lambda: seal._parse_t0_epoch(payload) - 700.0,
        )
    assert list(output.iterdir()) == []


def test_preservation_filename_contract_matches_downstream_publishers() -> None:
    contract = seal.preservation_filename_contract()
    assert contract["preregistration_filename_template"].startswith(
        "mtvclc_gap_v3_preregistration_"
    )
    assert contract["capture_root_name_template"].startswith(
        "mtvclc_prospective_capture_gap_v3_"
    )
    assert contract["handoff_filename_template"].startswith(
        "mtvclc_capture_handoff_v2_"
    )
    assert contract["report_filename_template"] == (
        "mtvclc_post_window_report_v3_{artifact_sha256}.json"
    )
