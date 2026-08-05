from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys

import pytest

from fxstack.scalp import mtvclc_auxiliary_multiplicity as universe


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = (
    REPO_ROOT
    / "tools"
    / "seal_mtvclc_auxiliary_multiplicity_preregistration.py"
)
SPEC = importlib.util.spec_from_file_location(
    "seal_mtvclc_auxiliary_multiplicity_preregistration", TOOL_PATH
)
assert SPEC is not None and SPEC.loader is not None
seal = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = seal
SPEC.loader.exec_module(seal)

GENERATED_AT = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
EXPECTED_MANIFEST_SHA256 = (
    "efe9dce389f048718e238fdd7ce6a13ab1c222803815371b0fd4837d97bffa6c"
)


def _rehash_template(payload: dict) -> dict:
    body = dict(payload)
    body.pop("template_body_sha256", None)
    body["template_body_sha256"] = seal.canonical_sha256(body)
    return body


def test_candidate_universe_is_exact_deterministic_and_nonhistorical() -> None:
    manifest = universe.trial_manifest_payload()

    assert universe.validate_trial_manifest(manifest) is True
    assert universe.canonical_sha256(manifest) == EXPECTED_MANIFEST_SHA256
    assert manifest["template_only"] is True
    assert manifest["publication_eligible"] is False
    assert manifest["replacement_primary_preregistration_required"] is True
    assert manifest["d80e_auxiliary_binding_eligible"] is False
    assert manifest["trial_count"] == 4_698
    assert manifest["negative_control_count"] == 4_697
    assert manifest["primary_column_index"] == 2_110
    assert manifest["generator"]["controls_are_historical_reconstructions"] is False

    rows = manifest["trials"]
    assert len({row["trial_id"] for row in rows}) == 4_698
    assert len({row["mapping_sha256"] for row in rows}) == 4_698
    assert len({row["column_order_sha256"] for row in rows}) == 4_698
    assert [row["column_index"] for row in rows] == list(range(4_698))


def test_every_control_target_mapping_excludes_primary_identity() -> None:
    primary = universe.mapping_for_ordinal(0)
    assert all(
        row.target_symbol == row.source_symbol
        and row.signal_lag_minutes == 0
        and row.side_transform == "preserve"
        for row in primary
    )

    for ordinal in range(1, universe.TRIAL_COUNT):
        assert all(
            not (
                row.target_symbol == row.source_symbol
                and row.signal_lag_minutes == 0
                and row.side_transform == "preserve"
            )
            for row in universe.mapping_for_ordinal(ordinal)
        )


def test_review_template_binds_design_but_grants_no_authority() -> None:
    payload = seal.build_template_payload(generated_at=GENERATED_AT)

    assert seal.validate_template_payload(payload) is True
    assert seal.validate_preregistration(payload) is False
    assert payload["template_only"] is True
    assert payload["publication_eligible"] is False
    assert payload["replacement_primary_preregistration_required"] is True
    assert payload["d80e_auxiliary_binding_eligible"] is False
    assert payload["authority"] == seal.FIXED_AUTHORITY_FLAGS
    assert payload["authority"] and not any(payload["authority"].values())

    withheld = payload["withheld_d80e_proposal"]
    assert withheld["refusal_code"] == seal.WITHHELD_REFUSAL_CODE
    assert withheld["primary_preregistration_body_sha256"] == (
        universe.PRIMARY_PREREGISTRATION_BODY_SHA256
    )
    assert withheld["historical_policy_lineage_reconstructed"] is False
    assert withheld["historical_return_columns_reconstructed"] is False
    assert withheld["may_not_be_published_or_evaluated"] is True

    replacement = payload["replacement_seal_preconditions"]
    assert replacement["new_primary_preregistration_required"] is True
    assert replacement["new_primary_t0_must_be_future_of_new_seal"] is True
    assert replacement["trial_universe_seed_and_all_hashes_must_be_regenerated"] is True
    assert replacement["no_d80e_capture_rows_or_outcomes_may_seed_the_replacement"] is True


def test_review_template_freezes_exact_candidate_math_and_market_semantics() -> None:
    payload = seal.build_template_payload(generated_at=GENERATED_AT)
    design = payload["candidate_experiment_design"]
    window = design["proposed_window"]

    assert window["start_utc_inclusive"] == "2026-09-02T13:30:00Z"
    assert window["end_utc_exclusive"] == "2027-01-30T13:30:00Z"
    assert window["consecutive_24h_periods"] == 150
    assert len(window["alignment_periods"]) == 150
    assert window["alignment_periods"][0]["period_index"] == 0
    assert window["alignment_periods"][-1]["period_index"] == 149

    semantics = design["policy_semantics"]
    assert semantics["entry_type"] == "immediate_market"
    assert semantics["pending_trade_instructions_forbidden"] is True
    returns = design["return_cost_and_missing_data_contract"]
    assert returns["missing_entry_after_reservation"].startswith(
        "adverse_negative_8x_target_cost"
    )
    assert returns["quote_gap_over_5_seconds_after_entry"].startswith(
        "adverse_negative_8x_target_cost"
    )
    assert returns["rows_may_not_be_dropped_reordered_or_imputed"] is True

    statistics = design["statistical_contract"]
    assert statistics["matrix_shape"] == [150, 4_698]
    assert statistics["cscv_pbo"]["exact_balanced_combinations"] == 252
    assert statistics["cscv_pbo"]["maximum_pbo"] == 0.40
    assert statistics["deflated_sharpe"]["n_trials"] == 4_698
    assert statistics["deflated_sharpe"]["minimum_dsr"] == 0.95
    assert statistics["primary_selection_gate"][
        "primary_must_be_unique_strict_full_window_maximum"
    ] is True


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("publication_eligible",), True),
        (("d80e_auxiliary_binding_eligible",), True),
        (("withheld_d80e_proposal", "may_not_be_published_or_evaluated"), False),
        (
            (
                "replacement_seal_preconditions",
                "new_primary_preregistration_required",
            ),
            False,
        ),
        (("authority", "trade_execution_authorized"), True),
    ],
)
def test_semantically_tampered_rehashed_template_is_invalid(
    path: tuple[str, ...], value: object
) -> None:
    payload = deepcopy(seal.build_template_payload(generated_at=GENERATED_AT))
    cursor = payload
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value

    assert seal.validate_template_payload(_rehash_template(payload)) is False


def test_build_and_publish_refuse_before_touching_supplied_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_path_access(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"unexpected path access: {args!r} {kwargs!r}")

    monkeypatch.setattr(seal, "_require_regular_file", unexpected_path_access)
    missing_primary = tmp_path / "never-read-primary.json"
    output = tmp_path / "never-written-output"

    with pytest.raises(
        seal.AuxiliaryPreregistrationRefusal,
        match=seal.WITHHELD_REFUSAL_CODE,
    ):
        seal.build_preregistration(
            primary_preregistration=missing_primary,
            sealed_at=GENERATED_AT,
        )

    with pytest.raises(
        seal.AuxiliaryPreregistrationRefusal,
        match=seal.PUBLISH_REFUSAL_CODE,
    ):
        seal.atomic_publish(
            output_root=output,
            payload={},
            input_paths=(missing_primary,),
        )
    assert not missing_primary.exists()
    assert not output.exists()


def test_cli_is_a_stable_no_output_refusal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    primary = tmp_path / "not-opened.json"
    output = tmp_path / "not-created"

    assert seal.main(
        [
            "--primary-preregistration",
            str(primary),
            "--output-root",
            str(output),
        ]
    ) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert seal.WITHHELD_REFUSAL_CODE in captured.err
    assert not output.exists()
