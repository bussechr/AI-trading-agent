from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient.py"
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


seal = _load("seal_mt4_tick_volume_preregistration_resilient", TOOL_PATH)
base_test = _load("base_sealer_test_helpers", BASE_TEST_PATH)


def _payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    capture, npz, fee = base_test._inputs(tmp_path)
    collector = tmp_path / "capture_ig_mt4_m1_activity_resilient.py"
    collector.write_text("# frozen restart-resilient collector\n", encoding="utf-8")
    monkeypatch.setattr(seal, "COLLECTOR_PATH", collector)
    return seal.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=base_test.SEALED_AT,
    )


def test_replacement_counts_failed_attempt_and_binds_capture_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(tmp_path, monkeypatch)

    assert seal.validate_preregistration(payload) is True
    assert payload["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_742,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_786,
    }
    assert payload["abandoned_preregistrations"][-2:] == [
        seal.FIRST_REPLACED_ATTEMPT,
        seal.SECOND_REPLACED_ATTEMPT,
    ]
    assert payload["replacement_lineage"] == seal.REPLACEMENT_LINEAGE
    assert payload["capture_integrity_contract"] == (
        seal.capture_integrity_contract(
            payload["source_identities"]["collector_source"]["sha256"]
        )
    )
    assert (
        payload["capture_integrity_contract"][
            "overlap_never_overwrites_or_duplicates_a_bar"
        ]
        is True
    )
    assert (
        payload["capture_integrity_contract"]["market_source_id_rollover_refuses"]
        is True
    )
    assert (
        payload["replacement_lineage"][
            "old_capture_used_for_signal_outcome_or_performance_selection"
        ]
        is False
    )
    assert (
        payload["strategy"]["attempt_manifest"][
            "cumulative_attempted_cells_lower_bound"
        ]
        == 4_786
    )
    assert payload["fixed_success_gates"]["cell_win_probability_interval"] == (
        "one_sided_wilson_family_adjusted_over_4786_attempted_cells"
    )
    assert (
        payload["source_identities"]["collector_support_source"]["sha256"]
        == seal.COLLECTOR_SUPPORT_SHA256
    )
    assert payload["source_identities"]["screen_support_source"]["sha256"] == (
        seal.SCREEN_SUPPORT_SHA256
    )
    assert not any(payload["authority"].values())


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        (
            "capture_integrity_contract",
            "revised_later_overlap_is_ignored",
            False,
        ),
        ("capture_integrity_contract", "tick_interval_seconds", 3.0),
        ("replacement_lineage", "old_window_restart_or_extension", True),
    ],
)
def test_replacement_validator_rejects_policy_or_lineage_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str,
    field: str,
    value: object,
) -> None:
    payload = _payload(tmp_path, monkeypatch)
    payload[section][field] = value
    body = {
        key: item
        for key, item in payload.items()
        if key != "preregistration_body_sha256"
    }
    payload["preregistration_body_sha256"] = seal.base.canonical_sha256(body)

    assert seal.validate_preregistration(payload) is False


def test_replacement_atomic_publish_is_authority_free_and_no_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inputs_root = tmp_path / "inputs"
    inputs_root.mkdir()
    capture, npz, fee = base_test._inputs(inputs_root)
    collector = tmp_path / "capture_ig_mt4_m1_activity_resilient.py"
    collector.write_text("# frozen restart-resilient collector\n", encoding="utf-8")
    monkeypatch.setattr(seal, "COLLECTOR_PATH", collector)
    payload = seal.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=base_test.SEALED_AT,
    )
    output_root = tmp_path / "sealed"
    output_root.mkdir()

    published = seal.atomic_publish(
        output_root=output_root,
        payload=payload,
        input_paths=(capture, npz, fee),
    )

    loaded = json.loads(published.read_text(encoding="utf-8"))
    assert loaded == payload
    assert not any(loaded["authority"].values())
    with pytest.raises(
        seal.ReplacementPreregistrationRefusal, match="output_already_exists"
    ):
        seal.atomic_publish(
            output_root=output_root,
            payload=payload,
            input_paths=(capture, npz, fee),
        )


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("strategy", "config_id", "forged"),
        ("fixed_success_gates", "minimum_total_trades", 0),
        ("scope", "cell_order", [{} for _index in range(44)]),
        (None, "cost_policy", {}),
    ],
)
def test_deep_validator_rejects_rehashed_frozen_contract_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    section: str | None,
    field: str,
    value: object,
) -> None:
    payload = _payload(tmp_path, monkeypatch)
    if section is None:
        payload[field] = value
    else:
        payload[section][field] = value
    body = {
        key: item
        for key, item in payload.items()
        if key != "preregistration_body_sha256"
    }
    payload["preregistration_body_sha256"] = seal.base.canonical_sha256(body)

    assert seal.validate_preregistration(payload) is False
