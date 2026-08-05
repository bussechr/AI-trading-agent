# AGENT: ROLE: Pure UTC funding-boundary guard for every production-scalper symbol.
# AGENT: ENTRYPOINT: `evaluate_production_scalp_rollover_guard`.
# AGENT: PRIMARY INPUTS: one UTC as-of timestamp and immutable production policy.
# AGENT: PRIMARY OUTPUTS: fail-closed entry blackout and pre-funding close posture.
# AGENT: STATE / SIDE EFFECTS: none; no settings, store, service, broker, or I/O access.
"""Expose IG's daily funding boundary as non-binding diagnostics.

IG applies its daily funding boundary at 22:00 Europe/London.  That is 21:00
UTC while British Summer Time is active and 22:00 UTC otherwise.  Production
does not depend on a host timezone database for this safety decision: the
fixed UTC window conservatively covers both possibilities every day.

The entry blackout starts one complete maximum holding horizon plus broker
close slack before the earliest possible boundary.  The forced-close window
starts one close-slack interval before that boundary and stays active through
the latest possible boundary plus the same slack.  All intervals are
half-open, which makes the exact daily reset deterministic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any


PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION = (
    "fxstack.runtime.production_scalp_rollover_guard.v1"
)
IG_FUNDING_BOUNDARY_LONDON_MINUTE = 22 * 60
IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE = 21 * 60
IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE = 22 * 60
# The unissued v4 contract reserves enough room for the fixed 30-M1-bar
# MTVCLC successor horizon.  The current 20-bar dislocation control remains
# valid under this more conservative global maximum.
PRODUCTION_SCALP_MAX_TIME_STOP_BARS = 30
PRODUCTION_SCALP_TIME_STOP_BAR_SECONDS = 60
PRODUCTION_SCALP_BROKER_CLOSE_SLACK_SECONDS = 10 * 60
SECONDS_PER_UTC_DAY = 24 * 60 * 60


def _minute_in_daily_window(
    minute_of_day: int,
    *,
    start_minute: int,
    end_minute: int,
) -> bool:
    """Return membership in a half-open daily interval, including wrap."""

    minute = int(minute_of_day) % (24 * 60)
    start = int(start_minute) % (24 * 60)
    end = int(end_minute) % (24 * 60)
    if start == end:
        return True
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


@dataclass(frozen=True, slots=True)
class ProductionScalpRolloverPolicy:
    """Versioned constants included in the signed strategy config identity."""

    funding_boundary_london_minute: int = IG_FUNDING_BOUNDARY_LONDON_MINUTE
    earliest_funding_boundary_utc_minute: int = (
        IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE
    )
    latest_funding_boundary_utc_minute: int = (
        IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE
    )
    max_time_stop_bars: int = PRODUCTION_SCALP_MAX_TIME_STOP_BARS
    time_stop_bar_seconds: int = PRODUCTION_SCALP_TIME_STOP_BAR_SECONDS
    broker_close_slack_seconds: int = (
        PRODUCTION_SCALP_BROKER_CLOSE_SLACK_SECONDS
    )
    schema_version: str = PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION

    @property
    def max_holding_seconds(self) -> int:
        return int(self.max_time_stop_bars) * int(self.time_stop_bar_seconds)

    @property
    def entry_blackout_start_second(self) -> int:
        return (
            int(self.earliest_funding_boundary_utc_minute) * 60
            - self.max_holding_seconds
            - int(self.broker_close_slack_seconds)
        ) % SECONDS_PER_UTC_DAY

    @property
    def forced_close_start_second(self) -> int:
        return (
            int(self.earliest_funding_boundary_utc_minute) * 60
            - int(self.broker_close_slack_seconds)
        ) % SECONDS_PER_UTC_DAY

    @property
    def blackout_end_second(self) -> int:
        return (
            int(self.latest_funding_boundary_utc_minute) * 60
            + int(self.broker_close_slack_seconds)
        ) % SECONDS_PER_UTC_DAY

    def validation_error(self) -> str:
        if self.schema_version != PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION:
            return "production_scalp_rollover_guard_schema_invalid"
        if self.funding_boundary_london_minute != 22 * 60:
            return "production_scalp_rollover_london_boundary_invalid"
        if not (
            0 <= int(self.earliest_funding_boundary_utc_minute) < 24 * 60
            and 0 <= int(self.latest_funding_boundary_utc_minute) < 24 * 60
            and int(self.earliest_funding_boundary_utc_minute)
            < int(self.latest_funding_boundary_utc_minute)
        ):
            return "production_scalp_rollover_utc_boundaries_invalid"
        if int(self.max_time_stop_bars) <= 0:
            return "production_scalp_rollover_max_time_stop_invalid"
        if int(self.time_stop_bar_seconds) != 60:
            return "production_scalp_rollover_bar_seconds_invalid"
        slack = int(self.broker_close_slack_seconds)
        if slack < 60 or slack > 30 * 60:
            return "production_scalp_rollover_close_slack_invalid"
        if self.max_holding_seconds + slack >= 12 * 60 * 60:
            return "production_scalp_rollover_horizon_invalid"
        return ""

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "funding_boundary_london_minute": int(
                self.funding_boundary_london_minute
            ),
            "earliest_funding_boundary_utc_minute": int(
                self.earliest_funding_boundary_utc_minute
            ),
            "latest_funding_boundary_utc_minute": int(
                self.latest_funding_boundary_utc_minute
            ),
            "max_time_stop_bars": int(self.max_time_stop_bars),
            "time_stop_bar_seconds": int(self.time_stop_bar_seconds),
            "broker_close_slack_seconds": int(
                self.broker_close_slack_seconds
            ),
            "max_holding_seconds": self.max_holding_seconds,
            "entry_blackout_start_second": self.entry_blackout_start_second,
            "forced_close_start_second": self.forced_close_start_second,
            "blackout_end_second": self.blackout_end_second,
        }

    def config_sha256(self) -> str:
        encoded = json.dumps(
            self.to_canonical_dict(),
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ProductionScalpRolloverDecision:
    accepted: bool
    reason: str
    as_of_epoch: float | None
    utc_second_of_day: int | None
    entry_blackout_active: bool
    force_close_active: bool
    entry_blackout_start_second: int
    forced_close_start_second: int
    blackout_end_second: int
    policy_config_sha256: str
    schema_version: str = PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION

    @property
    def entry_allowed(self) -> bool:
        return bool(self.accepted and not self.entry_blackout_active)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["entry_allowed"] = self.entry_allowed
        return payload


DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY = ProductionScalpRolloverPolicy()


def _in_second_window(
    second_of_day: int,
    *,
    start_second: int,
    end_second: int,
) -> bool:
    # Preserve second precision while sharing the same wrap semantics as the
    # minute helper used by contract tests and operator-facing documentation.
    second = int(second_of_day) % SECONDS_PER_UTC_DAY
    start = int(start_second) % SECONDS_PER_UTC_DAY
    end = int(end_second) % SECONDS_PER_UTC_DAY
    if start == end:
        return True
    if start < end:
        return start <= second < end
    return second >= start or second < end


def evaluate_production_scalp_rollover_guard(
    as_of_epoch: Any,
    *,
    policy: ProductionScalpRolloverPolicy = (
        DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY
    ),
) -> ProductionScalpRolloverDecision:
    """Return a non-binding diagnostic snapshot for one instant.

    The scalp loop no longer uses a wall-clock entry blackout or forced-close
    window. Broker availability, current spread, exact finalized bars, the
    signal rules, and the position time stop remain binding.
    """

    policy_error = policy.validation_error()
    try:
        now = float(as_of_epoch)
    except (TypeError, ValueError, OverflowError):
        now = float("nan")
    clock_valid = math.isfinite(now) and now > 0.0
    accepted = bool(not policy_error)
    reason = policy_error
    second_of_day: int | None = None
    entry_blackout = False
    force_close = False
    if clock_valid:
        utc_now = datetime.fromtimestamp(now, tz=timezone.utc)
        second_of_day = (
            utc_now.hour * 60 * 60 + utc_now.minute * 60 + utc_now.second
        )

    return ProductionScalpRolloverDecision(
        accepted=accepted,
        reason=reason,
        as_of_epoch=now if clock_valid else None,
        utc_second_of_day=second_of_day,
        entry_blackout_active=bool(entry_blackout),
        force_close_active=bool(force_close),
        entry_blackout_start_second=policy.entry_blackout_start_second,
        forced_close_start_second=policy.forced_close_start_second,
        blackout_end_second=policy.blackout_end_second,
        policy_config_sha256=policy.config_sha256(),
    )


__all__ = [
    "DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY",
    "IG_FUNDING_BOUNDARY_EARLIEST_UTC_MINUTE",
    "IG_FUNDING_BOUNDARY_LATEST_UTC_MINUTE",
    "IG_FUNDING_BOUNDARY_LONDON_MINUTE",
    "PRODUCTION_SCALP_BROKER_CLOSE_SLACK_SECONDS",
    "PRODUCTION_SCALP_MAX_TIME_STOP_BARS",
    "PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION",
    "PRODUCTION_SCALP_TIME_STOP_BAR_SECONDS",
    "ProductionScalpRolloverDecision",
    "ProductionScalpRolloverPolicy",
    "evaluate_production_scalp_rollover_guard",
]
