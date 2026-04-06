# AGENT: ROLE: Shared thesis-campaign state machine for twin replay and live runtime adaptive sequencing.
# AGENT: ENTRYPOINT: imported by `tools/fxstack_digital_twin_backtest.py` and `fxstack/runtime/runner.py`.
# AGENT: PRIMARY INPUTS: adaptive candidate diagnostics, open-position lifecycle context, campaign registry state.
# AGENT: PRIMARY OUTPUTS: thesis IDs, campaign snapshots, transition decisions, allocator/lifecycle modifiers.
# AGENT: DEPENDS ON: `fxstack/strategy/campaign_types.py`.
# AGENT: CALLED BY: twin replay and runtime adaptive paths.
# AGENT: STATE / SIDE EFFECTS: pure calculations; caller owns registry persistence and event logs.
# AGENT: HANDSHAKES: thesis-state seam between allocator ranking and lifecycle replacement protection.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/strategy/allocator.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fxstack.strategy.campaign_types import (
    CampaignConfig,
    CampaignDecisionContext,
    CampaignRegistryEntry,
    CampaignSnapshot,
    CampaignTransition,
)


PLAYBOOK_TREND_PULLBACK = "trend_pullback"
PLAYBOOK_RANGE_MEAN_REVERSION = "range_mean_reversion"
PLAYBOOK_BREAKOUT_EXPANSION = "breakout_expansion"
PLAYBOOK_FAILED_BREAKOUT_REVERSAL = "failed_breakout_reversal"
CAMPAIGN_ENABLED_SLEEVES = {
    PLAYBOOK_TREND_PULLBACK,
    PLAYBOOK_RANGE_MEAN_REVERSION,
    PLAYBOOK_BREAKOUT_EXPANSION,
    PLAYBOOK_FAILED_BREAKOUT_REVERSAL,
}

CAMPAIGN_STATE_INACTIVE = "inactive"
CAMPAIGN_STATE_PROBE = "probe"
CAMPAIGN_STATE_CONFIRMED = "confirmed"
CAMPAIGN_STATE_PRESS = "press"
CAMPAIGN_STATE_HARVEST = "harvest"
CAMPAIGN_STATE_REATTACK_READY = "re_attack_ready"
CAMPAIGN_STATE_ABANDONED = "abandoned"
CAMPAIGN_ACTIVE_STATES = {
    CAMPAIGN_STATE_PROBE,
    CAMPAIGN_STATE_CONFIRMED,
    CAMPAIGN_STATE_PRESS,
    CAMPAIGN_STATE_HARVEST,
}
CAMPAIGN_MEMORY_STATES = {
    CAMPAIGN_STATE_INACTIVE,
    CAMPAIGN_STATE_REATTACK_READY,
    CAMPAIGN_STATE_ABANDONED,
}


def clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def campaign_config_from_settings(settings: Any) -> CampaignConfig:
    return CampaignConfig(
        enabled=bool(getattr(settings, "campaign_manager_enabled", False)),
        shadow_only=bool(getattr(settings, "campaign_shadow_only", True)),
        abandon_cooldown_bars=max(1, int(getattr(settings, "campaign_abandon_cooldown_bars", 8) or 8)),
        press_protected_bars=max(1, int(getattr(settings, "campaign_press_protected_bars", 4) or 4)),
        reattack_cooldown_scale=float(getattr(settings, "campaign_reattack_cooldown_scale", 0.5) or 0.5),
    )


def build_thesis_id(pair: str, side: str, sleeve: str) -> str:
    pair_key = str(pair or "").upper().strip()
    side_key = str(side or "").lower().strip()
    sleeve_key = str(sleeve or "").strip()
    return f"{pair_key}:{side_key}:{sleeve_key}"


def campaign_enabled_for_sleeve(sleeve: str) -> bool:
    return str(sleeve or "").strip() in CAMPAIGN_ENABLED_SLEEVES


