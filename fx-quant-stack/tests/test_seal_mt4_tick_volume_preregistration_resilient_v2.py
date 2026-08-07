from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v2.py"
COLLECTOR_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v2.py"
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


seal = _load("seal_mt4_tick_volume_preregistration_resilient_v2", TOOL_PATH)
base_test = _load("gap_v3_base_sealer_test_helpers", BASE_TEST_PATH)
collector = _load("gap_v3_collector_for_sealer_test", COLLECTOR_PATH)


def _payload(tmp_path: Path) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    capture, npz, fee = base_test._inputs(tmp_path)
    return seal.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=base_test.SEALED_AT,
    )


def _rehash(payload: dict) -> None:
    body = {
        key: value
        for key, value in payload.items()
        if key != "preregistration_body_sha256"
    }
    payload["preregistration_body_sha256"] = seal.base.canonical_sha256(body)


def test_new_preregistration_counts_both_failed_attempts_and_binds_gap_contract(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)

    assert seal.validate_preregistration(payload) is True
    assert payload["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    abandoned = payload["abandoned_preregistrations"]
    assert abandoned[-3:] == [
        seal.D80_FAILED_ATTEMPT,
        seal.SEVEN_B_FAILED_ATTEMPT,
        seal.CROSSED_T0_REFUSED_ATTEMPT,
    ]
    assert abandoned[-3]["attempted_cells_increment"] == 44
    assert abandoned[-2]["attempted_cells_increment"] == 44
    assert abandoned[-1]["attempted_cells_increment"] == 0
    assert payload["replacement_lineage"] == seal.REPLACEMENT_LINEAGE
    assert (
        payload["strategy"]["attempt_manifest"][
            "cumulative_attempted_cells_lower_bound"
        ]
        == 4_830
    )
    assert payload["fixed_success_gates"]["cell_win_probability_interval"] == (
        "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
    )
    contract = payload["capture_integrity_contract"]
    assert contract == seal.capture_integrity_contract(
        payload["source_identities"]["collector_source"]["sha256"]
    )
    assert contract["late_unseen_epoch_is_never_backfilled"] is True
    assert contract["late_unseen_epoch_is_never_baseline_eligible"] is True
    assert contract["late_gap_event_is_fsynced_hash_chained_and_immutable"] is True
    assert contract["maximum_start_edge_lag_seconds"] == 30.0
    assert payload["source_identities"]["sealer_support_source"]["sha256"] == (
        seal.SEALER_SUPPORT_SHA256
    )
    assert not any(payload["authority"].values())


def test_new_seal_is_directly_accepted_by_exact_collector_handshake(
    tmp_path: Path,
) -> None:
    payload = _payload(tmp_path)
    preregistration = tmp_path / "candidate.json"
    preregistration.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    binding = collector.load_preregistration(preregistration)

    assert binding.preregistration_body_sha256 == payload["preregistration_body_sha256"]
    assert payload["capture_integrity_contract"] == (
        collector.expected_capture_integrity_contract()
    )


@pytest.mark.parametrize(
    ("mutation"),
    [
        "remove_d80",
        "remove_07b",
        "reorder_failures",
        "under_count_family",
        "permit_late_baseline",
        "weaken_start_edge",
    ],
)
def test_rehashed_lineage_or_integrity_forgery_is_rejected(
    tmp_path: Path,
    mutation: str,
) -> None:
    payload = _payload(tmp_path)
    if mutation == "remove_d80":
        payload["abandoned_preregistrations"].pop(-3)
    elif mutation == "remove_07b":
        payload["abandoned_preregistrations"].pop(-2)
    elif mutation == "reorder_failures":
        payload["abandoned_preregistrations"][-3:-1] = reversed(
            payload["abandoned_preregistrations"][-3:-1]
        )
    elif mutation == "under_count_family":
        payload["attempt_accounting"] = {
            "prior_attempted_cells_lower_bound": 4_742,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_786,
        }
    elif mutation == "permit_late_baseline":
        payload["capture_integrity_contract"][
            "late_unseen_epoch_is_never_baseline_eligible"
        ] = False
    elif mutation == "weaken_start_edge":
        payload["capture_integrity_contract"]["maximum_start_edge_lag_seconds"] = 31.0
    else:  # pragma: no cover
        raise AssertionError(mutation)
    _rehash(payload)

    assert seal.validate_preregistration(payload) is False


def test_deep_validator_rejects_rehashed_strategy_and_cost_drift(
    tmp_path: Path,
) -> None:
    strategy_payload = _payload(tmp_path / "strategy")
    strategy_payload["strategy"]["config_id"] = "forged"
    _rehash(strategy_payload)
    assert seal.validate_preregistration(strategy_payload) is False

    cost_payload = _payload(tmp_path / "cost")
    symbol = next(iter(cost_payload["cost_policy"]["symbols"]))
    cost_payload["cost_policy"]["symbols"][symbol][
        "fixed_adverse_execution_debit_bps"
    ] = 0.0
    _rehash(cost_payload)
    assert seal.validate_preregistration(cost_payload) is False


def test_screen_family_math_is_the_new_4830_cell_allocation() -> None:
    manifest = seal.replacement_screen.attempt_manifest()

    assert manifest["prior_attempted_cells_lower_bound"] == 4_786
    assert manifest["current_attempted_cells"] == 44
    assert manifest["cumulative_attempted_cells_lower_bound"] == 4_830
    assert manifest["win_probability_familywise_attempted_cells"] == 4_830
    assert manifest["win_probability_alpha_allocation"] == ("one_sided_0.05_over_4830")
    assert seal.replacement_screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD == (
        4.648050309953223
    )


def test_validator_is_pure_after_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload(tmp_path)

    def forbidden_read(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("validator attempted source or capture I/O")

    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    monkeypatch.setattr(Path, "read_text", forbidden_read)

    assert seal.validate_preregistration(copy.deepcopy(payload)) is True
