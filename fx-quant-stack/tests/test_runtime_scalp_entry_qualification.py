from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import math
from pathlib import Path

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_CATALOG,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime import scalp_entry_qualification as qualification_module
from fxstack.runtime.scalp_entry_qualification import (
    P_STAR_ABSOLUTE_TOLERANCE,
    SCALP_DISLOCATION_CONFIG_SHA256,
    qualify_scalp_entry_candidate,
)
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
    ScalpValidationVerification,
)
from fxstack.schemas.entry import EntryProposal
from fxstack.strategy.scalp_dislocation import (
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    SCALP_EXECUTION_DEBIT_BPS,
    DislocationPolicy,
)


NOW = 1_800_000_000.0


def _proposal(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    stop_bps: float = 5.0,
    target_bps: float = 10.0,
    spread_bps: float = 1.0,
    p_star: float | None = None,
    entry_price: float = 1.1000,
    sl_price: float | None = None,
    tp_price: float | None = None,
) -> EntryProposal:
    instrument = IG_MT4_SCALP_CATALOG[symbol]
    selected_p_star = (
        (stop_bps + spread_bps + SCALP_EXECUTION_DEBIT_BPS)
        / (target_bps + stop_bps)
        if p_star is None
        else p_star
    )
    selected_sl = (
        entry_price - 0.0010 if side == "BUY" else entry_price + 0.0010
    ) if sl_price is None else sl_price
    selected_tp = (
        entry_price + 0.0020 if side == "BUY" else entry_price - 0.0020
    ) if tp_price is None else tp_price
    return EntryProposal(
        strategy_id=SCALP_DISLOCATION_STRATEGY_ID,
        strategy_version=SCALP_DISLOCATION_STRATEGY_VERSION,
        config_sha256=SCALP_DISLOCATION_CONFIG_SHA256,
        symbol=symbol,
        instrument_id=instrument.instrument_id,
        venue_id=IG_MT4_VENUE_ID,
        source_id="ig_mt4_bridge_m1",
        source_version="bridge-bars-v1",
        allowed=True,
        reasons=(),
        side=side,  # type: ignore[arg-type]
        minute_epoch=29_999_999,
        ref_mid=entry_price,
        entry_price=entry_price,
        sl_price=selected_sl,
        tp_price=selected_tp,
        atr_bps=6.0,
        stop_bps=stop_bps,
        target_bps=target_bps,
        disp_z=2.5,
        spread_bps=spread_bps,
        p_star=selected_p_star,
        time_stop_bars=20,
        entry_deadline_epoch=int(NOW) + 5,
    )


def _bounds(*, lower_bound: float = 0.70) -> dict[str, dict[str, float]]:
    return {
        symbol: {"BUY": lower_bound, "SELL": lower_bound}
        for symbol in IG_MT4_SCALP_SYMBOLS
    }