def campaign_profile_for_sleeve(sleeve: str) -> dict[str, float]:
    sleeve_key = str(sleeve or "").strip()
    defaults = {
        "reattack_reset_quality": 0.62,
        "probe_abandon": 0.86,
        "probe_confirm_proof": 0.54,
        "probe_confirm_trigger": 0.42,
        "probe_confirm_macro": 0.48,
        "probe_min_age": 1.0,
        "confirm_abandon": 0.93,
        "press_proof": 0.66,
        "press_env_stability": 0.58,
        "press_progress": 0.10,
        "press_extension_max": 0.72,
        "press_min_age": 2.0,
        "harvest_maturity": 0.68,
        "harvest_profit_extension": 0.55,
        "harvest_extension": 0.82,
    }
    if sleeve_key == PLAYBOOK_RANGE_MEAN_REVERSION:
        defaults.update(
            {
                "reattack_reset_quality": 0.58,
                "probe_abandon": 0.82,
                "probe_confirm_proof": 0.50,
                "probe_confirm_trigger": 0.40,
                "probe_confirm_macro": 0.44,
                "press_proof": 0.60,
                "press_env_stability": 0.50,
                "press_progress": 0.05,
                "press_extension_max": 0.62,
                "press_min_age": 1.0,
                "harvest_maturity": 0.52,
                "harvest_profit_extension": 0.34,
                "harvest_extension": 0.60,
            }
        )
    elif sleeve_key == PLAYBOOK_BREAKOUT_EXPANSION:
        defaults.update(
            {
                "reattack_reset_quality": 0.60,
                "probe_abandon": 0.84,
                "probe_confirm_proof": 0.55,
                "probe_confirm_trigger": 0.52,
                "probe_confirm_macro": 0.52,
                "press_proof": 0.70,
                "press_env_stability": 0.62,
                "press_progress": 0.08,
                "press_extension_max": 0.78,
                "press_min_age": 1.0,
                "harvest_maturity": 0.72,
                "harvest_profit_extension": 0.48,
                "harvest_extension": 0.86,
            }
        )
    elif sleeve_key == PLAYBOOK_FAILED_BREAKOUT_REVERSAL:
        defaults.update(
            {
                "reattack_reset_quality": 0.60,
                "probe_abandon": 0.83,
                "probe_confirm_proof": 0.56,
                "probe_confirm_trigger": 0.46,
                "probe_confirm_macro": 0.50,
                "press_proof": 0.68,
                "press_env_stability": 0.54,
                "press_progress": 0.06,
                "press_extension_max": 0.84,
                "press_min_age": 1.0,
                "harvest_maturity": 0.66,
                "harvest_profit_extension": 0.42,
                "harvest_extension": 0.88,
            }
        )
    return defaults


def campaign_is_memory_state(state: str) -> bool:
    return str(state or "") in CAMPAIGN_MEMORY_STATES


def campaign_is_active_state(state: str) -> bool:
    return str(state or "") in CAMPAIGN_ACTIVE_STATES


def campaign_priority_boost(state: str) -> float:
    mapping = {
        CAMPAIGN_STATE_INACTIVE: 0.0,
        CAMPAIGN_STATE_REATTACK_READY: 0.08,
        CAMPAIGN_STATE_PROBE: 0.0,
        CAMPAIGN_STATE_CONFIRMED: 0.04,
        CAMPAIGN_STATE_PRESS: 0.08,
        CAMPAIGN_STATE_HARVEST: -0.01,
        CAMPAIGN_STATE_ABANDONED: 0.0,
    }
    return float(mapping.get(str(state or ""), 0.0))


def campaign_keep_adjustment(state: str) -> float:
    mapping = {
        CAMPAIGN_STATE_PROBE: 0.0,
        CAMPAIGN_STATE_CONFIRMED: 0.06,
        CAMPAIGN_STATE_PRESS: 0.12,
        CAMPAIGN_STATE_HARVEST: -0.02,
        CAMPAIGN_STATE_REATTACK_READY: 0.0,
        CAMPAIGN_STATE_ABANDONED: 0.0,
        CAMPAIGN_STATE_INACTIVE: 0.0,
    }
    return float(mapping.get(str(state or ""), 0.0))


def campaign_replacement_margin_delta(state: str) -> float:
    mapping = {
        CAMPAIGN_STATE_CONFIRMED: 0.03,
        CAMPAIGN_STATE_PRESS: 0.08,
        CAMPAIGN_STATE_HARVEST: -0.02,
    }
    return float(mapping.get(str(state or ""), 0.0))


def campaign_cooldown_scale(state: str, config: CampaignConfig) -> float:
    if str(state or "") == CAMPAIGN_STATE_REATTACK_READY:
        return float(max(0.1, min(float(config.reattack_cooldown_scale), 0.35)))
    return 1.0


