"""Pure candidate universe for a future MTVCLC multiplicity experiment.

This module creates no signals, outcomes, performance statistics, files, or
authority.  It deterministically demonstrates one fixed MTVCLC policy and
4,697 causal, deliberately perturbed full-portfolio policies.  The current
universe is template-only because the d80e-bound capture lost continuity; it
must be regenerated and reviewed against a new primary preregistration before
any future seal.  The controls do not reconstruct, identify, or stand in for
any historical policy.
"""

from __future__ import annotations

# AGENT: ROLE: Pure template-only MTVCLC trial-universe definition.
# AGENT: PRIMARY OUTPUTS: deterministic policy mappings and content identities.
# AGENT ISOLATION: no I/O, outcomes, issuer, runtime, broker, or trading authority.

from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
import json
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "fxstack.scalp.mtvclc_auxiliary_trial_universe.v1"
MAPPING_SCHEMA_VERSION = "fxstack.scalp.mtvclc_auxiliary_mapping.v1"
TRIAL_SCHEMA_VERSION = "fxstack.scalp.mtvclc_auxiliary_trial.v1"

PRIMARY_PREREGISTRATION_BODY_SHA256 = (
    "d80e6cc9f05726ff2d2e851890ec06ca1b2df8e08efb4066bc83e31c216f17f3"
)
PRIMARY_STRATEGY_ID = "ig_mt4_tick_volume_close_location_continuation"
PRIMARY_STRATEGY_VERSION = "mtvclc.v1"
PRIMARY_CONFIG_ID = "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
PRIMARY_CONFIG_SHA256 = (
    "aac8e4bd98d71243b41993983d63ab1da1f8836b74b9243c7e60232f0eb00701"
)
PRIMARY_TRIAL_ID = "mtvclc_primary_fixed_v1"
TEMPLATE_ONLY = True
PUBLICATION_ELIGIBLE = False
REPLACEMENT_PRIMARY_PREREGISTRATION_REQUIRED = True
D80E_AUXILIARY_BINDING_ELIGIBLE = False
PROPOSED_AUXILIARY_START_UTC = "2026-09-02T13:30:00Z"
PROPOSED_AUXILIARY_END_UTC = "2027-01-30T13:30:00Z"

SYMBOLS: tuple[str, ...] = (
    "EURUSD",
    "USDJPY",
    "AUDUSD",
    "GBPUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
    "AUDJPY",
    "CADJPY",
    "CHFJPY",
    "EURAUD",
    "EURCAD",
    "EURCHF",
    "GBPCAD",
    "GBPCHF",
    "GBPJPY",
    "BTCUSD",
    "ETHUSD",
    "AUDCAD",
    "NZDJPY",
)
SIDE_TRANSFORMS: tuple[str, ...] = ("preserve", "invert")
MAXIMUM_SIGNAL_LAG_MINUTES = 120

# The original preregistration disclosed 4,698 cumulative attempted cells.
# This new universe conservatively uses that number as its number of complete
# portfolio trials.  It does not assert a one-to-one historical mapping.
DISCLOSED_ATTEMPTED_CELL_LOWER_BOUND = 4_698
TRIAL_COUNT = DISCLOSED_ATTEMPTED_CELL_LOWER_BOUND
NEGATIVE_CONTROL_COUNT = TRIAL_COUNT - 1

_MASTER_SEED_MATERIAL = (
    "fxstack.mtvclc.auxiliary.multiplicity.v1|"
    + PRIMARY_PREREGISTRATION_BODY_SHA256
    + "|withheld-proposed-window-"
    + PROPOSED_AUXILIARY_START_UTC
).encode("ascii")
MASTER_SEED_SHA256 = hashlib.sha256(_MASTER_SEED_MATERIAL).hexdigest()
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True, slots=True)
class TargetMapping:
    """Causal source-to-target rule for one target instrument."""

    target_symbol: str
    source_symbol: str
    signal_lag_minutes: int
    side_transform: str


@dataclass(frozen=True, slots=True)
class TrialDescriptor:
    """One immutable trial row in evaluator column order."""

    schema_version: str
    ordinal: int
    trial_id: str
    trial_kind: str
    derivation_seed_sha256: str
    mapping_sha256: str
    column_order_sha256: str
    column_index: int