def _verification(
    *,
    lower_bound: float = 0.70,
    bounds: dict[str, dict[str, float]] | None = None,
    **updates: object,
) -> ScalpValidationVerification:
    values: dict[str, object] = {
        "valid": True,
        "reason": "",
        "errors": (),
        "authenticated": True,
        "revocation_verified": True,
        "certificate_sha256": "a" * 64,
        "evidence_sha256": "b" * 64,
        "signing_key_id": "c" * 64,
        "generation_id": "scalp-generation-1",
        "strategy_id": SCALP_DISLOCATION_STRATEGY_ID,
        "strategy_version": SCALP_DISLOCATION_STRATEGY_VERSION,
        "engine_sha256": "d" * 64,
        "config_sha256": SCALP_DISLOCATION_CONFIG_SHA256,
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": IG_MT4_SCALP_SYMBOLS,
        "max_entries_per_symbol_utc_day": 1,
        "issued_at_epoch": NOW - 60.0,
        "expires_at_epoch": NOW + 60.0,
        "win_probability_lower_bounds": (
            _bounds(lower_bound=lower_bound) if bounds is None else bounds
        ),
        "win_probability_bounds_reason": "",
    }
    values.update(updates)
    return ScalpValidationVerification(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_valid_candidate_exposes_only_conservative_qualified_metrics(side: str) -> None:
    proposal = _proposal(side=side)

    result = qualify_scalp_entry_candidate(
        proposal,
        _verification(),
        as_of_epoch=NOW,
    )

    assert result.qualified is True
    assert result.reasons == ()
    assert result.proposal is proposal
    assert result.qualified_candidate is not None
    assert result.qualified_candidate.proposal is proposal
    assert result.win_probability_lower_bound == pytest.approx(0.70)
    assert result.conservative_expected_edge_bps == pytest.approx(3.5)
    assert result.reward_risk_ratio == pytest.approx(2.0)
    assert proposal.qualification == "candidate_unqualified"
    assert proposal.execution_qualified is False
    assert proposal.win_probability is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_direct_demo_candidate_cannot_qualify_new_entry(side: str) -> None:
    proposal = _proposal(side=side)
    verification = _verification(
        admission_mode=SCALP_ADMISSION_MODE_DIRECT_DEMO,
        authenticated=False,
        revocation_verified=False,
        signing_key_id="",
        win_probability_lower_bounds={},
    )

    result = qualify_scalp_entry_candidate(
        proposal,
        verification,
        as_of_epoch=NOW,
    )

    assert result.qualified is False
    assert "scalp_qualification_admission_mode_invalid" in result.reasons
    assert "scalp_qualification_validation_unauthenticated" in result.reasons
    assert "scalp_qualification_revocation_unverified" in result.reasons
    assert result.qualified_candidate is None
    assert result.win_probability_lower_bound is None
    assert result.conservative_expected_edge_bps is None


def test_result_and_qualified_wrapper_are_frozen() -> None:
    result = qualify_scalp_entry_candidate(
        _proposal(),
        _verification(),
        as_of_epoch=NOW,
    )
    assert result.qualified_candidate is not None

    with pytest.raises(FrozenInstanceError):
        result.reasons = ("tampered",)  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.qualified_candidate.reward_risk_ratio = 99.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("verification_updates", "expected_reason"),
    [
        ({"valid": False}, "scalp_qualification_validation_invalid"),
        ({"authenticated": False}, "scalp_qualification_validation_unauthenticated"),
        ({"revocation_verified": False}, "scalp_qualification_revocation_unverified"),
        (
            {"strategy_id": "tampered"},
            "scalp_qualification_validation_strategy_id_invalid",
        ),
        (
            {"strategy_version": "tampered.v2"},
            "scalp_qualification_validation_strategy_version_invalid",
        ),
        (
            {"config_sha256": "f" * 64},
            "scalp_qualification_validation_config_invalid",
        ),
        ({"venue_id": "other"}, "scalp_qualification_validation_venue_invalid"),
        (
            {"symbol_scope": IG_MT4_SCALP_SYMBOLS[:-1]},
            "scalp_qualification_validation_scope_invalid",
        ),
        (
            {"symbol_scope": tuple(reversed(IG_MT4_SCALP_SYMBOLS))},
            "scalp_qualification_validation_scope_invalid",
        ),
        (
            {"max_entries_per_symbol_utc_day": 2},
            "scalp_qualification_validation_daily_cap_invalid",
        ),
    ],
)
def test_tampered_verification_never_exposes_probability_or_candidate(
    verification_updates: dict[str, object],
    expected_reason: str,
) -> None:
    result = qualify_scalp_entry_candidate(
        _proposal(),
        _verification(**verification_updates),  # type: ignore[arg-type]
        as_of_epoch=NOW,
    )

    assert result.qualified is False
    assert expected_reason in result.reasons
    assert result.qualified_candidate is None
    assert result.win_probability_lower_bound is None
    assert result.conservative_expected_edge_bps is None
    assert result.reward_risk_ratio is None


@pytest.mark.parametrize(
    ("proposal_update", "expected_reason"),
    [
        ({"strategy_id": "tampered"}, "scalp_qualification_strategy_id_invalid"),
        (
            {"strategy_version": "tampered.v2"},
            "scalp_qualification_strategy_version_invalid",
        ),
        (
            {"config_sha256": "f" * 64},
            "scalp_qualification_strategy_config_invalid",
        ),
        ({"venue_id": "other"}, "scalp_qualification_venue_invalid"),
        ({"instrument_id": "fx:ig_mt4:USDJPY"}, "scalp_qualification_instrument_id_invalid"),
        ({"spread_bps": 0.0}, "scalp_qualification_spread_bps_invalid"),
        ({"entry_price": math.nan}, "scalp_qualification_entry_price_invalid"),
        (
            {"entry_deadline_epoch": int(NOW)},
            "scalp_qualification_entry_deadline_expired",
        ),
    ],
)
def test_tampered_proposal_is_refused_without_mutation(
    proposal_update: dict[str, object],
    expected_reason: str,
) -> None:
    proposal = replace(_proposal(), **proposal_update)  # type: ignore[arg-type]

    result = qualify_scalp_entry_candidate(
        proposal,
        _verification(),
        as_of_epoch=NOW,
    )

    assert expected_reason in result.reasons
    assert result.qualified_candidate is None
    assert proposal.execution_qualified is False
    assert proposal.win_probability is None