def _entry_context_from_row(*, pair: str, side: str, sleeve: str, row: dict[str, Any], bar_idx: int, ts: str) -> CampaignDecisionContext:
    return CampaignDecisionContext(
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        bar_idx=int(bar_idx),
        ts=str(ts),
        playbook_score=float(row.get("playbook_score", 0.0) or 0.0),
        location_score=float(row.get("location_score", 0.0) or 0.0),
        trigger_score=float(row.get("trigger_score", 0.0) or 0.0),
        macro_coherence_score=float(row.get("macro_coherence_score", 0.0) or 0.0),
        hostility_score=float(row.get("hostility_score", 0.0) or 0.0),
        extension_penalty_score=float(row.get("extension_penalty_score", 0.0) or 0.0),
        environment_state=str(row.get("environment_state") or ""),
        entry_trade_prob=float(row.get("trade_prob", row.get("entry_trade_prob", 0.0)) or 0.0),
    )


# AGENT HOT PATH: campaign scores intentionally reuse adaptive lifecycle primitives so campaign-lite changes sequencing, not model inputs.
def derive_campaign_scores(context: CampaignDecisionContext) -> dict[str, float]:
    unrealized_progress_score = (
        clip01(float(context.unrealized_pnl_usd) / max(float(context.open_equity_usd) * 0.005, 1.0))
        if float(context.unrealized_pnl_usd) > 0.0 and float(context.open_equity_usd) > 0.0
        else 0.0
    )
    thesis_integrity = clip01(
        (0.45 * float(context.playbook_score))
        + (0.30 * float(context.location_score))
        + (0.15 * float(context.trigger_score))
        + (0.10 * float(context.macro_coherence_score))
    )
    environment_stability = clip01(1.0 - float(context.hostility_score))
    thesis_decay = clip01(1.0 - thesis_integrity)
    profit_extension_score = clip01(unrealized_progress_score * float(context.extension_penalty_score))
    age_decay_score = clip01(float(context.age_bars) / 16.0)
    playbook_invalidation_score = clip01(
        (0.45 * thesis_decay)
        + (0.30 * float(context.hostility_score))
        + (0.25 * (1.0 - float(context.macro_coherence_score)))
    )
    environment_deterioration_score = clip01(float(context.hostility_score))
    trigger_failure_score = clip01(1.0 - float(context.trigger_score))
    campaign_maturity_score = clip01(
        (0.45 * profit_extension_score)
        + (0.30 * float(context.extension_penalty_score))
        + (0.25 * age_decay_score)
    )
    campaign_reset_quality = clip01(
        (0.40 * float(context.location_score))
        + (0.30 * float(context.trigger_score))
        + (0.20 * (1.0 - float(context.extension_penalty_score)))
        + (0.10 * float(context.macro_coherence_score))
    )
    campaign_abandon_score = clip01(
        max(
            float(playbook_invalidation_score),
            float(environment_deterioration_score),
            float(trigger_failure_score),
        )
    )
    return {
        "unrealized_progress_score": float(unrealized_progress_score),
        "thesis_integrity": float(thesis_integrity),
        "environment_stability": float(environment_stability),
        "thesis_decay": float(thesis_decay),
        "profit_extension_score": float(profit_extension_score),
        "age_decay_score": float(age_decay_score),
        "playbook_invalidation_score": float(playbook_invalidation_score),
        "environment_deterioration_score": float(environment_deterioration_score),
        "trigger_failure_score": float(trigger_failure_score),
        "campaign_proof_score": float(thesis_integrity),
        "campaign_maturity_score": float(campaign_maturity_score),
        "campaign_reset_quality": float(campaign_reset_quality),
        "campaign_abandon_score": float(campaign_abandon_score),
    }


def _registry_entry(
    registry: dict[str, CampaignRegistryEntry],
    *,
    thesis_id: str,
    pair: str,
    side: str,
    sleeve: str,
) -> CampaignRegistryEntry:
    current = registry.get(str(thesis_id))
    if current is not None:
        return current
    current = CampaignRegistryEntry(
        thesis_id=str(thesis_id),
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
    )
    registry[str(thesis_id)] = current
    return current


