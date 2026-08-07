from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from fxstack.runtime.scalp_rollover_guard import (
    DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY,
    PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION,
    ProductionScalpRolloverPolicy,
    _minute_in_daily_window,
    evaluate_production_scalp_rollover_guard,
)


def _epoch(hour: int, minute: int, second: int = 0, *, day: int = 3) -> float:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    ("hour", "minute", "second"),
    (
        (20, 19, 59),
        (20, 20, 0),
        (20, 49, 59),
        (20, 50, 0),
        (20, 59, 59),
        (21, 0, 0),
        (21, 59, 59),
        (22, 0, 0),
        (22, 9, 59),
        (22, 10, 0),
    ),
)
def test_funding_boundary_clock_is_diagnostic_only(
    hour: int,
    minute: int,
    second: int,
) -> None:
    decision = evaluate_production_scalp_rollover_guard(
        _epoch(hour, minute, second)
    )

    assert decision.accepted is True
    assert decision.utc_second_of_day == hour * 3600 + minute * 60 + second
    assert decision.entry_blackout_active is False
    assert decision.force_close_active is False
    assert decision.entry_allowed is True


def test_london_2200_gmt_and_bst_do_not_create_a_runtime_clock_gate() -> None:
    london = ZoneInfo("Europe/London")
    winter = datetime(2026, 1, 15, 22, 0, tzinfo=london).astimezone(UTC)
    summer = datetime(2026, 7, 15, 22, 0, tzinfo=london).astimezone(UTC)

    assert (winter.hour, winter.minute) == (22, 0)
    assert (summer.hour, summer.minute) == (21, 0)
    for boundary in (winter, summer):
        decision = evaluate_production_scalp_rollover_guard(
            boundary.timestamp()
        )
        assert decision.entry_allowed is True
        assert decision.force_close_active is False


def test_dst_transition_days_remain_non_binding() -> None:
    london = ZoneInfo("Europe/London")
    for local_day, expected_utc_hour in (
        ((2026, 3, 29), 21),
        ((2026, 10, 25), 22),
    ):
        boundary = datetime(
            *local_day,
            22,
            0,
            tzinfo=london,
        ).astimezone(UTC)
        assert boundary.hour == expected_utc_hour
        decision = evaluate_production_scalp_rollover_guard(boundary.timestamp())
        assert decision.entry_allowed is True
        assert decision.force_close_active is False


def test_daily_reset_and_generic_interval_wrap_are_half_open() -> None:
    before_midnight = evaluate_production_scalp_rollover_guard(
        _epoch(23, 59, 59)
    )
    after_midnight = evaluate_production_scalp_rollover_guard(
        _epoch(0, 0, 0, day=4)
    )

    assert before_midnight.entry_allowed is True
    assert after_midnight.entry_allowed is True
    assert _minute_in_daily_window(
        23 * 60 + 55,
        start_minute=23 * 60 + 50,
        end_minute=10,
    )
    assert _minute_in_daily_window(
        5,
        start_minute=23 * 60 + 50,
        end_minute=10,
    )
    assert not _minute_in_daily_window(
        10,
        start_minute=23 * 60 + 50,
        end_minute=10,
    )


def test_guard_policy_is_frozen_versioned_and_hashes_every_boundary_constant() -> None:
    policy = DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY
    payload = policy.to_canonical_dict()

    assert policy.validation_error() == ""
    assert payload["schema_version"] == (
        PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION
    )
    assert payload["entry_blackout_start_second"] == 20 * 3600 + 20 * 60
    assert payload["forced_close_start_second"] == 20 * 3600 + 50 * 60
    assert payload["blackout_end_second"] == 22 * 3600 + 10 * 60
    assert len(policy.config_sha256()) == 64
    assert replace(policy, broker_close_slack_seconds=11 * 60).config_sha256() != (
        policy.config_sha256()
    )
    with pytest.raises(FrozenInstanceError):
        policy.max_time_stop_bars = 21  # type: ignore[misc]


@pytest.mark.parametrize("as_of", (None, "", 0, float("nan"), float("inf")))
def test_invalid_clock_does_not_create_an_entry_blocker(as_of: object) -> None:
    decision = evaluate_production_scalp_rollover_guard(as_of)
    expected_payload = asdict(decision)
    expected_payload["entry_allowed"] = decision.entry_allowed

    assert decision.accepted is True
    assert decision.entry_allowed is True
    assert decision.entry_blackout_active is False
    assert decision.force_close_active is False
    assert decision.reason == ""
    assert decision.to_dict() == expected_payload


def test_invalid_policy_fails_closed() -> None:
    policy = ProductionScalpRolloverPolicy(max_time_stop_bars=0)
    decision = evaluate_production_scalp_rollover_guard(
        _epoch(12, 0),
        policy=policy,
    )

    assert decision.accepted is False
    assert decision.entry_allowed is False
    assert decision.reason == "production_scalp_rollover_max_time_stop_invalid"