def test_missing_or_malformed_probability_cell_fails_the_entire_surface() -> None:
    missing = _bounds()
    missing["BTCUSD"].pop("SELL")
    missing_result = qualify_scalp_entry_candidate(
        _proposal(),
        _verification(bounds=missing),
        as_of_epoch=NOW,
    )
    assert (
        "scalp_qualification_probability_side_scope_invalid:BTCUSD"
        in missing_result.reasons
    )
    assert missing_result.win_probability_lower_bound is None

    malformed = _bounds()
    malformed["NZDJPY"]["SELL"] = math.nan
    malformed_result = qualify_scalp_entry_candidate(
        _proposal(),
        _verification(bounds=malformed),
        as_of_epoch=NOW,
    )
    assert (
        "scalp_qualification_probability_lower_bound_invalid:NZDJPY:SELL"
        in malformed_result.reasons
    )
    assert malformed_result.qualified_candidate is None


def test_probability_equal_to_break_even_is_not_qualified() -> None:
    proposal = _proposal()
    assert proposal.p_star is not None
    result = qualify_scalp_entry_candidate(
        proposal,
        _verification(lower_bound=float(proposal.p_star)),
        as_of_epoch=NOW,
    )

    assert result.reasons == (
        "scalp_qualification_probability_not_above_p_star",
    )
    assert result.win_probability_lower_bound is None
    assert result.qualified_candidate is None


def test_nonpositive_conservative_edge_is_independently_refused() -> None:
    exact_p_star = (5.0 + 1.0 + SCALP_EXECUTION_DEBIT_BPS) / (10.0 + 5.0)
    proposal = _proposal(p_star=exact_p_star - P_STAR_ABSOLUTE_TOLERANCE / 2.0)
    lower_bound = exact_p_star - P_STAR_ABSOLUTE_TOLERANCE / 4.0

    result = qualify_scalp_entry_candidate(
        proposal,
        _verification(lower_bound=lower_bound),
        as_of_epoch=NOW,
    )

    assert result.reasons == ("scalp_qualification_expected_edge_nonpositive",)
    assert result.win_probability_lower_bound is None


@pytest.mark.parametrize(
    "as_of_epoch",
    [NOW + 60.0, NOW + 61.0],
)
def test_expired_verification_is_refused_at_or_after_expiry(
    as_of_epoch: float,
) -> None:
    result = qualify_scalp_entry_candidate(
        _proposal(),
        _verification(),
        as_of_epoch=as_of_epoch,
    )

    assert "scalp_qualification_validation_expired" in result.reasons
    assert result.qualified_candidate is None


@pytest.mark.parametrize(
    "proposal",
    [
        _proposal(side="BUY", sl_price=1.1010, tp_price=1.1020),
        _proposal(side="SELL", sl_price=1.0990, tp_price=1.0980),
    ],
)
def test_directionally_invalid_bracket_is_refused(proposal: EntryProposal) -> None:
    result = qualify_scalp_entry_candidate(
        proposal,
        _verification(),
        as_of_epoch=NOW,
    )

    assert "scalp_qualification_bracket_direction_invalid" in result.reasons
    assert result.qualified_candidate is None


def test_p_star_is_recomputed_with_exact_absolute_tolerance() -> None:
    exact = (5.0 + 1.0 + SCALP_EXECUTION_DEBIT_BPS) / (10.0 + 5.0)
    within = _proposal(p_star=exact + P_STAR_ABSOLUTE_TOLERANCE)
    outside = _proposal(p_star=exact + P_STAR_ABSOLUTE_TOLERANCE * 2.0)

    within_result = qualify_scalp_entry_candidate(
        within,
        _verification(),
        as_of_epoch=NOW,
    )
    outside_result = qualify_scalp_entry_candidate(
        outside,
        _verification(),
        as_of_epoch=NOW,
    )

    assert within_result.qualified is True
    assert "scalp_qualification_p_star_mismatch" in outside_result.reasons


def test_probability_mapping_permutations_are_idempotent() -> None:
    canonical_bounds = _bounds()
    permuted_bounds = {
        symbol: {
            "SELL": canonical_bounds[symbol]["SELL"],
            "BUY": canonical_bounds[symbol]["BUY"],
        }
        for symbol in reversed(IG_MT4_SCALP_SYMBOLS)
    }
    proposal = _proposal()

    canonical = qualify_scalp_entry_candidate(
        proposal,
        _verification(bounds=canonical_bounds),
        as_of_epoch=NOW,
    )
    permuted = qualify_scalp_entry_candidate(
        proposal,
        _verification(bounds=permuted_bounds),
        as_of_epoch=NOW,
    )
    repeated = qualify_scalp_entry_candidate(
        proposal,
        _verification(bounds=permuted_bounds),
        as_of_epoch=NOW,
    )

    assert canonical == permuted == repeated


def test_exact_config_is_the_production_dislocation_default() -> None:
    assert SCALP_DISLOCATION_CONFIG_SHA256 == DislocationPolicy().config_sha256()


def test_qualification_module_has_no_research_scalp_dependency() -> None:
    source = Path(qualification_module.__file__).read_text(encoding="utf-8")
    assert "fxstack.scalp" not in source