def _inactive_snapshot(*, thesis_id: str, pair: str, side: str, sleeve: str, scores: dict[str, float] | None = None, campaign_seq: int = 0, entry_kind: str = "", state_reason: str = "campaign_memory_inactive") -> CampaignSnapshot:
    score_map = scores or {}
    return CampaignSnapshot(
        thesis_id=str(thesis_id),
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        campaign_seq=int(campaign_seq),
        entry_kind=str(entry_kind),
        state=CAMPAIGN_STATE_INACTIVE,
        state_reason=str(state_reason),
        proof_score=float(score_map.get("campaign_proof_score", 0.0) or 0.0),
        maturity_score=float(score_map.get("campaign_maturity_score", 0.0) or 0.0),
        reset_quality=float(score_map.get("campaign_reset_quality", 0.0) or 0.0),
        abandon_score=float(score_map.get("campaign_abandon_score", 0.0) or 0.0),
        priority_boost=0.0,
        reentry_blocked=False,
        reentry_block_reason="",
        keep_adjustment=0.0,
        replacement_margin_delta=0.0,
        press_protected=False,
    )


# AGENT HOT PATH: entry-time evaluation is memory-only; campaign-lite never creates active states until a real fill happens.
def evaluate_entry_campaign_memory(
    *,
    pair: str,
    side: str,
    sleeve: str,
    row: dict[str, Any],
    bar_idx: int,
    ts: str,
    registry: dict[str, CampaignRegistryEntry],
    config: CampaignConfig,
) -> CampaignSnapshot:
    thesis_id = build_thesis_id(pair, side, sleeve)
    entry = _registry_entry(registry, thesis_id=thesis_id, pair=pair, side=side, sleeve=sleeve)
    ctx = _entry_context_from_row(pair=pair, side=side, sleeve=sleeve, row=row, bar_idx=bar_idx, ts=ts)
    scores = derive_campaign_scores(ctx)
    profile = campaign_profile_for_sleeve(sleeve)
    if (not bool(config.enabled)) or (not campaign_enabled_for_sleeve(sleeve)):
        return _inactive_snapshot(
            thesis_id=thesis_id,
            pair=pair,
            side=side,
            sleeve=sleeve,
            scores=scores,
            campaign_seq=int(entry.campaign_seq),
            entry_kind=str(entry.entry_kind or ""),
        )

    state = CAMPAIGN_STATE_INACTIVE
    reason = "campaign_memory_inactive"
    reentry_blocked = False
    reentry_block_reason = ""
    entry_kind = ""

    if str(entry.state) == CAMPAIGN_STATE_ABANDONED and entry.abandoned_at_bar is not None:
        since = max(0, int(bar_idx) - int(entry.abandoned_at_bar))
        if since < int(config.abandon_cooldown_bars):
            state = CAMPAIGN_STATE_ABANDONED
            reason = "campaign_abandon_cooldown"
            reentry_blocked = True
            reentry_block_reason = reason
        else:
            state = CAMPAIGN_STATE_INACTIVE
            reason = "campaign_abandon_cooldown_expired"
    elif (
        str(entry.state) == CAMPAIGN_STATE_REATTACK_READY
        and not bool(entry.campaign_active)
        and float(scores["campaign_reset_quality"]) >= float(profile["reattack_reset_quality"])
    ):
        state = CAMPAIGN_STATE_REATTACK_READY
        reason = "campaign_re_attack_ready"
        entry_kind = "re_attack_entry"

    return CampaignSnapshot(
        thesis_id=str(thesis_id),
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        campaign_seq=int(entry.campaign_seq),
        entry_kind=str(entry_kind or entry.entry_kind or ""),
        state=str(state),
        state_reason=str(reason),
        proof_score=float(scores["campaign_proof_score"]),
        maturity_score=float(scores["campaign_maturity_score"]),
        reset_quality=float(scores["campaign_reset_quality"]),
        abandon_score=float(scores["campaign_abandon_score"]),
        priority_boost=float(campaign_priority_boost(state)),
        reentry_blocked=bool(reentry_blocked),
        reentry_block_reason=str(reentry_block_reason),
        keep_adjustment=0.0,
        replacement_margin_delta=0.0,
        press_protected=False,
    )


# AGENT PARITY: keep the old symbol as a wrapper so runtime imports stay stable while twin adopts the memory-only semantics.
def evaluate_entry_campaign(**kwargs: Any) -> CampaignSnapshot:
    return evaluate_entry_campaign_memory(**kwargs)


