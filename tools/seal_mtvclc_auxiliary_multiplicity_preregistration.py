"""Review-only template for a replacement MTVCLC multiplicity declaration.

The previously proposed d80e-bound auxiliary experiment is not eligible for a
seal because the underlying v3 capture lost continuity.  This module preserves
the proposed multiplicity-control design for review, but every publication path
refuses.  It reads no capture, signals, outcomes, performance data, network,
database, credential, issuer, runtime, broker, or trade-execution surface.
"""

from __future__ import annotations

# AGENT: ROLE: Review-only replacement MTVCLC multiplicity template.
# AGENT HANDSHAKE: d80e identity context + pure candidate universe -> no artifact.
# AGENT ISOLATION: no capture, outcome, issuer, runtime, broker, or publish access.
# AGENT: SIDE EFFECTS: none; every publication path refuses before filesystem use.

import argparse
import ast
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.scalp import mtvclc_auxiliary_multiplicity as universe  # noqa: E402


TEMPLATE_SCHEMA = (
    "fxstack.scalp.mtvclc_auxiliary_multiplicity_replacement_template.v1"
)
TEMPLATE_ONLY = True
PUBLICATION_ELIGIBLE = False
REPLACEMENT_PRIMARY_PREREGISTRATION_REQUIRED = True
D80E_AUXILIARY_BINDING_ELIGIBLE = False
WITHHELD_REFUSAL_CODE = (
    "d80e_auxiliary_seal_withheld_replacement_primary_required"
)
PUBLISH_REFUSAL_CODE = "template_publication_forbidden"

PRIMARY_PREREGISTRATION_SCHEMA = "fxstack.scalp.mtvclc_preregistration.v1"
PRIMARY_PREREGISTRATION_FILE_SHA256 = (
    "566c85789fdf1f8639a4c010421d27f3b02e6dd2f9a1912968ca8f9b500c51b3"
)
PRIMARY_ATTEMPT_MANIFEST_SHA256 = (
    "e9d171562727591002a187ca8106cfb59fc744c8ac7ad3584671788ae134efeb"
)
PRIMARY_SCREEN_SOURCE_SHA256 = (
    "7e3dd3e829a4429e1316be2925f17d6ec3c9c0e3257e529cefd153e660558059"
)
PRIMARY_COLLECTOR_SOURCE_SHA256 = (
    "87af2452ae3f0f3964c35b2904ad35fda28f3675e1f614e941ee46fd693bda5d"
)
PRIMARY_T0_UTC = datetime(2026, 8, 3, 13, 30, tzinfo=UTC)
PROPOSED_AUXILIARY_START_UTC = datetime(2026, 9, 2, 13, 30, tzinfo=UTC)
PROPOSED_AUXILIARY_END_UTC = datetime(2027, 1, 30, 13, 30, tzinfo=UTC)
PROPOSED_AUXILIARY_PERIOD_DAYS = 150
ALIGNMENT_PERIOD_SECONDS = 86_400
MINIMUM_REPLACEMENT_SEAL_LEAD_SECONDS = 86_400
PBO_SPLITS = 10
PBO_MAX_COMBINATIONS = 512
PBO_EXACT_COMBINATIONS = 252
MAXIMUM_PBO = 0.40
MINIMUM_DSR = 0.95
MINIMUM_NONZERO_PERIODS_PER_TRIAL = 20
MINIMUM_CENTERED_MATRIX_RANK = 100
MAXIMUM_INPUT_BYTES = 64 * 1024 * 1024

TOOL_PATH = Path(__file__).resolve()
UNIVERSE_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "mtvclc_auxiliary_multiplicity.py"
)
OVERFITTING_PATH = FXSTACK_SRC / "fxstack" / "validation" / "overfitting.py"
METRICS_PATH = FXSTACK_SRC / "fxstack" / "validation" / "metrics.py"
RUNTIME_GATE_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "scalp_validation_evidence.py"
)

FIXED_AUTHORITY_FLAGS: dict[str, bool] = {
    "research_process_authorized": False,
    "signal_access_authorized": False,
    "outcome_access_authorized": False,
    "performance_access_authorized": False,
    "success_claim_authorized": False,
    "promotion_authorized": False,
    "activation_authorized": False,
    "registry_write_authorized": False,
    "runtime_authorized": False,
    "issuer_authorized": False,
    "signature_authorized": False,
    "broker_access_authorized": False,
    "trade_execution_authorized": False,
}
_HEX = frozenset("0123456789abcdef")