def canonical_json_bytes(value: Any) -> bytes:
    """Return the one canonical JSON representation used by this universe."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "")
    return len(text) == 64 and set(text) <= _HEX


def _trial_seed(ordinal: int) -> bytes:
    if not 1 <= ordinal < TRIAL_COUNT:
        raise ValueError("negative-control ordinal outside frozen universe")
    return hashlib.sha256(
        bytes.fromhex(MASTER_SEED_SHA256)
        + b"|control|"
        + ordinal.to_bytes(8, "big", signed=False)
    ).digest()


def _uniform_index(*, seed: bytes, domain: bytes, upper_bound: int) -> int:
    """Draw an unbiased deterministic integer by SHA-256 rejection sampling."""

    if upper_bound < 1:
        raise ValueError("uniform upper bound must be positive")
    space = 1 << 64
    limit = space - (space % upper_bound)
    counter = 0
    while True:
        digest = hashlib.sha256(
            seed + b"|" + domain + b"|" + counter.to_bytes(4, "big")
        ).digest()
        candidate = int.from_bytes(digest[:8], "big", signed=False)
        if candidate < limit:
            return candidate % upper_bound
        counter += 1


def _control_mapping_for_target(
    *, ordinal: int, target_index: int
) -> TargetMapping:
    """Choose uniformly from every mapping except the exact primary mapping."""

    seed = _trial_seed(ordinal)
    target = SYMBOLS[target_index]
    full_choice_count = (
        len(SYMBOLS)
        * (MAXIMUM_SIGNAL_LAG_MINUTES + 1)
        * len(SIDE_TRANSFORMS)
    )
    identity_full_index = (
        target_index
        * (MAXIMUM_SIGNAL_LAG_MINUTES + 1)
        * len(SIDE_TRANSFORMS)
    )
    reduced_index = _uniform_index(
        seed=seed,
        domain=f"target:{target_index}:{target}".encode("ascii"),
        upper_bound=full_choice_count - 1,
    )
    full_index = (
        reduced_index
        if reduced_index < identity_full_index
        else reduced_index + 1
    )
    transform_index = full_index % len(SIDE_TRANSFORMS)
    quotient = full_index // len(SIDE_TRANSFORMS)
    lag = quotient % (MAXIMUM_SIGNAL_LAG_MINUTES + 1)
    source_index = quotient // (MAXIMUM_SIGNAL_LAG_MINUTES + 1)
    mapping = TargetMapping(
        target_symbol=target,
        source_symbol=SYMBOLS[source_index],
        signal_lag_minutes=lag,
        side_transform=SIDE_TRANSFORMS[transform_index],
    )
    if mapping == TargetMapping(target, target, 0, "preserve"):
        raise RuntimeError("control generator emitted exact primary mapping")
    return mapping


@lru_cache(maxsize=TRIAL_COUNT)
def mapping_for_ordinal(ordinal: int) -> tuple[TargetMapping, ...]:
    """Return the exact 22-target mapping for a frozen trial ordinal."""

    if isinstance(ordinal, bool) or not isinstance(ordinal, int):
        raise ValueError("trial ordinal must be an integer")
    if ordinal == 0:
        return tuple(
            TargetMapping(symbol, symbol, 0, "preserve") for symbol in SYMBOLS
        )
    if not 1 <= ordinal < TRIAL_COUNT:
        raise ValueError("trial ordinal outside frozen universe")
    return tuple(
        _control_mapping_for_target(ordinal=ordinal, target_index=index)
        for index in range(len(SYMBOLS))
    )


def mapping_payload(ordinal: int) -> dict[str, Any]:
    return {
        "schema_version": MAPPING_SCHEMA_VERSION,
        "strategy_id": PRIMARY_STRATEGY_ID,
        "strategy_version": PRIMARY_STRATEGY_VERSION,
        "primary_config_sha256": PRIMARY_CONFIG_SHA256,
        "ordinal": ordinal,
        "targets": [asdict(row) for row in mapping_for_ordinal(ordinal)],
    }


def mapping_sha256(ordinal: int) -> str:
    return canonical_sha256(mapping_payload(ordinal))


def _unordered_trial_row(ordinal: int) -> dict[str, Any]:
    digest = mapping_sha256(ordinal)
    if ordinal == 0:
        trial_id = PRIMARY_TRIAL_ID
        kind = "fixed_primary"
        seed_sha = hashlib.sha256(
            b"fixed-primary|" + bytes.fromhex(PRIMARY_CONFIG_SHA256)
        ).hexdigest()
    else:
        trial_id = f"mtvclc_aux_negative_control_{ordinal:04d}_{digest[:12]}"
        kind = "prospective_negative_control"
        seed_sha = _trial_seed(ordinal).hex()
    order_sha = hashlib.sha256(
        (
            "fxstack.mtvclc.auxiliary.column.v1|"
            + trial_id
            + "|"
            + digest
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema_version": TRIAL_SCHEMA_VERSION,
        "ordinal": ordinal,
        "trial_id": trial_id,
        "trial_kind": kind,
        "derivation_seed_sha256": seed_sha,
        "mapping_sha256": digest,
        "column_order_sha256": order_sha,
    }


@lru_cache(maxsize=1)
def trial_descriptors() -> tuple[TrialDescriptor, ...]:
    """Return every template trial in neutral, hash-derived column order."""

    unordered = [_unordered_trial_row(ordinal) for ordinal in range(TRIAL_COUNT)]
    mapping_hashes = [str(row["mapping_sha256"]) for row in unordered]
    trial_ids = [str(row["trial_id"]) for row in unordered]
    order_hashes = [str(row["column_order_sha256"]) for row in unordered]
    if (
        len(set(mapping_hashes)) != TRIAL_COUNT
        or len(set(trial_ids)) != TRIAL_COUNT
        or len(set(order_hashes)) != TRIAL_COUNT
    ):
        raise RuntimeError("auxiliary trial universe is not unique")
    ordered = sorted(unordered, key=lambda row: str(row["column_order_sha256"]))
    return tuple(
        TrialDescriptor(**row, column_index=index)
        for index, row in enumerate(ordered)
    )


def trial_manifest_payload() -> dict[str, Any]:
    rows = [asdict(row) for row in trial_descriptors()]
    primary_index = next(
        row["column_index"]
        for row in rows
        if row["trial_id"] == PRIMARY_TRIAL_ID
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "template_only": TEMPLATE_ONLY,
        "publication_eligible": PUBLICATION_ELIGIBLE,
        "replacement_primary_preregistration_required": (
            REPLACEMENT_PRIMARY_PREREGISTRATION_REQUIRED
        ),
        "d80e_auxiliary_binding_eligible": D80E_AUXILIARY_BINDING_ELIGIBLE,
        "withheld_proposed_window": {
            "start_utc_inclusive": PROPOSED_AUXILIARY_START_UTC,
            "end_utc_exclusive": PROPOSED_AUXILIARY_END_UTC,
        },
        "primary_preregistration_body_sha256": (
            PRIMARY_PREREGISTRATION_BODY_SHA256
        ),
        "strategy_id": PRIMARY_STRATEGY_ID,
        "strategy_version": PRIMARY_STRATEGY_VERSION,
        "primary_config_id": PRIMARY_CONFIG_ID,
        "primary_config_sha256": PRIMARY_CONFIG_SHA256,
        "master_seed_sha256": MASTER_SEED_SHA256,
        "generator": {
            "method": (
                "sha256_rejection_sampled_per_target_source_lag_side_mapping.v1"
            ),
            "symbols": list(SYMBOLS),
            "side_transforms": list(SIDE_TRANSFORMS),
            "maximum_signal_lag_minutes": MAXIMUM_SIGNAL_LAG_MINUTES,
            "every_control_target_mapping_excludes_exact_primary": True,
            "controls_are_historical_reconstructions": False,
            "regenerate_after_replacement_primary_preregistration": True,
        },
        "trial_count": TRIAL_COUNT,
        "negative_control_count": NEGATIVE_CONTROL_COUNT,
        "primary_trial_id": PRIMARY_TRIAL_ID,
        "primary_column_index": primary_index,
        "trials": rows,
    }


def validate_trial_manifest(payload: Mapping[str, Any]) -> bool:
    """Validate a serialized universe against the pure template generator."""

    if not isinstance(payload, Mapping):
        return False
    expected = trial_manifest_payload()
    if dict(payload) != expected:
        return False
    rows = payload.get("trials")
    if not isinstance(rows, Sequence) or len(rows) != TRIAL_COUNT:
        return False
    for raw in rows:
        if not isinstance(raw, Mapping):
            return False
        if (
            not _is_sha256(raw.get("derivation_seed_sha256"))
            or not _is_sha256(raw.get("mapping_sha256"))
            or not _is_sha256(raw.get("column_order_sha256"))
        ):
            return False
    return True


__all__ = [
    "D80E_AUXILIARY_BINDING_ELIGIBLE",
    "DISCLOSED_ATTEMPTED_CELL_LOWER_BOUND",
    "MASTER_SEED_SHA256",
    "MAXIMUM_SIGNAL_LAG_MINUTES",
    "NEGATIVE_CONTROL_COUNT",
    "PRIMARY_CONFIG_ID",
    "PRIMARY_CONFIG_SHA256",
    "PRIMARY_PREREGISTRATION_BODY_SHA256",
    "PRIMARY_STRATEGY_ID",
    "PRIMARY_STRATEGY_VERSION",
    "PRIMARY_TRIAL_ID",
    "PROPOSED_AUXILIARY_END_UTC",
    "PROPOSED_AUXILIARY_START_UTC",
    "PUBLICATION_ELIGIBLE",
    "REPLACEMENT_PRIMARY_PREREGISTRATION_REQUIRED",
    "SIDE_TRANSFORMS",
    "SYMBOLS",
    "TRIAL_COUNT",
    "TEMPLATE_ONLY",
    "TargetMapping",
    "TrialDescriptor",
    "canonical_json_bytes",
    "canonical_sha256",
    "mapping_for_ordinal",
    "mapping_payload",
    "mapping_sha256",
    "trial_descriptors",
    "trial_manifest_payload",
    "validate_trial_manifest",
]