def start_campaign_on_entry(
    *,
    pair: str,
    side: str,
    sleeve: str,
    row: dict[str, Any],
    bar_idx: int,
    ts: str,
    registry: dict[str, CampaignRegistryEntry],
    prior_snapshot: CampaignSnapshot | None = None,
) -> CampaignSnapshot:
    thesis_id = build_thesis_id(pair, side, sleeve)
    entry = _registry_entry(registry, thesis_id=thesis_id, pair=pair, side=side, sleeve=sleeve)
    ctx = _entry_context_from_row(pair=pair, side=side, sleeve=sleeve, row=row, bar_idx=bar_idx, ts=ts)
    scores = derive_campaign_scores(ctx)
    if not campaign_enabled_for_sleeve(sleeve):
        return _inactive_snapshot(
            thesis_id=thesis_id,
            pair=pair,
            side=side,
            sleeve=sleeve,
            scores=scores,
            campaign_seq=0,
            entry_kind="",
            state_reason="campaign_disabled_for_sleeve",
        )

    prior_state = str(prior_snapshot.state if prior_snapshot is not None else entry.state or CAMPAIGN_STATE_INACTIVE)
    prior_seq = int(entry.campaign_seq)
    campaign_seq = prior_seq + 1
    entry_kind = "re_attack_entry" if prior_state == CAMPAIGN_STATE_REATTACK_READY else "fresh_probe"
    return CampaignSnapshot(
        thesis_id=str(thesis_id),
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        campaign_seq=int(campaign_seq),
        entry_kind=str(entry_kind),
        state=CAMPAIGN_STATE_PROBE,
        state_reason=str(entry_kind),
        proof_score=float(scores["campaign_proof_score"]),
        maturity_score=float(scores["campaign_maturity_score"]),
        reset_quality=float(scores["campaign_reset_quality"]),
        abandon_score=float(scores["campaign_abandon_score"]),
        priority_boost=0.0,
        reentry_blocked=False,
        reentry_block_reason="",
        keep_adjustment=0.0,
        replacement_margin_delta=0.0,
        press_protected=False,
    )