class AuxiliaryPreregistrationRefusal(RuntimeError):
    """Stable fail-closed refusal raised before any publication operation."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise AuxiliaryPreregistrationRefusal("payload_not_canonical") from exc
    return encoded.encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(character in _HEX for character in text)


def _format_utc_second(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuxiliaryPreregistrationRefusal("timestamp_not_timezone_aware")
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_utc_second(value: Any, *, label: str) -> datetime:
    text = str(value or "")
    if not text.endswith("Z"):
        raise AuxiliaryPreregistrationRefusal(f"{label}_invalid")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as exc:
        raise AuxiliaryPreregistrationRefusal(f"{label}_invalid") from exc
    if parsed.microsecond != 0 or parsed.tzinfo is None:
        raise AuxiliaryPreregistrationRefusal(f"{label}_invalid")
    return parsed.astimezone(UTC)


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    return bool(attributes & 0x400)


def _require_regular_file(path: Path, *, label: str) -> Path:
    if path.is_symlink() or _is_reparse_point(path):
        raise AuxiliaryPreregistrationRefusal(f"{label}_not_regular_file")
    try:
        resolved = path.resolve(strict=True)
        size = resolved.stat().st_size
    except OSError as exc:
        raise AuxiliaryPreregistrationRefusal(
            f"{label}_missing_or_unreadable"
        ) from exc
    if (
        resolved.is_symlink()
        or _is_reparse_point(resolved)
        or not resolved.is_file()
        or size <= 0
        or size > MAXIMUM_INPUT_BYTES
    ):
        raise AuxiliaryPreregistrationRefusal(f"{label}_not_regular_file")
    return resolved


def _file_identity(path: Path, *, label: str) -> dict[str, Any]:
    resolved = _require_regular_file(path, label=label)
    digest = hashlib.sha256()
    size = 0
    try:
        with resolved.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
    except OSError as exc:
        raise AuxiliaryPreregistrationRefusal(f"{label}_unreadable") from exc
    return {
        "filename": resolved.name,
        "size_bytes": size,
        "sha256": digest.hexdigest(),
    }


def _literal_assignment(path: Path, *, name: str) -> Any:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise AuxiliaryPreregistrationRefusal("gate_source_invalid") from exc
    for node in tree.body:
        targets: list[ast.expr] = []
        value_node: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value_node = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value_node = node.value
        if value_node is None:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == name
            for target in targets
        ):
            try:
                return ast.literal_eval(value_node)
            except (ValueError, TypeError) as exc:
                raise AuxiliaryPreregistrationRefusal(
                    f"gate_constant_invalid:{name}"
                ) from exc
    raise AuxiliaryPreregistrationRefusal(f"gate_constant_missing:{name}")


def _alignment_periods() -> list[dict[str, Any]]:
    return [
        {
            "period_index": index,
            "start_utc_inclusive": _format_utc_second(
                PROPOSED_AUXILIARY_START_UTC + timedelta(days=index)
            ),
            "end_utc_exclusive": _format_utc_second(
                PROPOSED_AUXILIARY_START_UTC + timedelta(days=index + 1)
            ),
        }
        for index in range(PROPOSED_AUXILIARY_PERIOD_DAYS)
    ]


def _source_identities() -> dict[str, Any]:
    if _literal_assignment(RUNTIME_GATE_PATH, name="MAX_PBO") != MAXIMUM_PBO:
        raise AuxiliaryPreregistrationRefusal("runtime_pbo_gate_changed")
    if _literal_assignment(RUNTIME_GATE_PATH, name="MIN_DSR") != MINIMUM_DSR:
        raise AuxiliaryPreregistrationRefusal("runtime_dsr_gate_changed")
    return {
        "template_source": _file_identity(
            TOOL_PATH, label="template_source"
        ),
        "trial_universe_source": _file_identity(
            UNIVERSE_PATH, label="trial_universe_source"
        ),
        "pbo_dsr_implementation_source": _file_identity(
            OVERFITTING_PATH, label="pbo_dsr_implementation_source"
        ),
        "statistical_metrics_source": _file_identity(
            METRICS_PATH, label="statistical_metrics_source"
        ),
        "runtime_gate_context_source": _file_identity(
            RUNTIME_GATE_PATH, label="runtime_gate_context_source"
        ),
    }


def build_template_payload(*, generated_at: datetime) -> dict[str, Any]:
    """Build an in-memory review template without opening primary/capture data."""

    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise AuxiliaryPreregistrationRefusal("generated_at_invalid")
    generated = generated_at.astimezone(UTC).replace(microsecond=0)
    trial_manifest = universe.trial_manifest_payload()
    if (
        not universe.validate_trial_manifest(trial_manifest)
        or trial_manifest.get("template_only") is not True
        or trial_manifest.get("publication_eligible") is not False
        or trial_manifest.get("d80e_auxiliary_binding_eligible") is not False
    ):
        raise AuxiliaryPreregistrationRefusal("trial_universe_template_invalid")
    periods = _alignment_periods()
    if periods[-1]["end_utc_exclusive"] != _format_utc_second(
        PROPOSED_AUXILIARY_END_UTC
    ):
        raise AuxiliaryPreregistrationRefusal("proposed_alignment_invalid")

    body: dict[str, Any] = {
        "schema_version": TEMPLATE_SCHEMA,
        "generated_at_utc": _format_utc_second(generated),
        "template_only": TEMPLATE_ONLY,
        "publication_eligible": PUBLICATION_ELIGIBLE,
        "replacement_primary_preregistration_required": (
            REPLACEMENT_PRIMARY_PREREGISTRATION_REQUIRED
        ),
        "d80e_auxiliary_binding_eligible": D80E_AUXILIARY_BINDING_ELIGIBLE,
        "research_only": True,
        "withheld_d80e_proposal": {
            "status": "withheld_fail_closed",
            "refusal_code": WITHHELD_REFUSAL_CODE,
            "reason_category": "source_capture_continuity_not_eligible",
            "primary_preregistration_schema": PRIMARY_PREREGISTRATION_SCHEMA,
            "primary_preregistration_file_sha256": (
                PRIMARY_PREREGISTRATION_FILE_SHA256
            ),
            "primary_preregistration_body_sha256": (
                universe.PRIMARY_PREREGISTRATION_BODY_SHA256
            ),
            "primary_attempt_manifest_sha256": (
                PRIMARY_ATTEMPT_MANIFEST_SHA256
            ),
            "primary_screen_source_sha256": PRIMARY_SCREEN_SOURCE_SHA256,
            "primary_collector_source_sha256": (
                PRIMARY_COLLECTOR_SOURCE_SHA256
            ),
            "primary_t0_utc_inclusive": _format_utc_second(PRIMARY_T0_UTC),
            "requested_auxiliary_start_utc_inclusive": _format_utc_second(
                PROPOSED_AUXILIARY_START_UTC
            ),
            "requested_auxiliary_end_utc_exclusive": _format_utc_second(
                PROPOSED_AUXILIARY_END_UTC
            ),
            "historical_policy_lineage_reconstructed": False,
            "historical_return_columns_reconstructed": False,
            "historical_attempts_relabelled_as_controls": False,
            "may_not_be_published_or_evaluated": True,
        },
        "replacement_seal_preconditions": {
            "new_primary_preregistration_required": True,
            "new_primary_t0_must_be_future_of_new_seal": True,
            "new_primary_capture_must_begin_from_a_clean_bound_state": True,
            "capture_continuity_must_be_eligible_before_auxiliary_design": True,
            "replacement_auxiliary_window_must_be_future_of_new_seal": True,
            "minimum_seal_lead_seconds": (
                MINIMUM_REPLACEMENT_SEAL_LEAD_SECONDS
            ),
            "replacement_window_dates_must_be_explicitly_reselected": True,
            "trial_universe_seed_and_all_hashes_must_be_regenerated": True,
            "screen_collector_scope_cost_and_fee_hashes_must_be_rebound": True,
            "no_d80e_capture_rows_or_outcomes_may_seed_the_replacement": True,
            "independent_code_and_statistical_review_required": True,
            "new_dedicated_sealer_change_required": True,
        },
        "candidate_experiment_design": {
            "status": "review_only_not_preregistered",
            "proposed_window": {
                "start_utc_inclusive": _format_utc_second(
                    PROPOSED_AUXILIARY_START_UTC
                ),
                "end_utc_exclusive": _format_utc_second(
                    PROPOSED_AUXILIARY_END_UTC
                ),
                "consecutive_24h_periods": PROPOSED_AUXILIARY_PERIOD_DAYS,
                "alignment_period_seconds": ALIGNMENT_PERIOD_SECONDS,
                "alignment_periods": periods,
                "signal_or_outcome_evaluation_before_end_forbidden": True,
                "performance_statistics_before_end_forbidden": True,
                "early_success_extension_or_restart_forbidden": True,
                "dates_are_withheld_d80e_proposal_not_replacement_dates": True,
            },
            "trial_universe": trial_manifest,
            "trial_universe_sha256": canonical_sha256(trial_manifest),
            "policy_semantics": {
                "policy_unit": "complete_22_target_symbol_portfolio",
                "primary_equivalence": (
                    "ordinal_0_maps_each_target_to_itself_with_zero_lag_and_"
                    "preserved_side_and_demonstrates_the_d80e_fixed_policy"
                ),
                "control_signal_source": (
                    "apply_the_exact_primary_closed_bar_ivolume_body_and_"
                    "close_location_predicate_to_the_mapped_source_symbol"
                ),
                "source_signal_cost_floor": (
                    "frozen_recorded_cost_for_the_mapped_source_symbol"
                ),
                "target_expected_entry_epoch": (
                    "source_signal_bar_epoch+60+60*signal_lag_minutes"
                ),
                "side_transform": (
                    "preserve_keeps_source_side;invert_swaps_BUY_SELL"
                ),
                "target_entry": (
                    "first_authenticated_target_bid_ask_transport_snapshot_"
                    "at_or_after_expected_entry_within_5_seconds"
                ),
                "target_spread_admission": (
                    "live_spread_must_not_exceed_frozen_target_symbol_p90_"
                    "spread;known_excess_does_not_reserve"
                ),
                "target_geometry": (
                    "4x_recorded_cost_take_profit_and_8x_recorded_cost_stop_"
                    "using_target_symbol_cost_and_conversion_rows"
                ),
                "target_outcome": (
                    "authenticated_executable_quotes_for_30_minutes;stop_"
                    "before_target;same_5_second_maximum_quote_gap"
                ),
                "rollover_blackout": "target_entry_in_[20:20,22:10)_UTC",
                "daily_reservation": (
                    "first_eligible_reservation_per_trial_target_symbol_UTC_"
                    "day_across_both_sides;maximum_one"
                ),
                "entry_type": "immediate_market",
                "pending_trade_instructions_forbidden": True,
            },
            "return_cost_and_missing_data_contract": {
                "trade_gross_quote_bps": (
                    "BUY=(exit_bid-entry_ask)/entry_ask*1e4;SELL=(entry_bid-"
                    "exit_ask)/entry_bid*1e4_with_target_gains_capped"
                ),
                "trade_net_bps": (
                    "gross_quote_bps-recorded_target_cost_bps-target_"
                    "conversion_rate*abs(gross_quote_bps)"
                ),
                "trade_net_r": "trade_net_bps/(8*recorded_target_cost_bps)",
                "aligned_period_return": (
                    "sum_trade_net_r_for_entries_in_period_across_22_targets/22"
                ),
                "untraded_target_or_period_contribution": 0.0,
                "missing_entry_after_reservation": (
                    "adverse_negative_8x_target_cost_gross_before_cost_and_"
                    "conversion_debits"
                ),
                "quote_gap_over_5_seconds_after_entry": (
                    "adverse_negative_8x_target_cost_gross_before_cost_and_"
                    "conversion_debits"
                ),
                "incomplete_30_minute_horizon": (
                    "adverse_negative_8x_target_cost_gross_before_cost_and_"
                    "conversion_debits"
                ),
                "missing_or_gapped_240_bar_source_baseline": "no_signal",
                "nonfinite_return_or_unknown_cost": "refuse_complete_evaluation",
                "source_error": "refuse_complete_evaluation",
                "rows_may_not_be_dropped_reordered_or_imputed": True,
                "trials_may_not_be_dropped_deduplicated_or_reweighted": True,
            },
            "statistical_contract": {
                "matrix_shape": [
                    PROPOSED_AUXILIARY_PERIOD_DAYS,
                    universe.TRIAL_COUNT,
                ],
                "matrix_rows": "exact_alignment_periods_in_declared_order",
                "matrix_columns": "trial_universe.trials_column_index_order",
                "all_values_finite_required": True,
                "all_trial_return_columns_unique_required": True,
                "minimum_nonzero_periods_per_trial": (
                    MINIMUM_NONZERO_PERIODS_PER_TRIAL
                ),
                "minimum_centered_matrix_numerical_rank": (
                    MINIMUM_CENTERED_MATRIX_RANK
                ),
                "rank_method": (
                    "numpy.linalg.matrix_rank_on_column_centered_float64_"
                    "matrix_with_default_SVD_tolerance"
                ),
                "primary_selection_gate": {
                    "selected_trial_id": universe.PRIMARY_TRIAL_ID,
                    "metric": (
                        "nonannualized_sample_mean/sample_sd_daily_sharpe"
                    ),
                    "primary_must_be_unique_strict_full_window_maximum": True,
                    "ties_or_nonpositive_primary_sharpe_refuse": True,
                },
                "cscv_pbo": {
                    "method": "contiguous_balanced_10_block_CSCV",
                    "implementation_function": (
                        "fxstack.validation.overfitting."
                        "probability_of_backtest_overfitting"
                    ),
                    "n_splits": PBO_SPLITS,
                    "max_combinations": PBO_MAX_COMBINATIONS,
                    "exact_balanced_combinations": PBO_EXACT_COMBINATIONS,
                    "every_split_must_have_unique_IS_and_selected_OOS": True,
                    "any_tie_refuses_before_PBO_computation": True,
                    "maximum_pbo": MAXIMUM_PBO,
                },
                "deflated_sharpe": {
                    "implementation_function": (
                        "fxstack.validation.overfitting.deflated_sharpe_ratio"
                    ),
                    "selected_trial_id": universe.PRIMARY_TRIAL_ID,
                    "n_trials": universe.TRIAL_COUNT,
                    "sharpe_variance_input": (
                        "max(sample_variance_of_all_trial_sharpes,1/(150-1))"
                    ),
                    "selected_skew_and_nonexcess_kurtosis": True,
                    "minimum_dsr": MINIMUM_DSR,
                },
                "gate_combination": (
                    "primary_unique_winner_AND_matrix_quality_AND_PBO_lte_"
                    "0.40_AND_DSR_gte_0.95"
                ),
                "failure_cannot_drop_controls_or_be_made_optional": True,
            },
            "evidentiary_limits": {
                "historical_pbo_or_dsr_recovered": False,
                "historical_multiplicity_lineage_recovered": False,
                "candidate_is_a_new_strategy_selection_experiment": True,
                "trial_policies_may_be_highly_related": True,
                "trial_count_not_reduced_for_correlation": True,
                "variance_floor_prevents_identical_like_controls_reducing_dsr": True,
                "matrix_degeneracy_or_ties_refuse_instead_of_repair": True,
                "passing_would_not_prove_runtime_parity_or_grant_authority": True,
            },
        },
        "source_identities": _source_identities(),
        "isolation_contract": {
            "template_reads_no_primary_or_capture_data": True,
            "template_computes_no_signals_outcomes_or_performance": True,
            "template_has_no_publish_path": True,
            "production_database_bridge_broker_credentials_registry_and_issuer_"
            "must_not_be_mounted": True,
        },
        "authority": dict(FIXED_AUTHORITY_FLAGS),
    }
    body["template_body_sha256"] = canonical_sha256(body)
    return body


def validate_template_payload(payload: Mapping[str, Any]) -> bool:
    """Validate every review byte; this never makes it publishable."""

    if not isinstance(payload, Mapping):
        return False
    body = dict(payload)
    claimed = str(body.pop("template_body_sha256", "")).lower()
    if not _is_sha256(claimed) or not hmac.compare_digest(
        claimed, canonical_sha256(body)
    ):
        return False
    try:
        generated = _parse_utc_second(
            body.get("generated_at_utc"), label="generated_at"
        )
        expected = build_template_payload(generated_at=generated)
    except AuxiliaryPreregistrationRefusal:
        return False
    return hmac.compare_digest(
        canonical_json_bytes(dict(payload)), canonical_json_bytes(expected)
    )


def build_preregistration(
    *, primary_preregistration: str | Path, sealed_at: datetime
) -> dict[str, Any]:
    """Refuse the invalid d80e-bound seal before reading either argument."""

    del primary_preregistration, sealed_at
    raise AuxiliaryPreregistrationRefusal(WITHHELD_REFUSAL_CODE)


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """No payload from this template is a valid preregistration."""

    del payload
    return False


def atomic_publish(
    *,
    output_root: str | Path,
    payload: Mapping[str, Any],
    input_paths: Sequence[str | Path],
) -> Path:
    """Refuse before resolving, reading, creating, or writing any path."""

    del output_root, payload, input_paths
    raise AuxiliaryPreregistrationRefusal(PUBLISH_REFUSAL_CODE)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary-preregistration", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        build_preregistration(
            primary_preregistration=args.primary_preregistration,
            sealed_at=datetime.now(UTC),
        )
    except AuxiliaryPreregistrationRefusal as exc:
        print(f"auxiliary preregistration refused: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable publication path")


if __name__ == "__main__":
    raise SystemExit(main())
