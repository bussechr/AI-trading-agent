from __future__ import annotations

import math

from fxstack.strategy.allocator import (
    allocate_candidates,
    build_allocator_candidate,
    replacement_pressure_score,
    spread_cost_penalty,
)
from fxstack.strategy.allocator_types import AllocatorConfig, AllocatorOpenPosition, SleeveHealthSnapshot


def _candidate(*, candidate_id: str, index: int, quality: float, spread_bps: float = 1.0):
    config = AllocatorConfig(
        max_total_positions=4,
        max_pair_positions=1,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    return build_allocator_candidate(
        candidate_id=candidate_id,
        index=index,
        pair="EURUSD" if index == 0 else "USDJPY",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve="trend_pullback",
        environment_state="PersistentTrend",
        session_bucket="london_open",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=quality,
        location_score=quality,
        trigger_score=quality,
        adaptive_entry_quality=quality,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=spread_bps,
        max_spread_bps=2.5,
        macro_coherence_score=quality,
        currency_crowding_penalty=0.0,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=[],
        sleeve_health=SleeveHealthSnapshot(sleeve="trend_pullback", score=0.6),
    )


def test_nonfinite_allocator_inputs_are_diagnostic_and_cannot_win_selection() -> None:
    config = AllocatorConfig(4, 1, 1, 2.5, 3.0)
    valid = _candidate(candidate_id="valid", index=0, quality=0.65)
    malformed = _candidate(candidate_id="malformed", index=1, quality=float("nan"), spread_bps=float("nan"))

    ranked, summary = allocate_candidates(
        candidates=[malformed, valid],
        open_positions=[],
        remaining_slots=1,
        config=config,
        tempo_gap_active=False,
    )

    by_id = {item.candidate_id: item for item in ranked}
    assert by_id["valid"].allocator_selected is True
    assert by_id["malformed"].allocator_selected is False
    assert by_id["malformed"].allocator_rejection_reason == "invalid_numeric_inputs"
    assert by_id["malformed"].numeric_inputs_valid is False
    assert "nonfinite:adaptive_entry_quality" in by_id["malformed"].numeric_input_errors
    assert "nonfinite:spread_bps" in by_id["malformed"].numeric_input_errors
    assert math.isfinite(by_id["malformed"].allocator_score)
    assert by_id["malformed"].spread_cost_penalty == 1.0
    assert summary.selected_count == 1


def test_allocator_penalties_treat_nonfinite_market_and_position_values_conservatively() -> None:
    assert spread_cost_penalty(spread_bps=float("nan"), max_spread_bps=2.5) == 1.0
    pressure = replacement_pressure_score(
        [
            AllocatorOpenPosition(
                position_id="bad-keep",
                pair="EURUSD",
                side="BUY",
                sleeve="trend_pullback",
                session_bucket="london_open",
                keep_score=float("nan"),
                age_bars=4.0,
                protected_hold=False,
                replaceable_hold=True,
            )
        ]
    )
    assert pressure == 1.0