# AGENT HOT PATH: campaign sequencing is shared across active sleeves, but thresholds stay sleeve-specific so trend/range/breakout theses pace differently.
def evaluate_open_campaign(
    *,
    pair: str,
    side: str,
    sleeve: str,
    current_state: str,
    row: dict[str, Any],
    unrealized_pnl_usd: float,
    age_bars: float,
    open_equity_usd: float,
    bar_idx: int,
    ts: str,
    lifecycle_action: str,
    lifecycle_reason: str,
    reversal_ready: bool,
    severe_invalidation: bool,
    config: CampaignConfig,
    campaign_seq: int = 0,
    entry_kind: str = "",
) -> CampaignSnapshot:
    thesis_id = build_thesis_id(pair, side, sleeve)
    ctx = CampaignDecisionContext(
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        bar_idx=int(bar_idx),
        ts=str(ts),
        playbook_score=float(row.get("playbook_score", 0.0) or 0.0),
        location_score=float(row.get("location_score", 0.0) or 0.0),
        trigger_score=float(row.get("trigger_score", 0.0) or 0.0),
        macro_coherence_score=float(row.get("macro_coherence_score", 0.0) or 0.0),
        hostility_score=float(row.get("hostility_score", 0.0) or 0.0),
        extension_penalty_score=float(row.get("extension_penalty_score", 0.0) or 0.0),
        environment_state=str(row.get("environment_state") or ""),
        unrealized_pnl_usd=float(unrealized_pnl_usd),
        age_bars=float(age_bars),
        open_equity_usd=float(open_equity_usd),
        lifecycle_action=str(lifecycle_action or "hold"),
        lifecycle_reason=str(lifecycle_reason or "hold"),
        reversal_ready=bool(reversal_ready),
        severe_invalidation=bool(severe_invalidation),
    )
    scores = derive_campaign_scores(ctx)
    profile = campaign_profile_for_sleeve(sleeve)
    if (not bool(config.enabled)) or (not campaign_enabled_for_sleeve(sleeve)):
        return _inactive_snapshot(
            thesis_id=thesis_id,
            pair=pair,
            side=side,
            sleeve=sleeve,
            scores=scores,
            campaign_seq=int(campaign_seq),
            entry_kind=str(entry_kind),
            state_reason="campaign_disabled_for_sleeve",
        )

    prior_state = str(current_state or CAMPAIGN_STATE_PROBE)
    if campaign_is_active_state(prior_state):
        next_state = prior_state
    elif prior_state == CAMPAIGN_STATE_ABANDONED:
        next_state = CAMPAIGN_STATE_ABANDONED
    else:
        next_state = CAMPAIGN_STATE_PROBE
    reason = "campaign_hold"
    hostile_env = str(ctx.environment_state) == "DislocatedHostile" or float(ctx.hostility_score) >= 0.95

    if prior_state == CAMPAIGN_STATE_PROBE:
        if bool(severe_invalidation) or hostile_env or float(scores["campaign_abandon_score"]) >= float(profile["probe_abandon"]):
            next_state = CAMPAIGN_STATE_ABANDONED
            reason = "campaign_probe_abandoned"
        elif (
            float(scores["campaign_proof_score"]) >= float(profile["probe_confirm_proof"])
            and float(ctx.trigger_score) >= float(profile["probe_confirm_trigger"])
            and float(ctx.macro_coherence_score) >= float(profile["probe_confirm_macro"])
            and float(age_bars) >= float(profile["probe_min_age"])
        ):
            next_state = CAMPAIGN_STATE_CONFIRMED
            reason = "campaign_confirmed"
    elif prior_state == CAMPAIGN_STATE_CONFIRMED:
        if bool(severe_invalidation) or hostile_env or float(scores["campaign_abandon_score"]) >= float(profile["confirm_abandon"]):
            next_state = CAMPAIGN_STATE_ABANDONED
            reason = "campaign_confirmed_abandoned"
        elif (
            float(scores["campaign_proof_score"]) >= float(profile["press_proof"])
            and float(scores["environment_stability"]) >= float(profile["press_env_stability"])
            and float(scores["unrealized_progress_score"]) >= float(profile["press_progress"])
            and float(ctx.extension_penalty_score) < float(profile["press_extension_max"])
            and float(age_bars) >= float(profile["press_min_age"])
        ):
            next_state = CAMPAIGN_STATE_PRESS
            reason = "campaign_press"
        elif (
            float(scores["campaign_maturity_score"]) >= float(profile["harvest_maturity"])
            or float(scores["profit_extension_score"]) >= float(profile["harvest_profit_extension"])
            or float(ctx.extension_penalty_score) >= float(profile["harvest_extension"])
        ):
            next_state = CAMPAIGN_STATE_HARVEST
            reason = "campaign_harvest"
    elif prior_state == CAMPAIGN_STATE_PRESS:
        if bool(severe_invalidation) or hostile_env or float(scores["campaign_abandon_score"]) >= float(profile["confirm_abandon"]):
            next_state = CAMPAIGN_STATE_ABANDONED
            reason = "campaign_press_abandoned"
        elif (
            float(scores["campaign_maturity_score"]) >= float(profile["harvest_maturity"])
            or float(scores["profit_extension_score"]) >= float(profile["harvest_profit_extension"])
            or float(ctx.extension_penalty_score) >= float(profile["harvest_extension"])
        ):
            next_state = CAMPAIGN_STATE_HARVEST
            reason = "campaign_harvest"
    elif prior_state == CAMPAIGN_STATE_HARVEST:
        if bool(severe_invalidation) or hostile_env or bool(reversal_ready):
            next_state = CAMPAIGN_STATE_ABANDONED
            reason = "campaign_harvest_abandoned"
        else:
            next_state = CAMPAIGN_STATE_HARVEST
            reason = "campaign_harvest_hold"

    press_protected = bool(next_state == CAMPAIGN_STATE_PRESS and float(age_bars) <= float(config.press_protected_bars))
    keep_adjustment = float(campaign_keep_adjustment(next_state))
    if next_state == CAMPAIGN_STATE_CONFIRMED and float(scores["campaign_proof_score"]) < 0.42:
        keep_adjustment = 0.0

    return CampaignSnapshot(
        thesis_id=str(thesis_id),
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        campaign_seq=int(campaign_seq),
        entry_kind=str(entry_kind),
        state=str(next_state),
        state_reason=str(reason),
        proof_score=float(scores["campaign_proof_score"]),
        maturity_score=float(scores["campaign_maturity_score"]),
        reset_quality=float(scores["campaign_reset_quality"]),
        abandon_score=float(scores["campaign_abandon_score"]),
        priority_boost=float(campaign_priority_boost(next_state)),
        reentry_blocked=False,
        reentry_block_reason="",
        keep_adjustment=float(keep_adjustment),
        replacement_margin_delta=float(campaign_replacement_margin_delta(next_state)),
        press_protected=bool(press_protected),
    )


# AGENT HANDSHAKE: lifecycle overrides run after the adaptive lifecycle model so campaign-lite changes sequencing without replacing model outputs.
def apply_campaign_lifecycle_overrides(
    *,
    snapshot: CampaignSnapshot,
    lifecycle_action: str,
    lifecycle_reason: str,
    unrealized_pnl_usd: float,
    severe_invalidation: bool,
) -> dict[str, Any]:
    action = str(lifecycle_action or "hold")
    reason = str(lifecycle_reason or "hold")
    state = str(snapshot.state or CAMPAIGN_STATE_INACTIVE)
    if state == CAMPAIGN_STATE_ABANDONED:
        action = "exit"
        reason = "adaptive_campaign_abandoned_exit"
    elif state == CAMPAIGN_STATE_PRESS and not bool(severe_invalidation):
        if reason == "adaptive_tempo_rotation_exit":
            action = "hold"
            reason = "adaptive_campaign_press_hold"
    elif state == CAMPAIGN_STATE_CONFIRMED and reason == "adaptive_tempo_rotation_exit" and float(snapshot.proof_score) >= 0.42:
        action = "hold"
        reason = "adaptive_campaign_confirmed_hold"
    elif state == CAMPAIGN_STATE_HARVEST:
        if action == "hold" and float(unrealized_pnl_usd) > 0.0 and float(snapshot.maturity_score) >= 0.60:
            action = "partial_tp"
            reason = "adaptive_campaign_harvest"
    elif state == CAMPAIGN_STATE_PROBE:
        if bool(severe_invalidation):
            action = "exit"
            reason = "adaptive_campaign_probe_failed"
    return {
        "lifecycle_action": str(action),
        "lifecycle_reason": str(reason),
    }


def campaign_state_after_close(
    *,
    position_state: str,
    pair: str,
    side: str,
    sleeve: str,
    row: dict[str, Any],
    lifecycle_reason: str,
    realized_pnl_usd: float,
    bar_idx: int,
    ts: str,
    config: CampaignConfig,
    campaign_seq: int = 0,
    entry_kind: str = "",
) -> CampaignSnapshot:
    thesis_id = build_thesis_id(pair, side, sleeve)
    ctx = _entry_context_from_row(pair=pair, side=side, sleeve=sleeve, row=row, bar_idx=bar_idx, ts=ts)
    scores = derive_campaign_scores(ctx)
    profile = campaign_profile_for_sleeve(sleeve)
    if (not bool(config.enabled)) or (not campaign_enabled_for_sleeve(sleeve)):
        return _inactive_snapshot(
            thesis_id=thesis_id,
            pair=pair,
            side=side,
            sleeve=sleeve,
            scores=scores,
            campaign_seq=int(campaign_seq),
            entry_kind=str(entry_kind),
            state_reason="campaign_disabled_for_sleeve",
        )

    invalidating_reasons = {
        "adaptive_breakout_follow_through_failed",
        "adaptive_failed_breakout_invalidated",
        "adaptive_reverse_ready",
        "adaptive_campaign_probe_failed",
        "adaptive_campaign_abandoned_exit",
        "reversal_models_exit",
        "campaign_probe_abandoned",
        "campaign_confirmed_abandoned",
        "campaign_press_abandoned",
        "campaign_harvest_abandoned",
    }
    invalidating = str(lifecycle_reason or "") in invalidating_reasons
    hostile_env = float(ctx.hostility_score) >= 0.60 or str(ctx.environment_state) == "DislocatedHostile"
    harvested_close = str(position_state or "") == CAMPAIGN_STATE_HARVEST or str(lifecycle_reason or "") == "adaptive_campaign_harvest"

    if bool(invalidating):
        state = CAMPAIGN_STATE_ABANDONED
        reason = "campaign_close_abandoned"
        reentry_blocked = True
    elif (
        float(realized_pnl_usd) > 0.0 or bool(harvested_close)
    ) and float(scores["campaign_reset_quality"]) >= float(profile["reattack_reset_quality"]) and not hostile_env:
        state = CAMPAIGN_STATE_REATTACK_READY
        reason = "campaign_close_re_attack_ready"
        reentry_blocked = False
    else:
        state = CAMPAIGN_STATE_INACTIVE
        reason = "campaign_close_inactive"
        reentry_blocked = False

    return CampaignSnapshot(
        thesis_id=str(thesis_id),
        pair=str(pair),
        side=str(side),
        sleeve=str(sleeve),
        campaign_seq=int(campaign_seq),
        entry_kind=str(entry_kind),
        state=str(state),
        state_reason=str(reason),
        proof_score=float(scores["campaign_proof_score"]),
        maturity_score=float(scores["campaign_maturity_score"]),
        reset_quality=float(scores["campaign_reset_quality"]),
        abandon_score=float(scores["campaign_abandon_score"]),
        priority_boost=float(campaign_priority_boost(state)),
        reentry_blocked=bool(reentry_blocked),
        reentry_block_reason="campaign_abandon_cooldown" if bool(reentry_blocked) else "",
        keep_adjustment=0.0,
        replacement_margin_delta=0.0,
        press_protected=False,
    )


# AGENT STATE: registry tracks thesis memory separately from active campaigns so candidate evaluation can read memory without counting synthetic campaigns.
def apply_campaign_registry_snapshot(
    registry: dict[str, CampaignRegistryEntry],
    *,
    snapshot: CampaignSnapshot,
    bar_idx: int,
    ts: str,
    active_position: bool,
    realized_pnl_usd: float = 0.0,
) -> CampaignRegistryEntry:
    entry = _registry_entry(
        registry,
        thesis_id=str(snapshot.thesis_id),
        pair=str(snapshot.pair),
        side=str(snapshot.side),
        sleeve=str(snapshot.sleeve),
    )
    prior_state = str(entry.state)
    prior_active = bool(entry.campaign_active)
    if prior_state != str(snapshot.state):
        entry.state_entered_bar = int(bar_idx)
    if int(snapshot.campaign_seq or 0) > 0:
        entry.campaign_seq = int(snapshot.campaign_seq)
    if str(snapshot.entry_kind or ""):
        entry.entry_kind = str(snapshot.entry_kind)
    entry.state = str(snapshot.state)
    entry.state_reason = str(snapshot.state_reason)
    entry.last_bar_idx = int(bar_idx)
    entry.last_ts = str(ts)
    entry.active_position = bool(active_position)
    entry.campaign_active = bool(active_position and campaign_is_active_state(snapshot.state))
    entry.last_realized_pnl_usd = float(realized_pnl_usd)
    if bool(entry.campaign_active) and (not prior_active):
        entry.campaign_start_bar = int(bar_idx)
        entry.campaign_start_ts = str(ts)
        entry.abandoned_at_bar = None
    if prior_state != str(snapshot.state) and str(snapshot.state) == CAMPAIGN_STATE_HARVEST:
        entry.harvest_count = int(entry.harvest_count) + 1
    if prior_state != str(snapshot.state) and str(snapshot.state) == CAMPAIGN_STATE_REATTACK_READY:
        entry.reattack_count = int(entry.reattack_count) + 1
    if prior_state != str(snapshot.state) and str(snapshot.state) == CAMPAIGN_STATE_ABANDONED:
        entry.abandoned_at_bar = int(bar_idx)
    if prior_active and not bool(entry.campaign_active):
        entry.completed_count = int(entry.completed_count) + 1
    return entry


def campaign_transition_if_changed(
    *,
    prior_state: str,
    snapshot: CampaignSnapshot,
    bar_idx: int,
    ts: str,
    realized_pnl_usd: float = 0.0,
    unrealized_pnl_usd: float = 0.0,
    holding_bars: float = 0.0,
    trade_id: str = "",
) -> CampaignTransition | None:
    if str(prior_state or "") == str(snapshot.state or ""):
        return None
    return CampaignTransition(
        thesis_id=str(snapshot.thesis_id),
        pair=str(snapshot.pair),
        side=str(snapshot.side),
        sleeve=str(snapshot.sleeve),
        prior_state=str(prior_state or CAMPAIGN_STATE_INACTIVE),
        new_state=str(snapshot.state or CAMPAIGN_STATE_INACTIVE),
        reason=str(snapshot.state_reason or ""),
        bar_idx=int(bar_idx),
        ts=str(ts),
        campaign_seq=int(snapshot.campaign_seq or 0),
        entry_kind=str(snapshot.entry_kind or ""),
        realized_pnl_usd=float(realized_pnl_usd),
        unrealized_pnl_usd=float(unrealized_pnl_usd),
        holding_bars=float(holding_bars),
        trade_id=str(trade_id or ""),
    )


def serialize_campaign_entry(entry: CampaignRegistryEntry) -> dict[str, Any]:
    return asdict(entry)


def serialize_campaign_snapshot(snapshot: CampaignSnapshot) -> dict[str, Any]:
    return asdict(snapshot)
