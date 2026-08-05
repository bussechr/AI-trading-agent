"""Evaluate the sealed MTVCLC window on a physically isolated host.

This post-window tool has no network, production database, credential, broker,
runtime, registry, issuer, signing, activation, or order surface.  It first
proves that the sealed 180-day window is closed, then verifies the immutable
handoff and capture, materializes compact per-symbol streams on local scratch
disk, and executes the frozen MTVCLC screen semantics once.

The raw capture is never loaded as one in-memory universe.  Materialization is
one chronological pass over the hash-chained manifest; evaluation then reads
one symbol at a time.  Pre-T0 bars are retained only as the fixed 240-bar
baseline context.  No pre-T0 signal bar or quote is evaluated.

Publication is deliberately one-shot: every content-addressed target must be
absent.  Even a byte-identical prior artifact causes a fail-closed refusal, so
an operator must use a fresh output namespace for a distinct invocation.
"""

from __future__ import annotations

# AGENT: ROLE: physically isolated, future-only MTVCLC outcome evaluator.
# AGENT: HANDSHAKE: sealed prereg + verified handoff + immutable capture ->
# research ledgers.
# AGENT ISOLATION: local files only; all production and authority surfaces are absent.
# AGENT: SIDE EFFECTS: compact scratch files plus content-addressed read-only
# research output.

import argparse
from collections import deque
from contextlib import ExitStack
from dataclasses import asdict, dataclass, replace
import hashlib
import hmac
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import sys
import tempfile
import time
from types import ModuleType
from typing import Any, BinaryIO, Iterator, Mapping, Protocol, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
EVALUATOR_PATH = Path(__file__).resolve()
HANDOFF_PATH = REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff.py"
SCREEN_PATH = (
    REPO_ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation.py"
)
REPLACEMENT_SCREEN_PATH = SCREEN_PATH.with_name(
    "screen_mt4_tick_volume_close_location_continuation_replacement.py"
)
BASE_SCREEN_FILENAME = SCREEN_PATH.name
REPLACEMENT_SCREEN_FILENAME = REPLACEMENT_SCREEN_PATH.name
HANDOFF_MODULE_NAME = "fxstack_isolated_mtvclc_handoff_verifier"
SCREEN_MODULE_NAME = "fxstack_isolated_mtvclc_frozen_screen"
REPLACEMENT_SCREEN_MODULE_NAME = "fxstack_isolated_mtvclc_replacement_screen"
MAXIMUM_EXECUTABLE_SOURCE_BYTES = 4 * 1024 * 1024
ACTIVE_HOUR_JOURNAL_FILENAME = "active-hour.journal.sha256.jsonl"
DATA_WRITER_LOCK_FILENAME = "collector-data-writer.lock"


class EvaluationRefusal(RuntimeError):
    """Fail-closed refusal raised before any research report is published."""


@dataclass(frozen=True, slots=True)
class ExecutedSourceImage:
    """The immutable bytes that were both hashed and executed."""

    path: Path
    raw: bytes
    sha256: str
    size_bytes: int

    def identity(self) -> dict[str, Any]:
        return {
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def _source_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _source_path_is_reparse(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return True
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & marker)


def _read_executed_source(path: Path) -> ExecutedSourceImage:
    """Read one stable regular source image without a path/hash race."""

    candidate = path.absolute()
    if candidate.is_symlink() or _source_path_is_reparse(candidate):
        raise EvaluationRefusal(f"executable_source_path_invalid:{candidate.name}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise EvaluationRefusal(
            f"executable_source_unreadable:{candidate.name}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise EvaluationRefusal(f"executable_source_not_regular:{candidate.name}")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(MAXIMUM_EXECUTABLE_SOURCE_BYTES + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        not raw
        or len(raw) > MAXIMUM_EXECUTABLE_SOURCE_BYTES
        or len(raw) != before.st_size
        or _source_stat_identity(before) != _source_stat_identity(after)
    ):
        raise EvaluationRefusal(f"executable_source_changed:{candidate.name}")
    return ExecutedSourceImage(
        path=candidate,
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        size_bytes=len(raw),
    )


def _execute_source_image(
    image: ExecutedSourceImage,
    *,
    module_name: str,
) -> ModuleType:
    """Compile and execute exactly the bytes represented by ``image``."""

    module = ModuleType(module_name)
    module.__file__ = str(image.path)
    module.__package__ = ""
    module.__dict__["__fxstack_exact_source_path__"] = image.path
    module.__dict__["__fxstack_exact_source_raw__"] = image.raw
    missing = object()
    previous = sys.modules.get(module_name, missing)
    sys.modules[module_name] = module
    try:
        code = compile(
            image.raw,
            str(image.path),
            "exec",
            dont_inherit=True,
        )
        exec(code, module.__dict__)
    except Exception as exc:
        raise EvaluationRefusal(
            f"executable_source_import_failed:{image.path.name}"
        ) from exc
    finally:
        if previous is missing:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous  # type: ignore[assignment]
    return module


def _self_executed_source_image() -> ExecutedSourceImage:
    bound_path = globals().get("__fxstack_exact_source_path__")
    bound_raw = globals().get("__fxstack_exact_source_raw__")
    if (
        isinstance(bound_path, Path)
        and bound_path == EVALUATOR_PATH
        and isinstance(bound_raw, bytes)
        and bound_raw
        and len(bound_raw) <= MAXIMUM_EXECUTABLE_SOURCE_BYTES
    ):
        return ExecutedSourceImage(
            path=EVALUATOR_PATH,
            raw=bound_raw,
            sha256=hashlib.sha256(bound_raw).hexdigest(),
            size_bytes=len(bound_raw),
        )
    return _read_executed_source(EVALUATOR_PATH)


EVALUATOR_SOURCE_IMAGE = _self_executed_source_image()
HANDOFF_SOURCE_IMAGE = _read_executed_source(HANDOFF_PATH)
handoff = _execute_source_image(
    HANDOFF_SOURCE_IMAGE,
    module_name=HANDOFF_MODULE_NAME,
)
BASE_SCREEN_SOURCE_IMAGE = _read_executed_source(SCREEN_PATH)
screen = _execute_source_image(
    BASE_SCREEN_SOURCE_IMAGE,
    module_name=SCREEN_MODULE_NAME,
)

EVALUATOR_SCHEMA = "fxstack.scalp.mtvclc_post_window_evaluator.v1"
REPORT_SCHEMA = "fxstack.scalp.mtvclc_post_window_report.v2"
LEDGER_ROW_SCHEMA = "fxstack.scalp.mtvclc_research_ledger_row.v1"
MATERIALIZATION_SCHEMA = "fxstack.scalp.mtvclc_binary_materialization.v1"
GLOBAL_MINIMUM_TRADES = 300
GLOBAL_MINIMUM_INDEPENDENT_DAYS = 60
BAR_RECORD = struct.Struct("!qddddq")
QUOTE_RECORD = struct.Struct("!qqddq32s")
MAXIMUM_HANDOFF_BYTES = 4 * 1024 * 1024
MATERIALIZATION_HEADROOM_BYTES = 512 * 1024 * 1024

EXPECTED_COST_POLICY = {
    "formula": (
        "net_bps=gross_quote_bps-(p90_ig_spread_bps+"
        "commission_bps_per_round_trip+financing_bps_per_trade+"
        "1.0bps_adverse_execution_debit)-"
        "conversion_rate*abs(gross_quote_bps)_when_profit_loss_currency_"
        "differs_from_account_currency"
    ),
    "conversion_treatment": (
        "debit_the_attested_rate_on_absolute_profit_or_loss_for_both_"
        "wins_and_losses;never_credit_conversion;zero_only_when_the_"
        "profit_loss_currency_equals_the_attested_account_currency"
    ),
    "geometry_uses_pre_conversion_cost": True,
    "final_cell_mean_uses_conversion_adjusted_net": True,
    "unknown_commission_financing_or_conversion_refuses_evaluation": True,
    "fee_schedule_change_or_source_uncertainty_refuses_evaluation": True,
}
COMMISSION_STATUSES = frozenset(
    {"explicit_source_attested", "conservative_upper_bound"}
)
FINANCING_STATUSES = frozenset(
    {
        "structurally_avoided_by_fixed_rollover_guard",
        "conservative_upper_bound",
    }
)
CONVERSION_STATUS = "debit_absolute_profit_or_loss_when_account_currency_differs"

FALSE_AUTHORITY: dict[str, bool] = dict(handoff.FALSE_AUTHORITY)

HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "strategy_id",
        "strategy_version",
        "config_id",
        "venue_id",
        "scope_version",
        "symbol_scope",
        "source_contract_id",
        "activity_metric_id",
        "capture_inventory",
        "capture_inventory_sha256",
        "window_closed",
        "manifest_and_chunks_verified",
        "outcome_evaluation_performed",
        "performance_statistics_computed",
        "research_only",
        "authority",
        "handoff_body_sha256",
    }
)

BASE_INVENTORY_FIELDS = frozenset(
    {
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "manifest_sha256",
        "manifest_head_sha256",
        "manifest_entries",
        "market_source_id",
        "segment_count",
        "bar_rows",
        "quote_rows",
        "bar_rows_by_symbol",
        "quote_rows_by_symbol",
        "first_bar_epoch_by_symbol",
        "first_quote_epoch_by_symbol",
        "last_quote_epoch_by_symbol",
        "last_bar_epoch_by_symbol",
        "maximum_transport_gap_seconds_by_symbol",
        "transport_gap_count_over_five_seconds_by_symbol",
        "referenced_chunk_files",
        "orphan_chunk_files",
    }
)
REPLACEMENT_INVENTORY_FIELDS = BASE_INVENTORY_FIELDS | {"guard_identity_sha256"}
# Compatibility name for tests/callers that refer to the original profile.
INVENTORY_FIELDS = BASE_INVENTORY_FIELDS

COST_POLICY_FIELDS = frozenset(
    {
        "formula",
        "conversion_treatment",
        "geometry_uses_pre_conversion_cost",
        "final_cell_mean_uses_conversion_adjusted_net",
        "unknown_commission_financing_or_conversion_refuses_evaluation",
        "fee_schedule_change_or_source_uncertainty_refuses_evaluation",
        "symbols",
    }
)

COST_ROW_FIELDS = frozenset(
    {
        "p90_ig_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "fixed_adverse_execution_debit_bps",
        "pre_conversion_geometry_cost_bps",
        "profit_loss_currency",
        "account_currency",
        "conversion_rate_of_absolute_profit_or_loss",
        "convert_on_close_charge_fraction_for_screen",
        "conversion_applies",
        "conversion_adjusted_break_even_win_probability",
        "commission_status",
        "financing_status",
        "conversion_status",
    }
)

COST_CAPTURE_FIELDS = frozenset(
    {
        "capture_json",
        "capture_npz",
        "capture_payload_sha256",
        "capture_mode",
        "scope_version",
        "venue_id",
    }
)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError) as exc:
        raise EvaluationRefusal("noncanonical_json_value") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any, reason: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EvaluationRefusal(reason)
    result = float(value)
    if not math.isfinite(result):
        raise EvaluationRefusal(reason)
    return result


def _nonnegative(value: Any, reason: str) -> float:
    result = _finite(value, reason)
    if result < 0.0:
        raise EvaluationRefusal(reason)
    return result


def _positive(value: Any, reason: str) -> float:
    result = _finite(value, reason)
    if result <= 0.0:
        raise EvaluationRefusal(reason)
    return result


def _strict_int(value: Any, reason: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise EvaluationRefusal(reason)
    return int(value)


def _read_object(
    path: str | Path,
    *,
    reason: str,
    maximum_bytes: int,
) -> tuple[Path, bytes, dict[str, Any]]:
    try:
        target, raw = handoff._read_regular_file(
            path,
            reason=reason,
            maximum_bytes=maximum_bytes,
        )
        payload = handoff._strict_json_object(
            raw,
            reason=reason,
        )
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal(reason) from exc
    return target, raw, payload


def _identity_matches_source_image(
    value: Any,
    image: ExecutedSourceImage,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "filename",
        "sha256",
        "size_bytes",
    }:
        return False
    return bool(
        value.get("filename") == image.path.name
        and value.get("size_bytes") == image.size_bytes
        and _is_sha256(value.get("sha256"))
        and hmac.compare_digest(
            str(value.get("sha256")).lower(),
            image.sha256,
        )
    )


def _execute_replacement_screen(image: ExecutedSourceImage) -> ModuleType:
    """Execute the wrapper against the already byte-bound base screen."""

    module_names = (
        "fxstack",
        "fxstack.scalp",
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation",
    )
    missing = object()
    previous: dict[str, ModuleType | object] = {
        name: sys.modules.get(name, missing) for name in module_names
    }
    fxstack_package = ModuleType("fxstack")
    scalp_package = ModuleType("fxstack.scalp")
    setattr(fxstack_package, "__path__", [])
    setattr(scalp_package, "__path__", [])
    setattr(fxstack_package, "scalp", scalp_package)
    setattr(
        scalp_package,
        "screen_mt4_tick_volume_close_location_continuation",
        screen,
    )
    sys.modules[module_names[0]] = fxstack_package
    sys.modules[module_names[1]] = scalp_package
    sys.modules[module_names[2]] = screen
    try:
        return _execute_source_image(
            image,
            module_name=REPLACEMENT_SCREEN_MODULE_NAME,
        )
    finally:
        for name in module_names:
            prior = previous[name]
            if prior is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior  # type: ignore[assignment]


def _select_executed_screen(
    preregistration: Mapping[str, Any],
) -> tuple[ModuleType, ExecutedSourceImage, ExecutedSourceImage]:
    identities = preregistration.get("source_identities")
    strategy = preregistration.get("strategy")
    accounting = preregistration.get("attempt_accounting")
    if (
        not isinstance(identities, Mapping)
        or not isinstance(strategy, Mapping)
        or not isinstance(accounting, Mapping)
    ):
        raise EvaluationRefusal("sealed_screen_contract_invalid")
    screen_identity = identities.get("screen_source")
    if not isinstance(screen_identity, Mapping):
        raise EvaluationRefusal("frozen_screen_identity_mismatch")
    filename = str(screen_identity.get("filename") or "")
    if filename == BASE_SCREEN_FILENAME:
        selected = screen
        selected_image = BASE_SCREEN_SOURCE_IMAGE
        support_image = BASE_SCREEN_SOURCE_IMAGE
    elif filename == REPLACEMENT_SCREEN_FILENAME:
        selected_image = _read_executed_source(REPLACEMENT_SCREEN_PATH)
        if not _identity_matches_source_image(screen_identity, selected_image):
            raise EvaluationRefusal("frozen_screen_identity_mismatch")
        support_identity = identities.get("screen_support_source")
        if not _identity_matches_source_image(
            support_identity,
            BASE_SCREEN_SOURCE_IMAGE,
        ):
            raise EvaluationRefusal("frozen_screen_support_identity_mismatch")
        selected = _execute_replacement_screen(selected_image)
        support_image = BASE_SCREEN_SOURCE_IMAGE
    else:
        raise EvaluationRefusal("frozen_screen_filename_invalid")
    if not _identity_matches_source_image(screen_identity, selected_image):
        raise EvaluationRefusal("frozen_screen_identity_mismatch")

    manifest = selected.attempt_manifest()
    manifest_sha = str(strategy.get("attempt_manifest_sha256") or "").lower()
    prior_attempts = _strict_int(
        getattr(selected, "IMMUTABLE_PRIOR_ATTEMPTED_CELLS", None),
        "sealed_screen_attempt_lineage_mismatch",
    )
    current_attempts = _strict_int(
        getattr(selected, "IMMUTABLE_CURRENT_ATTEMPTED_CELLS", None),
        "sealed_screen_attempt_lineage_mismatch",
        minimum=1,
    )
    cumulative_attempts = _strict_int(
        getattr(selected, "IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS", None),
        "sealed_screen_attempt_lineage_mismatch",
        minimum=1,
    )
    expected_accounting = {
        "prior_attempted_cells_lower_bound": prior_attempts,
        "current_attempted_cells": current_attempts,
        "cumulative_attempted_cells_lower_bound": cumulative_attempts,
    }
    if (
        strategy.get("attempt_manifest") != manifest
        or not _is_sha256(manifest_sha)
        or not hmac.compare_digest(manifest_sha, canonical_sha256(manifest))
        or dict(accounting) != expected_accounting
        or current_attempts != 44
        or cumulative_attempts != prior_attempts + current_attempts
    ):
        raise EvaluationRefusal("sealed_screen_attempt_lineage_mismatch")
    return selected, selected_image, support_image


def _sealed_file_identity_valid(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "filename",
        "sha256",
        "size_bytes",
    }:
        return False
    filename = str(value.get("filename") or "")
    size = value.get("size_bytes")
    return bool(
        filename
        and Path(filename).name == filename
        and _is_sha256(value.get("sha256"))
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
    )


class ProspectiveBinding(Protocol):
    preregistration_path: Path
    preregistration_body_sha256: str
    preregistration_artifact_sha256: str
    t0_utc: str
    end_utc_exclusive: str
    t0_epoch: float
    end_epoch_exclusive: float

    @property
    def tuple(self) -> tuple[str, str, str, str]: ...


@dataclass(frozen=True, slots=True)
class SealedEvaluationInputs:
    binding: ProspectiveBinding
    preregistration: dict[str, Any]
    handoff_payload: dict[str, Any]
    handoff_artifact_sha256: str
    costs_by_symbol: dict[str, Any]
    cost_source_sha256: str
    screen_module: ModuleType
    screen_source_filename: str
    screen_source_sha256: str
    screen_support_source_sha256: str
    handoff_source_sha256: str
    guard_identity_sha256: str | None


def _load_costs(
    preregistration: Mapping[str, Any],
    *,
    selected_screen: ModuleType = screen,
) -> tuple[dict[str, Any], str]:
    policy = preregistration.get("cost_policy")
    identities = preregistration.get("source_identities")
    if (
        not isinstance(policy, Mapping)
        or set(policy) != COST_POLICY_FIELDS
        or not isinstance(identities, Mapping)
        or policy.get("formula") != EXPECTED_COST_POLICY["formula"]
        or policy.get("conversion_treatment")
        != EXPECTED_COST_POLICY["conversion_treatment"]
        or any(
            policy.get(field) is not True
            for field in (
                "geometry_uses_pre_conversion_cost",
                "final_cell_mean_uses_conversion_adjusted_net",
                "unknown_commission_financing_or_conversion_refuses_evaluation",
                "fee_schedule_change_or_source_uncertainty_refuses_evaluation",
            )
        )
    ):
        raise EvaluationRefusal("sealed_cost_policy_invalid")
    raw_symbols = policy.get("symbols")
    cost_capture = identities.get("cost_capture")
    fee_attestation = identities.get("fee_attestation")
    attested_account_currency = (
        str(fee_attestation.get("account_currency") or "")
        if isinstance(fee_attestation, Mapping)
        else ""
    )
    if (
        not isinstance(raw_symbols, Mapping)
        or set(raw_symbols) != set(handoff.SYMBOLS)
        or not isinstance(cost_capture, Mapping)
        or set(cost_capture) != COST_CAPTURE_FIELDS
        or not _sealed_file_identity_valid(cost_capture.get("capture_json"))
        or not _sealed_file_identity_valid(cost_capture.get("capture_npz"))
        or not _is_sha256(cost_capture.get("capture_payload_sha256"))
        or cost_capture.get("capture_mode")
        not in {"live_endpoint", "authenticated_same_source_db_history"}
        or cost_capture.get("scope_version") != handoff.SCOPE_VERSION
        or cost_capture.get("venue_id") != handoff.VENUE_ID
        or len(attested_account_currency) != 3
        or not attested_account_currency.isalpha()
        or not attested_account_currency.isupper()
    ):
        raise EvaluationRefusal("sealed_cost_policy_invalid")
    capture_json = cost_capture.get("capture_json")
    if not isinstance(capture_json, Mapping):
        raise EvaluationRefusal("sealed_cost_source_invalid")
    cost_source_sha256 = str(capture_json.get("sha256") or "").lower()
    if not _is_sha256(cost_source_sha256):
        raise EvaluationRefusal("sealed_cost_source_invalid")

    costs: dict[str, Any] = {}
    for symbol in handoff.SYMBOLS:
        row = raw_symbols.get(symbol)
        if not isinstance(row, Mapping) or set(row) != COST_ROW_FIELDS:
            raise EvaluationRefusal(f"sealed_cost_row_invalid:{symbol}")
        conversion_applies = row.get("conversion_applies")
        if not isinstance(conversion_applies, bool):
            raise EvaluationRefusal(f"sealed_cost_row_invalid:{symbol}")
        p90 = _positive(
            row.get("p90_ig_spread_bps"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        commission = _nonnegative(
            row.get("commission_bps_per_round_trip"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        financing = _nonnegative(
            row.get("financing_bps_per_trade"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        adverse = _nonnegative(
            row.get("fixed_adverse_execution_debit_bps"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        conversion_rate = _nonnegative(
            row.get("conversion_rate_of_absolute_profit_or_loss"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        screen_conversion = _nonnegative(
            row.get("convert_on_close_charge_fraction_for_screen"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        account_currency = str(row.get("account_currency") or "")
        pnl_currency = str(row.get("profit_loss_currency") or "")
        expected_conversion = conversion_rate if conversion_applies else 0.0
        commission_status = row.get("commission_status")
        financing_status = row.get("financing_status")
        if (
            adverse != selected_screen.FIXED_ADVERSE_EXECUTION_DEBIT_BPS
            or conversion_rate
            != selected_screen.IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
            or pnl_currency != symbol[3:]
            or account_currency != attested_account_currency
            or conversion_applies != (account_currency != pnl_currency)
            or commission_status not in COMMISSION_STATUSES
            or financing_status not in FINANCING_STATUSES
            or (
                financing == 0.0
                and financing_status != "structurally_avoided_by_fixed_rollover_guard"
            )
            or row.get("conversion_status") != CONVERSION_STATUS
            or not math.isclose(
                screen_conversion,
                expected_conversion,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        ):
            raise EvaluationRefusal(f"sealed_cost_row_invalid:{symbol}")
        calibration = selected_screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=p90,
            commission_bps_per_round_trip=commission,
            financing_bps_per_trade=financing,
            account_currency=account_currency,
            pnl_currency=pnl_currency,
            convert_on_close_charge_fraction=screen_conversion,
            source_sha256=cost_source_sha256,
            adverse_execution_debit_bps=adverse,
        )
        pre_conversion = _positive(
            row.get("pre_conversion_geometry_cost_bps"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        sealed_break_even = _positive(
            row.get("conversion_adjusted_break_even_win_probability"),
            f"sealed_cost_row_invalid:{symbol}",
        )
        if (
            not selected_screen.validate_cost_calibration(
                calibration,
                expected_symbol=symbol,
            )
            or not math.isclose(
                pre_conversion,
                calibration.recorded_cost_bps,
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            or not math.isclose(
                sealed_break_even,
                calibration.break_even_win_probability,
                rel_tol=0.0,
                abs_tol=1e-15,
            )
        ):
            raise EvaluationRefusal(f"sealed_cost_screen_parity_failed:{symbol}")
        costs[symbol] = calibration
    return costs, cost_source_sha256


def _load_handoff(
    path: str | Path,
    *,
    binding: ProspectiveBinding,
    replacement_profile: bool = False,
) -> tuple[dict[str, Any], str]:
    target, raw, payload = _read_object(
        path,
        reason="capture_handoff_invalid",
        maximum_bytes=MAXIMUM_HANDOFF_BYTES,
    )
    if set(payload) != HANDOFF_FIELDS:
        raise EvaluationRefusal("capture_handoff_scope_invalid")
    body = dict(payload)
    claimed = str(body.pop("handoff_body_sha256", "")).lower()
    if (
        not _is_sha256(claimed)
        or not hmac.compare_digest(claimed, canonical_sha256(body))
        or target.name != f"mtvclc_capture_handoff_{claimed}.json"
        or raw != canonical_json_bytes(payload) + b"\n"
    ):
        raise EvaluationRefusal("capture_handoff_hash_invalid")
    inventory = payload.get("capture_inventory")
    expected_inventory_fields = (
        REPLACEMENT_INVENTORY_FIELDS if replacement_profile else BASE_INVENTORY_FIELDS
    )
    if (
        not isinstance(inventory, Mapping)
        or set(inventory) != expected_inventory_fields
    ):
        raise EvaluationRefusal("capture_handoff_inventory_invalid")
    guard_identity = inventory.get("guard_identity_sha256")
    if replacement_profile and (
        not isinstance(guard_identity, str)
        or guard_identity != guard_identity.strip().lower()
        or not _is_sha256(guard_identity)
    ):
        raise EvaluationRefusal("capture_handoff_guard_identity_invalid")
    symbol_maps = (
        "bar_rows_by_symbol",
        "quote_rows_by_symbol",
        "first_bar_epoch_by_symbol",
        "first_quote_epoch_by_symbol",
        "last_quote_epoch_by_symbol",
        "last_bar_epoch_by_symbol",
        "maximum_transport_gap_seconds_by_symbol",
        "transport_gap_count_over_five_seconds_by_symbol",
    )
    if any(
        not isinstance(inventory.get(field), Mapping)
        or set(inventory[field]) != set(handoff.SYMBOLS)
        for field in symbol_maps
    ):
        raise EvaluationRefusal("capture_handoff_inventory_invalid")
    manifest_entries = _strict_int(
        inventory.get("manifest_entries"),
        "capture_handoff_inventory_invalid",
        minimum=1,
    )
    segment_count = _strict_int(
        inventory.get("segment_count"),
        "capture_handoff_inventory_invalid",
        minimum=1,
    )
    referenced_chunks = _strict_int(
        inventory.get("referenced_chunk_files"),
        "capture_handoff_inventory_invalid",
        minimum=1,
    )
    orphan_chunks = _strict_int(
        inventory.get("orphan_chunk_files"),
        "capture_handoff_inventory_invalid",
    )
    if (
        segment_count != 1
        or orphan_chunks != 0
        or referenced_chunks != manifest_entries
        or not _is_sha256(inventory.get("manifest_sha256"))
        or not _is_sha256(inventory.get("manifest_head_sha256"))
        or not _is_sha256(inventory.get("market_source_id"))
    ):
        raise EvaluationRefusal("capture_handoff_inventory_invalid")
    inventory_sha = str(payload.get("capture_inventory_sha256") or "").lower()
    if (
        payload.get("schema_version") != handoff.HANDOFF_SCHEMA
        or payload.get("strategy_id") != handoff.STRATEGY_ID
        or payload.get("strategy_version") != handoff.STRATEGY_VERSION
        or payload.get("config_id") != handoff.CONFIG_ID
        or payload.get("venue_id") != handoff.VENUE_ID
        or payload.get("scope_version") != handoff.SCOPE_VERSION
        or payload.get("symbol_scope") != list(handoff.SYMBOLS)
        or payload.get("source_contract_id") != handoff.SOURCE_CONTRACT_ID
        or payload.get("activity_metric_id") != handoff.ACTIVITY_METRIC_ID
        or payload.get("window_closed") is not True
        or payload.get("manifest_and_chunks_verified") is not True
        or payload.get("outcome_evaluation_performed") is not False
        or payload.get("performance_statistics_computed") is not False
        or payload.get("research_only") is not True
        or payload.get("authority") != FALSE_AUTHORITY
        or not _is_sha256(inventory_sha)
        or not hmac.compare_digest(inventory_sha, canonical_sha256(inventory))
        or str(inventory.get("preregistration_body_sha256") or "").lower()
        != binding.preregistration_body_sha256
        or str(inventory.get("preregistration_artifact_sha256") or "").lower()
        != binding.preregistration_artifact_sha256
        or inventory.get("prospective_t0_utc_inclusive") != binding.t0_utc
        or inventory.get("prospective_end_utc_exclusive") != binding.end_utc_exclusive
    ):
        raise EvaluationRefusal("capture_handoff_contract_invalid")
    return payload, hashlib.sha256(raw).hexdigest()


def load_sealed_inputs(
    *,
    preregistration_path: str | Path,
    handoff_path: str | Path,
    now_epoch: float,
) -> SealedEvaluationInputs:
    """Load seals only; the capture path is intentionally not an argument."""

    try:
        binding = handoff.load_preregistration(preregistration_path)
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal("preregistration_invalid") from exc
    now = _finite(now_epoch, "evaluation_clock_invalid")
    if now < binding.end_epoch_exclusive:
        raise EvaluationRefusal("prospective_window_not_closed")
    prereg_path, prereg_raw, preregistration = _read_object(
        preregistration_path,
        reason="preregistration_invalid",
        maximum_bytes=handoff.MAXIMUM_PREREGISTRATION_BYTES,
    )
    if (
        prereg_path != binding.preregistration_path
        or hashlib.sha256(prereg_raw).hexdigest()
        != binding.preregistration_artifact_sha256
    ):
        raise EvaluationRefusal("preregistration_identity_changed")
    selected_screen, screen_image, screen_support_image = _select_executed_screen(
        preregistration
    )
    costs, cost_source_sha = _load_costs(
        preregistration,
        selected_screen=selected_screen,
    )
    handoff_payload, handoff_artifact_sha = _load_handoff(
        handoff_path,
        binding=binding,
        replacement_profile=(screen_image.path.name == REPLACEMENT_SCREEN_FILENAME),
    )
    return SealedEvaluationInputs(
        binding=binding,
        preregistration=preregistration,
        handoff_payload=handoff_payload,
        handoff_artifact_sha256=handoff_artifact_sha,
        costs_by_symbol=costs,
        cost_source_sha256=cost_source_sha,
        screen_module=selected_screen,
        screen_source_filename=screen_image.path.name,
        screen_source_sha256=screen_image.sha256,
        screen_support_source_sha256=screen_support_image.sha256,
        handoff_source_sha256=HANDOFF_SOURCE_IMAGE.sha256,
        guard_identity_sha256=(
            str(handoff_payload["capture_inventory"].get("guard_identity_sha256"))
            if screen_image.path.name == REPLACEMENT_SCREEN_FILENAME
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class MaterializedCapture:
    root: Path
    bar_paths: dict[str, Path]
    quote_paths: dict[str, Path]
    bar_counts: dict[str, int]
    quote_counts: dict[str, int]
    source_sha256_by_symbol: dict[str, str]
    manifest_sha256: str
    manifest_head_sha256: str
    manifest_entries: int
    projected_size_bytes: int
    snapshot_files_verified_read_only: int
    snapshot_directories_verified_read_only: int


@dataclass(frozen=True, slots=True)
class SnapshotFileIdentity:
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    changed_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CaptureSnapshotFence:
    root: Path
    manifest_path: Path
    manifest_identity: SnapshotFileIdentity
    handoff_body_sha256: str
    capture_inventory_sha256: str
    manifest_head_sha256: str
    manifest_entries: int
    require_data_writer_lock: bool
    guard_identity_sha256: str | None


WRITE_PERMISSION_BITS = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH


def _snapshot_stat_identity(
    value: os.stat_result,
    *,
    sha256: str,
) -> SnapshotFileIdentity:
    return SnapshotFileIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        size_bytes=int(value.st_size),
        modified_ns=int(value.st_mtime_ns),
        changed_ns=int(value.st_ctime_ns),
        sha256=sha256,
    )


def _require_read_only_directory(path: Path, *, reason: str) -> os.stat_result:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvaluationRefusal(reason) from exc
    if (
        not stat.S_ISDIR(value.st_mode)
        or path.is_symlink()
        or handoff._is_reparse_point(path)
        or value.st_mode & WRITE_PERMISSION_BITS
    ):
        raise EvaluationRefusal(reason)
    return value


def _require_read_only_file(path: Path, *, reason: str) -> os.stat_result:
    try:
        value = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise EvaluationRefusal(reason) from exc
    if (
        not stat.S_ISREG(value.st_mode)
        or path.is_symlink()
        or handoff._is_reparse_point(path)
        or value.st_mode & WRITE_PERMISSION_BITS
    ):
        raise EvaluationRefusal(reason)
    return value


def _stream_sha256(path: Path, *, reason: str) -> tuple[str, os.stat_result]:
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            digest = hashlib.sha256()
            while block := handle.read(8 * 1024 * 1024):
                digest.update(block)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise EvaluationRefusal(reason) from exc
    if _source_stat_identity(before) != _source_stat_identity(after):
        raise EvaluationRefusal(reason)
    return digest.hexdigest(), after


def _assert_capture_writer_stopped(
    capture: Path,
    *,
    require_data_writer_lock: bool,
) -> None:
    active_journal = capture / ACTIVE_HOUR_JOURNAL_FILENAME
    if active_journal.exists() or active_journal.is_symlink():
        raise EvaluationRefusal("capture_snapshot_active_hour_not_finalized")

    data_writer_lock = capture / DATA_WRITER_LOCK_FILENAME
    if require_data_writer_lock and not data_writer_lock.is_file():
        raise EvaluationRefusal("capture_snapshot_data_writer_lock_missing")
    if data_writer_lock.exists() or data_writer_lock.is_symlink():
        _require_read_only_file(
            data_writer_lock,
            reason="capture_snapshot_data_writer_lock_invalid",
        )
        try:
            with data_writer_lock.open("rb") as handle:
                if os.fstat(handle.fileno()).st_size < 1:
                    raise EvaluationRefusal("capture_snapshot_data_writer_lock_invalid")
                handle.seek(0)
                if os.name == "nt":
                    lock_api = __import__("msvcrt")
                    locking = getattr(lock_api, "locking")
                    locking(
                        handle.fileno(),
                        int(getattr(lock_api, "LK_NBLCK")),
                        1,
                    )
                    handle.seek(0)
                    locking(
                        handle.fileno(),
                        int(getattr(lock_api, "LK_UNLCK")),
                        1,
                    )
                else:
                    lock_api = __import__("fcntl")
                    flock = getattr(lock_api, "flock")
                    flock(
                        handle.fileno(),
                        int(getattr(lock_api, "LOCK_EX"))
                        | int(getattr(lock_api, "LOCK_NB")),
                    )
                    flock(handle.fileno(), int(getattr(lock_api, "LOCK_UN")))
        except EvaluationRefusal:
            raise
        except (OSError, ImportError) as exc:
            raise EvaluationRefusal(
                "capture_snapshot_data_writer_still_active"
            ) from exc

    # Both guard generations hold their file with FileShare.None on Windows.
    # A copied snapshot may contain either file, but it must be openable now.
    for supervision_directory in ("supervision", "supervision-resilient"):
        writer_lock = capture / supervision_directory / "collector-writer.lock"
        if writer_lock.exists() or writer_lock.is_symlink():
            if writer_lock.is_symlink() or handoff._is_reparse_point(writer_lock):
                raise EvaluationRefusal("capture_snapshot_writer_lock_invalid")
            try:
                with writer_lock.open("rb") as handle:
                    handle.read(1)
            except OSError as exc:
                raise EvaluationRefusal("capture_snapshot_writer_still_active") from exc


def _require_stopped_read_only_capture_root(
    capture_root: str | Path,
    *,
    require_data_writer_lock: bool,
) -> Path:
    capture = _resolved_directory(capture_root, reason="capture_root_invalid")
    _require_read_only_directory(
        capture,
        reason="capture_snapshot_root_not_read_only",
    )
    chunks = capture / handoff.CHUNK_DIRECTORY
    _require_read_only_directory(
        chunks,
        reason="capture_snapshot_chunks_not_read_only",
    )
    _require_read_only_file(
        capture / handoff.MANIFEST_FILENAME,
        reason="capture_snapshot_manifest_not_read_only",
    )
    _assert_capture_writer_stopped(
        capture,
        require_data_writer_lock=require_data_writer_lock,
    )
    if require_data_writer_lock:
        _require_read_only_file(
            capture / handoff.GUARD_IDENTITY_FILENAME,
            reason="capture_snapshot_guard_identity_not_read_only",
        )
    return capture


def _build_capture_snapshot_fence(
    *,
    capture_root: Path,
    verified_handoff: Mapping[str, Any],
    require_data_writer_lock: bool = False,
) -> CaptureSnapshotFence:
    inventory = verified_handoff.get("capture_inventory")
    if not isinstance(inventory, Mapping):
        raise EvaluationRefusal("capture_snapshot_inventory_invalid")
    manifest_path = capture_root / handoff.MANIFEST_FILENAME
    manifest_stat = _require_read_only_file(
        manifest_path,
        reason="capture_snapshot_manifest_not_read_only",
    )
    expected_manifest_sha = str(inventory.get("manifest_sha256") or "").lower()
    if not _is_sha256(expected_manifest_sha):
        raise EvaluationRefusal("capture_snapshot_inventory_invalid")
    guard_identity_value = inventory.get("guard_identity_sha256")
    if require_data_writer_lock:
        if (
            not isinstance(guard_identity_value, str)
            or guard_identity_value != guard_identity_value.strip().lower()
            or not _is_sha256(guard_identity_value)
        ):
            raise EvaluationRefusal("capture_snapshot_guard_identity_invalid")
        guard_identity_sha256: str | None = guard_identity_value
        guard_digest, _guard_stat = _stream_sha256(
            capture_root / handoff.GUARD_IDENTITY_FILENAME,
            reason="capture_snapshot_guard_identity_changed",
        )
        if not hmac.compare_digest(guard_digest, guard_identity_value):
            raise EvaluationRefusal("capture_snapshot_guard_identity_changed")
    else:
        if guard_identity_value is not None:
            raise EvaluationRefusal("capture_snapshot_guard_identity_unexpected")
        guard_identity_sha256 = None
    return CaptureSnapshotFence(
        root=capture_root,
        manifest_path=manifest_path,
        manifest_identity=_snapshot_stat_identity(
            manifest_stat,
            sha256=expected_manifest_sha,
        ),
        handoff_body_sha256=str(
            verified_handoff.get("handoff_body_sha256") or ""
        ).lower(),
        capture_inventory_sha256=str(
            verified_handoff.get("capture_inventory_sha256") or ""
        ).lower(),
        manifest_head_sha256=str(inventory.get("manifest_head_sha256") or "").lower(),
        manifest_entries=_strict_int(
            inventory.get("manifest_entries"),
            "capture_snapshot_inventory_invalid",
            minimum=1,
        ),
        require_data_writer_lock=require_data_writer_lock,
        guard_identity_sha256=guard_identity_sha256,
    )


def _assert_capture_snapshot_fence(
    fence: CaptureSnapshotFence,
    *,
    rehash_manifest: bool,
) -> None:
    _require_read_only_directory(
        fence.root,
        reason="capture_snapshot_root_not_read_only",
    )
    _require_read_only_directory(
        fence.root / handoff.CHUNK_DIRECTORY,
        reason="capture_snapshot_chunks_not_read_only",
    )
    current_stat = _require_read_only_file(
        fence.manifest_path,
        reason="capture_snapshot_manifest_not_read_only",
    )
    current = _snapshot_stat_identity(
        current_stat,
        sha256=fence.manifest_identity.sha256,
    )
    if current != fence.manifest_identity:
        raise EvaluationRefusal("capture_snapshot_manifest_changed")
    _assert_capture_writer_stopped(
        fence.root,
        require_data_writer_lock=fence.require_data_writer_lock,
    )
    if fence.guard_identity_sha256 is not None:
        guard_path = fence.root / handoff.GUARD_IDENTITY_FILENAME
        _require_read_only_file(
            guard_path,
            reason="capture_snapshot_guard_identity_not_read_only",
        )
        guard_digest, _guard_stat = _stream_sha256(
            guard_path,
            reason="capture_snapshot_guard_identity_changed",
        )
        if not hmac.compare_digest(guard_digest, fence.guard_identity_sha256):
            raise EvaluationRefusal("capture_snapshot_guard_identity_changed")
    if rehash_manifest:
        digest, after = _stream_sha256(
            fence.manifest_path,
            reason="capture_snapshot_manifest_changed",
        )
        if (
            not hmac.compare_digest(digest, fence.manifest_identity.sha256)
            or _snapshot_stat_identity(after, sha256=digest) != fence.manifest_identity
        ):
            raise EvaluationRefusal("capture_snapshot_manifest_changed")


def _reverify_capture_snapshot(
    *,
    fence: CaptureSnapshotFence,
    sealed: SealedEvaluationInputs,
    preregistration_path: str | Path,
    now_epoch: float,
) -> None:
    """Re-authenticate the snapshot after projection and before outcomes."""

    _assert_capture_snapshot_fence(fence, rehash_manifest=False)
    try:
        verified = handoff.verify_capture_handoff(
            preregistration_path=preregistration_path,
            capture_root=fence.root,
            now_epoch=now_epoch,
        )
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal(
            "capture_snapshot_post_materialization_invalid"
        ) from exc
    inventory = verified.get("capture_inventory")
    if (
        not isinstance(inventory, Mapping)
        or canonical_json_bytes(verified)
        != canonical_json_bytes(sealed.handoff_payload)
        or str(verified.get("handoff_body_sha256") or "").lower()
        != fence.handoff_body_sha256
        or str(verified.get("capture_inventory_sha256") or "").lower()
        != fence.capture_inventory_sha256
        or str(inventory.get("manifest_sha256") or "").lower()
        != fence.manifest_identity.sha256
        or str(inventory.get("manifest_head_sha256") or "").lower()
        != fence.manifest_head_sha256
        or inventory.get("manifest_entries") != fence.manifest_entries
        or inventory.get("guard_identity_sha256") != fence.guard_identity_sha256
    ):
        raise EvaluationRefusal("capture_snapshot_post_materialization_changed")
    _assert_capture_snapshot_fence(fence, rehash_manifest=False)


def verified_handoff_proves_source_ready(
    *,
    sealed: SealedEvaluationInputs,
    materialized: MaterializedCapture,
) -> bool:
    """Re-establish the frozen screen's source-ready premise from the handoff."""

    selected_screen = sealed.screen_module
    inventory = sealed.handoff_payload.get("capture_inventory")
    if not isinstance(inventory, Mapping):
        raise EvaluationRefusal("verified_source_inventory_invalid")
    bar_counts = inventory.get("bar_rows_by_symbol")
    quote_counts = inventory.get("quote_rows_by_symbol")
    if (
        not isinstance(bar_counts, Mapping)
        or not isinstance(quote_counts, Mapping)
        or set(bar_counts) != set(handoff.SYMBOLS)
        or set(quote_counts) != set(handoff.SYMBOLS)
        or materialized.bar_counts != dict(bar_counts)
        or materialized.quote_counts != dict(quote_counts)
        or set(materialized.source_sha256_by_symbol) != set(handoff.SYMBOLS)
    ):
        raise EvaluationRefusal("verified_source_inventory_invalid")
    for symbol in handoff.SYMBOLS:
        if (
            _strict_int(
                bar_counts[symbol],
                "verified_source_inventory_invalid",
            )
            < selected_screen.BASELINE_M1_BARS + 1
            or _strict_int(
                quote_counts[symbol],
                "verified_source_inventory_invalid",
            )
            < 1
            or not _is_sha256(materialized.source_sha256_by_symbol[symbol])
        ):
            raise EvaluationRefusal(f"verified_source_coverage_invalid:{symbol}")
    return True


def _resolved_directory(path: str | Path, *, reason: str) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or handoff._is_reparse_point(candidate):
        raise EvaluationRefusal(reason)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise EvaluationRefusal(reason) from exc
    if (
        not resolved.is_dir()
        or resolved.is_symlink()
        or handoff._is_reparse_point(resolved)
    ):
        raise EvaluationRefusal(reason)
    return resolved


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left.is_relative_to(right) or right.is_relative_to(left)


def _materialization_prefix(
    *,
    sealed: SealedEvaluationInputs,
    symbol: str,
) -> bytes:
    inventory = sealed.handoff_payload["capture_inventory"]
    prefix = {
        "schema_version": MATERIALIZATION_SCHEMA,
        "symbol": symbol,
        "preregistration_body_sha256": (sealed.binding.preregistration_body_sha256),
        "preregistration_artifact_sha256": (
            sealed.binding.preregistration_artifact_sha256
        ),
        "handoff_body_sha256": sealed.handoff_payload["handoff_body_sha256"],
        "handoff_artifact_sha256": sealed.handoff_artifact_sha256,
        "capture_inventory_sha256": sealed.handoff_payload["capture_inventory_sha256"],
        "manifest_sha256": inventory["manifest_sha256"],
        "market_source_id": inventory["market_source_id"],
        "source_contract_id": handoff.SOURCE_CONTRACT_ID,
    }
    if sealed.guard_identity_sha256 is not None:
        prefix["guard_identity_sha256"] = sealed.guard_identity_sha256
    return canonical_json_bytes(prefix)


def materialize_verified_capture(
    *,
    sealed: SealedEvaluationInputs,
    capture_root: str | Path,
    staging_root: str | Path,
    snapshot_fence: CaptureSnapshotFence,
) -> MaterializedCapture:
    """Stream the immutable capture into compact per-symbol binary files."""

    capture = _resolved_directory(capture_root, reason="capture_root_invalid")
    if capture != snapshot_fence.root:
        raise EvaluationRefusal("capture_snapshot_root_changed")
    _assert_capture_snapshot_fence(snapshot_fence, rehash_manifest=False)
    staging_parent = _resolved_directory(
        staging_root,
        reason="staging_root_invalid",
    )
    if _paths_overlap(capture, staging_parent):
        raise EvaluationRefusal("capture_and_staging_roots_overlap")
    inventory = sealed.handoff_payload.get("capture_inventory")
    if not isinstance(inventory, Mapping):
        raise EvaluationRefusal("capture_inventory_invalid")
    projected_bytes = (
        _strict_int(inventory.get("bar_rows"), "capture_inventory_invalid")
        * BAR_RECORD.size
        + _strict_int(
            inventory.get("quote_rows"),
            "capture_inventory_invalid",
        )
        * QUOTE_RECORD.size
    )
    try:
        free_bytes = shutil.disk_usage(staging_parent).free
    except OSError as exc:
        raise EvaluationRefusal("staging_capacity_unknown") from exc
    required_bytes = projected_bytes + max(
        MATERIALIZATION_HEADROOM_BYTES,
        projected_bytes // 10,
    )
    if free_bytes < required_bytes:
        raise EvaluationRefusal("staging_capacity_insufficient")
    try:
        stage = Path(
            tempfile.mkdtemp(
                prefix=".mtvclc-materialization-",
                dir=str(staging_parent),
            )
        ).resolve(strict=True)
    except OSError as exc:
        raise EvaluationRefusal("staging_directory_create_failed") from exc

    bar_paths = {symbol: stage / f"{symbol}.bars.bin" for symbol in handoff.SYMBOLS}
    quote_paths = {symbol: stage / f"{symbol}.quotes.bin" for symbol in handoff.SYMBOLS}
    bar_counts = {symbol: 0 for symbol in handoff.SYMBOLS}
    quote_counts = {symbol: 0 for symbol in handoff.SYMBOLS}
    source_digests = {
        symbol: hashlib.sha256(_materialization_prefix(sealed=sealed, symbol=symbol))
        for symbol in handoff.SYMBOLS
    }
    manifest_digest = hashlib.sha256()
    previous_entry_hash = handoff.ZERO_SHA256
    entries = 0
    bar_rows = 0
    quote_rows = 0
    read_only_files = 1
    read_only_directories = 2
    verified_hour_directories: set[Path] = set()
    inventory = dict(inventory)
    manifest_path = capture / handoff.MANIFEST_FILENAME

    try:
        with ExitStack() as stack:
            bar_handles: dict[str, BinaryIO] = {
                symbol: stack.enter_context(bar_paths[symbol].open("xb"))
                for symbol in handoff.SYMBOLS
            }
            quote_handles: dict[str, BinaryIO] = {
                symbol: stack.enter_context(quote_paths[symbol].open("xb"))
                for symbol in handoff.SYMBOLS
            }
            manifest_handle = stack.enter_context(manifest_path.open("rb"))
            manifest_stat_before = os.fstat(manifest_handle.fileno())
            for raw_line in manifest_handle:
                if (
                    not raw_line.endswith(b"\n")
                    or len(raw_line) > handoff.MAXIMUM_MANIFEST_LINE_BYTES
                ):
                    raise EvaluationRefusal("capture_manifest_line_invalid")
                manifest_digest.update(raw_line)
                try:
                    entry = handoff._strict_json_object(
                        raw_line[:-1],
                        reason="capture_manifest_json_invalid",
                    )
                except handoff.HandoffRefusal as exc:
                    raise EvaluationRefusal("capture_manifest_json_invalid") from exc
                if (
                    set(entry) != handoff.MANIFEST_FIELDS
                    or raw_line != canonical_json_bytes(entry) + b"\n"
                ):
                    raise EvaluationRefusal("capture_manifest_entry_invalid")
                entries += 1
                sequence = _strict_int(
                    entry.get("sequence"),
                    "capture_manifest_sequence_invalid",
                    minimum=1,
                )
                entry_body = dict(entry)
                claimed_entry_hash = str(
                    entry_body.pop("manifest_entry_sha256", "")
                ).lower()
                if (
                    sequence != entries
                    or entry.get("previous_entry_sha256") != previous_entry_hash
                    or not _is_sha256(claimed_entry_hash)
                    or not hmac.compare_digest(
                        claimed_entry_hash,
                        canonical_sha256(entry_body),
                    )
                    or handoff._binding_tuple(
                        entry,
                        "capture_binding_invalid",
                    )
                    != sealed.binding.tuple
                ):
                    raise EvaluationRefusal("capture_manifest_chain_invalid")
                utc_hour = str(entry.get("utc_hour") or "")
                segment = _strict_int(
                    entry.get("segment_index"),
                    "capture_segment_invalid",
                    minimum=1,
                )
                expected_relative = PurePosixPath(
                    handoff.CHUNK_DIRECTORY,
                    utc_hour,
                    f"ig-mt4-m1-activity-s{segment:04d}-q{sequence:010d}.json",
                ).as_posix()
                try:
                    chunk_path = handoff._safe_chunk_path(
                        capture,
                        str(entry.get("chunk_path") or ""),
                        expected=expected_relative,
                    )
                    hour_directory = chunk_path.parent
                    if hour_directory not in verified_hour_directories:
                        _require_read_only_directory(
                            hour_directory,
                            reason="capture_snapshot_hour_not_read_only",
                        )
                        verified_hour_directories.add(hour_directory)
                        read_only_directories += 1
                    _require_read_only_file(
                        chunk_path,
                        reason="capture_snapshot_chunk_not_read_only",
                    )
                    chunk_raw = chunk_path.read_bytes()
                    read_only_files += 1
                except (handoff.HandoffRefusal, OSError) as exc:
                    raise EvaluationRefusal("capture_chunk_invalid") from exc
                chunk_size = _strict_int(
                    entry.get("chunk_size_bytes"),
                    "capture_chunk_size_invalid",
                    minimum=1,
                )
                chunk_sha = str(entry.get("chunk_sha256") or "").lower()
                if (
                    len(chunk_raw) != chunk_size
                    or len(chunk_raw) > handoff.MAXIMUM_CHUNK_BYTES
                    or not _is_sha256(chunk_sha)
                    or not hmac.compare_digest(
                        hashlib.sha256(chunk_raw).hexdigest(),
                        chunk_sha,
                    )
                ):
                    raise EvaluationRefusal("capture_chunk_hash_invalid")
                try:
                    chunk = handoff._strict_json_object(
                        chunk_raw,
                        reason="capture_chunk_json_invalid",
                    )
                except handoff.HandoffRefusal as exc:
                    raise EvaluationRefusal("capture_chunk_json_invalid") from exc
                if (
                    set(chunk) != handoff.CHUNK_FIELDS
                    or chunk_raw != canonical_json_bytes(chunk) + b"\n"
                    or handoff._binding_tuple(
                        chunk,
                        "capture_binding_invalid",
                    )
                    != sealed.binding.tuple
                    or chunk.get("utc_hour") != utc_hour
                    or chunk.get("segment_index") != segment
                ):
                    raise EvaluationRefusal("capture_chunk_contract_invalid")
                raw_bars = chunk.get("bars")
                raw_quotes = chunk.get("quotes")
                if not isinstance(raw_bars, list) or not isinstance(raw_quotes, list):
                    raise EvaluationRefusal("capture_chunk_rows_invalid")
                if entry.get("bar_rows") != len(raw_bars) or entry.get(
                    "quote_rows"
                ) != len(raw_quotes):
                    raise EvaluationRefusal("capture_chunk_row_count_invalid")
                for raw_bar in raw_bars:
                    if not isinstance(raw_bar, Mapping):
                        raise EvaluationRefusal("capture_bar_invalid")
                    symbol = str(raw_bar.get("symbol") or "").upper()
                    if symbol not in bar_handles:
                        raise EvaluationRefusal("capture_bar_symbol_invalid")
                    record = BAR_RECORD.pack(
                        _strict_int(
                            raw_bar.get("minute_epoch"),
                            "capture_bar_invalid",
                            minimum=1,
                        ),
                        _positive(raw_bar.get("bid_open"), "capture_bar_invalid"),
                        _positive(raw_bar.get("bid_high"), "capture_bar_invalid"),
                        _positive(raw_bar.get("bid_low"), "capture_bar_invalid"),
                        _positive(raw_bar.get("bid_close"), "capture_bar_invalid"),
                        _strict_int(
                            raw_bar.get("tick_volume"),
                            "capture_bar_invalid",
                        ),
                    )
                    bar_handles[symbol].write(record)
                    source_digests[symbol].update(b"B" + record)
                    bar_counts[symbol] += 1
                    bar_rows += 1
                for raw_quote in raw_quotes:
                    if not isinstance(raw_quote, Mapping):
                        raise EvaluationRefusal("capture_quote_invalid")
                    symbol = str(raw_quote.get("symbol") or "").upper()
                    if symbol not in quote_handles:
                        raise EvaluationRefusal("capture_quote_symbol_invalid")
                    token_hash = str(
                        raw_quote.get("source_event_token_sha256") or ""
                    ).lower()
                    if not _is_sha256(token_hash):
                        raise EvaluationRefusal("capture_quote_hash_invalid")
                    record = QUOTE_RECORD.pack(
                        _strict_int(
                            raw_quote.get("observation_sequence"),
                            "capture_quote_invalid",
                            minimum=1,
                        ),
                        _strict_int(
                            raw_quote.get("observation_epoch"),
                            "capture_quote_invalid",
                            minimum=1,
                        ),
                        _positive(raw_quote.get("bid"), "capture_quote_invalid"),
                        _positive(raw_quote.get("ask"), "capture_quote_invalid"),
                        _strict_int(
                            raw_quote.get("market_event_sequence"),
                            "capture_quote_invalid",
                        ),
                        bytes.fromhex(token_hash),
                    )
                    quote_handles[symbol].write(record)
                    source_digests[symbol].update(b"Q" + record)
                    quote_counts[symbol] += 1
                    quote_rows += 1
                previous_entry_hash = claimed_entry_hash
            manifest_stat_after = os.fstat(manifest_handle.fileno())
            if (
                _source_stat_identity(manifest_stat_before)
                != _source_stat_identity(manifest_stat_after)
                or _snapshot_stat_identity(
                    manifest_stat_after,
                    sha256=manifest_digest.hexdigest(),
                )
                != snapshot_fence.manifest_identity
            ):
                raise EvaluationRefusal("capture_snapshot_manifest_changed")
            for handle in (*bar_handles.values(), *quote_handles.values()):
                handle.flush()
                os.fsync(handle.fileno())
    except Exception:
        _cleanup_materialization(stage, bar_paths, quote_paths)
        raise

    manifest_sha = manifest_digest.hexdigest()
    if (
        manifest_sha != inventory.get("manifest_sha256")
        or manifest_sha != snapshot_fence.manifest_identity.sha256
        or previous_entry_hash != inventory.get("manifest_head_sha256")
        or previous_entry_hash != snapshot_fence.manifest_head_sha256
        or entries != inventory.get("manifest_entries")
        or entries != snapshot_fence.manifest_entries
        or bar_rows != inventory.get("bar_rows")
        or quote_rows != inventory.get("quote_rows")
        or bar_counts != inventory.get("bar_rows_by_symbol")
        or quote_counts != inventory.get("quote_rows_by_symbol")
        or read_only_files != entries + 1
    ):
        _cleanup_materialization(stage, bar_paths, quote_paths)
        raise EvaluationRefusal("capture_changed_after_handoff_verification")
    try:
        for path in (*bar_paths.values(), *quote_paths.values()):
            path.chmod(stat.S_IREAD)
        stage.chmod(stat.S_IREAD | stat.S_IEXEC)
    except OSError as exc:
        _cleanup_materialization(stage, bar_paths, quote_paths)
        raise EvaluationRefusal("materialization_freeze_failed") from exc
    return MaterializedCapture(
        root=stage,
        bar_paths=bar_paths,
        quote_paths=quote_paths,
        bar_counts=bar_counts,
        quote_counts=quote_counts,
        source_sha256_by_symbol={
            symbol: source_digests[symbol].hexdigest() for symbol in handoff.SYMBOLS
        },
        manifest_sha256=manifest_sha,
        manifest_head_sha256=previous_entry_hash,
        manifest_entries=entries,
        projected_size_bytes=projected_bytes,
        snapshot_files_verified_read_only=read_only_files,
        snapshot_directories_verified_read_only=read_only_directories,
    )


def _cleanup_materialization(
    root: Path,
    bar_paths: Mapping[str, Path],
    quote_paths: Mapping[str, Path],
) -> bool:
    try:
        if root.is_dir() and not root.is_symlink():
            root.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    except OSError:
        pass
    for path in (*bar_paths.values(), *quote_paths.values()):
        try:
            if path.parent == root and path.is_file() and not path.is_symlink():
                path.chmod(stat.S_IREAD | stat.S_IWRITE)
                path.unlink()
        except OSError:
            pass
    try:
        if root.is_dir() and not root.is_symlink() and not any(root.iterdir()):
            root.rmdir()
    except OSError:
        pass
    return not root.exists()


def _binary_records(path: Path, record: struct.Struct) -> Iterator[tuple[Any, ...]]:
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise EvaluationRefusal("materialized_stream_unreadable") from exc
    with handle:
        while True:
            raw = handle.read(record.size)
            if not raw:
                break
            if len(raw) != record.size:
                raise EvaluationRefusal("materialized_stream_truncated")
            try:
                yield record.unpack(raw)
            except struct.error as exc:
                raise EvaluationRefusal("materialized_stream_invalid") from exc


def iter_materialized_bars(
    path: Path,
    *,
    screen_module: ModuleType = screen,
) -> Iterator[Any]:
    prior_epoch = 0
    for values in _binary_records(path, BAR_RECORD):
        epoch, bid_open, bid_high, bid_low, bid_close, tick_volume = values
        bar = screen_module.MT4BidBar(
            epoch=int(epoch),
            bid_open=float(bid_open),
            bid_high=float(bid_high),
            bid_low=float(bid_low),
            bid_close=float(bid_close),
            tick_volume=int(tick_volume),
        )
        if not screen_module.validate_bid_bar(bar) or bar.epoch <= prior_epoch:
            raise EvaluationRefusal("materialized_bar_order_invalid")
        prior_epoch = bar.epoch
        yield bar


def iter_materialized_quotes(
    path: Path,
    *,
    screen_module: ModuleType = screen,
) -> Iterator[Any]:
    prior_sequence = 0
    prior_epoch = 0
    for values in _binary_records(path, QUOTE_RECORD):
        sequence, epoch, bid, ask, event_sequence, token_bytes = values
        quote = screen_module.MT4Quote(
            epoch=int(epoch),
            bid=float(bid),
            ask=float(ask),
            source_event_token_sha256=bytes(token_bytes).hex(),
            market_event_sequence=int(event_sequence),
        )
        if (
            int(sequence) != prior_sequence + 1
            or quote.epoch <= prior_epoch
            or not screen_module.validate_quote(quote)
        ):
            raise EvaluationRefusal("materialized_quote_order_invalid")
        prior_sequence = int(sequence)
        prior_epoch = quote.epoch
        yield quote


def iter_post_t0_closed_signals(
    *,
    bar_path: Path,
    symbol: str,
    cost: Any,
    t0_epoch: float,
    end_epoch_exclusive: float,
    screen_module: ModuleType = screen,
) -> Iterator[Any]:
    """Yield frozen closed signals, retaining pre-T0 baseline bars only."""

    window: deque[Any] = deque(maxlen=screen_module.BASELINE_M1_BARS + 1)
    for global_index, bar in enumerate(
        iter_materialized_bars(bar_path, screen_module=screen_module)
    ):
        window.append(bar)
        if not t0_epoch <= bar.epoch < end_epoch_exclusive:
            continue
        # A bar is a signal observation, while execution is fixed at its
        # close.  The exclusive seal applies to that entry instant too: the
        # final bar whose close equals the seal end is context, never a trial.
        if bar.epoch + 60 >= end_epoch_exclusive:
            continue
        if len(window) != screen_module.BASELINE_M1_BARS + 1:
            continue
        if bar.bid_close == bar.bid_open:
            continue
        side = "BUY" if bar.bid_close > bar.bid_open else "SELL"
        prepared = tuple(window)
        closed, _reason = screen_module.evaluate_closed_signal(
            prepared=prepared,
            signal_index=screen_module.BASELINE_M1_BARS,
            symbol=symbol,
            side=side,
            cost=cost,
        )
        if closed is not None:
            yield replace(closed, signal_index=global_index)


@dataclass(slots=True)
class _ActiveOutcome:
    ordinal: int
    signal: Any
    previous_epoch: int


def _step_active(
    active: _ActiveOutcome,
    quote: Any,
    *,
    screen_module: ModuleType = screen,
) -> Any | None:
    signal = active.signal
    if quote.epoch - active.previous_epoch > screen_module.MAX_QUOTE_GAP_SECONDS:
        return screen_module._outcome(
            signal,
            quote=quote,
            exit_reason="QUOTE_GAP_ADVERSE",
            full_target_hit_first=False,
        )
    active.previous_epoch = quote.epoch
    executable = quote.bid if signal.side == "BUY" else quote.ask
    stop_hit = (
        executable <= signal.stop_price
        if signal.side == "BUY"
        else executable >= signal.stop_price
    )
    target_hit = (
        executable >= signal.target_price
        if signal.side == "BUY"
        else executable <= signal.target_price
    )
    if stop_hit:
        return screen_module._outcome(
            signal,
            quote=quote,
            exit_price=executable,
            exit_reason="STOP_LOSS",
            full_target_hit_first=False,
        )
    if target_hit:
        return screen_module._outcome(
            signal,
            quote=quote,
            exit_reason="TAKE_PROFIT",
            full_target_hit_first=True,
        )
    if quote.epoch >= signal.entry_epoch + screen_module.OUTCOME_HORIZON_M1_BARS * 60:
        return screen_module._outcome(
            signal,
            quote=quote,
            exit_price=executable,
            exit_reason="TIME_STOP",
            full_target_hit_first=False,
        )
    return None


def evaluate_materialized_symbol(
    *,
    symbol: str,
    bar_path: Path,
    quote_path: Path,
    cost: Any,
    t0_epoch: float,
    end_epoch_exclusive: float,
    screen_module: ModuleType = screen,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute the frozen one-reservation/day semantics for one symbol."""

    candidates = iter_post_t0_closed_signals(
        bar_path=bar_path,
        symbol=symbol,
        cost=cost,
        t0_epoch=t0_epoch,
        end_epoch_exclusive=end_epoch_exclusive,
        screen_module=screen_module,
    )
    next_candidate = next(candidates, None)
    reserved_days: set[str] = set()
    reservations: list[dict[str, Any]] = []
    outcome_by_ordinal: list[tuple[int, Any]] = []
    active_outcomes: list[_ActiveOutcome] = []
    next_ordinal = 0

    for quote in iter_materialized_quotes(
        quote_path,
        screen_module=screen_module,
    ):
        if quote.epoch < t0_epoch:
            continue
        if quote.epoch >= end_epoch_exclusive:
            break
        survivors: list[_ActiveOutcome] = []
        for active in active_outcomes:
            outcome = _step_active(active, quote, screen_module=screen_module)
            if outcome is None:
                survivors.append(active)
            else:
                outcome_by_ordinal.append((active.ordinal, outcome))
        active_outcomes = survivors

        while (
            next_candidate is not None
            and next_candidate.expected_entry_epoch <= quote.epoch
        ):
            closed = next_candidate
            next_candidate = next(candidates, None)
            if closed.entry_day in reserved_days:
                continue
            signal, entry_reason = screen_module.attach_entry(
                closed=closed,
                quotes=(quote,),
            )
            if entry_reason == "live_spread_above_frozen_p90":
                continue
            reserved_days.add(closed.entry_day)
            reservation = asdict(closed)
            reservation["entry_status"] = entry_reason or "admitted"
            reservations.append(reservation)
            ordinal = next_ordinal
            next_ordinal += 1
            if signal is None:
                outcome_by_ordinal.append(
                    (
                        ordinal,
                        screen_module.adverse_missing_entry_outcome(closed),
                    )
                )
            else:
                active_outcomes.append(
                    _ActiveOutcome(
                        ordinal=ordinal,
                        signal=signal,
                        previous_epoch=signal.entry_epoch,
                    )
                )

    while next_candidate is not None:
        closed = next_candidate
        next_candidate = next(candidates, None)
        if closed.entry_day in reserved_days:
            continue
        reserved_days.add(closed.entry_day)
        reservation = asdict(closed)
        reservation["entry_status"] = "contemporaneous_entry_quote_missing"
        reservations.append(reservation)
        ordinal = next_ordinal
        next_ordinal += 1
        outcome_by_ordinal.append(
            (ordinal, screen_module.adverse_missing_entry_outcome(closed))
        )
    for active in active_outcomes:
        outcome_by_ordinal.append(
            (
                active.ordinal,
                screen_module.score_signal(active.signal, quotes=()),
            )
        )
    outcomes = [
        asdict(outcome)
        for _ordinal, outcome in sorted(outcome_by_ordinal, key=lambda item: item[0])
    ]
    if len(reservations) != len(outcomes):
        raise EvaluationRefusal("reservation_outcome_cardinality_invalid")
    return reservations, outcomes


def _screen_result_from_materialization(
    *,
    sealed: SealedEvaluationInputs,
    materialized: MaterializedCapture,
) -> dict[str, Any]:
    selected_screen = sealed.screen_module
    source_ready = verified_handoff_proves_source_ready(
        sealed=sealed,
        materialized=materialized,
    )
    reservations: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    outcome_objects: list[Any] = []
    for symbol in handoff.SYMBOLS:
        symbol_reservations, symbol_outcomes = evaluate_materialized_symbol(
            symbol=symbol,
            bar_path=materialized.bar_paths[symbol],
            quote_path=materialized.quote_paths[symbol],
            cost=sealed.costs_by_symbol[symbol],
            t0_epoch=sealed.binding.t0_epoch,
            end_epoch_exclusive=sealed.binding.end_epoch_exclusive,
            screen_module=selected_screen,
        )
        reservations.extend(symbol_reservations)
        outcomes.extend(symbol_outcomes)
        outcome_objects.extend(
            selected_screen.MTVCLCOutcome(**row) for row in symbol_outcomes
        )
    cells = [
        selected_screen._cell_payload(
            symbol=symbol,
            side=side,
            source_ready=source_ready,
            outcomes=outcome_objects,
            break_even_probability=(
                sealed.costs_by_symbol[symbol].break_even_win_probability
            ),
        )
        for symbol in handoff.SYMBOLS
        for side in ("BUY", "SELL")
    ]
    result = {
        "schema_version": "fxstack.scalp.mtvclc_screen_result.v1",
        "strategy_id": selected_screen.STRATEGY_ID,
        "strategy_version": selected_screen.STRATEGY_VERSION,
        "config_ids": [selected_screen.CONFIG_ID],
        "symbol_scope": list(selected_screen.MTVCLC_SYMBOLS),
        "source_contract_id": selected_screen.SOURCE_CONTRACT_ID,
        "activity_metric_id": selected_screen.ACTIVITY_METRIC_ID,
        "attempt_accounting": dict(sealed.preregistration["attempt_accounting"]),
        "source_scope_ready": source_ready,
        "source_sha256_by_symbol": dict(materialized.source_sha256_by_symbol),
        "source_errors": [],
        "costs": {
            symbol: asdict(sealed.costs_by_symbol[symbol]) for symbol in handoff.SYMBOLS
        },
        "cells": cells,
        "reservation_ledger": reservations,
        "outcome_ledger": outcomes,
        "all_cells_pass_fixed_screen": all(
            cell["passes_fixed_cell_screen"] for cell in cells
        ),
        "attempt_manifest": dict(
            sealed.preregistration["strategy"]["attempt_manifest"]
        ),
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "order_authorized": False,
    }
    if not selected_screen.validate_result_bundle(result):
        raise EvaluationRefusal("frozen_screen_result_invalid")
    return result


def enforce_global_gates(screen_result: Mapping[str, Any]) -> dict[str, Any]:
    raw_outcomes = screen_result.get("outcome_ledger")
    if not isinstance(raw_outcomes, list):
        raise EvaluationRefusal("screen_outcome_ledger_invalid")
    entry_days = {
        str(row.get("entry_day") or "")
        for row in raw_outcomes
        if isinstance(row, Mapping) and str(row.get("entry_day") or "")
    }
    total_trades = len(raw_outcomes)
    total_days = len(entry_days)
    all_cells = screen_result.get("all_cells_pass_fixed_screen") is True
    source_ready = bool(
        screen_result.get("source_scope_ready") is True
        and screen_result.get("source_errors") == []
    )
    passed = bool(
        all_cells
        and source_ready
        and total_trades >= GLOBAL_MINIMUM_TRADES
        and total_days >= GLOBAL_MINIMUM_INDEPENDENT_DAYS
    )
    return {
        "all_44_cells_pass": all_cells,
        "source_scope_ready": source_ready,
        "total_trades": total_trades,
        "minimum_total_trades": GLOBAL_MINIMUM_TRADES,
        "total_independent_utc_days": total_days,
        "minimum_total_independent_utc_days": GLOBAL_MINIMUM_INDEPENDENT_DAYS,
        "global_trade_gate_pass": total_trades >= GLOBAL_MINIMUM_TRADES,
        "global_day_gate_pass": total_days >= GLOBAL_MINIMUM_INDEPENDENT_DAYS,
        "all_preregistered_success_gates_pass": passed,
    }


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    filename: str
    sha256: str
    size_bytes: int
    rows: int | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }
        if self.rows is not None:
            value["rows"] = self.rows
        return value


def _prepare_output_root(path: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if candidate.exists():
        return _resolved_directory(candidate, reason="output_root_invalid")
    if candidate.is_symlink():
        raise EvaluationRefusal("output_root_invalid")
    try:
        candidate.mkdir(parents=True, exist_ok=False)
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise EvaluationRefusal("output_root_invalid") from exc


def _publish_content_addressed(
    *,
    root: Path,
    prefix: str,
    suffix: str,
    payload: bytes,
    rows: int | None = None,
) -> ArtifactIdentity:
    """Publish once and refuse every collision, including byte-identical ones.

    A closed prospective evaluation is a one-shot evidentiary act.  Reusing an
    existing pathname would make it ambiguous whether this invocation wrote
    its claimed output, so callers must provide a fresh output namespace.
    """

    digest = hashlib.sha256(payload).hexdigest()
    filename = f"{prefix}_{digest}{suffix}"
    target = root / filename
    if target.exists():
        raise EvaluationRefusal("research_artifact_output_exists")
    temporary = root / f".{filename}.{os.getpid()}.tmp"
    if temporary.exists():
        raise EvaluationRefusal("research_artifact_temporary_exists")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        temporary.unlink()
        target.chmod(stat.S_IREAD)
        if target.read_bytes() != payload:
            raise EvaluationRefusal("research_artifact_reopen_mismatch")
    except EvaluationRefusal:
        raise
    except OSError as exc:
        try:
            if temporary.exists():
                temporary.chmod(stat.S_IREAD | stat.S_IWRITE)
                temporary.unlink()
        except OSError:
            pass
        raise EvaluationRefusal("research_artifact_publish_failed") from exc
    return ArtifactIdentity(filename, digest, len(payload), rows)


def _ledger_bytes(
    *,
    kind: str,
    rows: Sequence[Mapping[str, Any]],
    evidence_binding_sha256: str,
    handoff_body_sha256: str,
    capture_inventory_sha256: str,
) -> bytes:
    lines: list[bytes] = []
    for sequence, row in enumerate(rows, start=1):
        wrapped = {
            "schema_version": LEDGER_ROW_SCHEMA,
            "ledger_kind": kind,
            "sequence": sequence,
            "evidence_binding_sha256": evidence_binding_sha256,
            "handoff_body_sha256": handoff_body_sha256,
            "capture_inventory_sha256": capture_inventory_sha256,
            "record": dict(row),
            "research_only": True,
            "authority": dict(FALSE_AUTHORITY),
        }
        lines.append(canonical_json_bytes(wrapped) + b"\n")
    return b"".join(lines)


def publish_research_artifacts(
    *,
    output_root: str | Path,
    sealed: SealedEvaluationInputs,
    materialized: MaterializedCapture,
    screen_result: Mapping[str, Any],
    global_gates: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    if _read_executed_source(EVALUATOR_PATH).sha256 != EVALUATOR_SOURCE_IMAGE.sha256:
        raise EvaluationRefusal("evaluator_source_changed_before_publication")
    root = _prepare_output_root(output_root)
    inventory = dict(sealed.handoff_payload["capture_inventory"])
    evidence_binding = {
        "preregistration_body_sha256": (sealed.binding.preregistration_body_sha256),
        "preregistration_artifact_sha256": (
            sealed.binding.preregistration_artifact_sha256
        ),
        "handoff_body_sha256": sealed.handoff_payload["handoff_body_sha256"],
        "handoff_artifact_sha256": sealed.handoff_artifact_sha256,
        "capture_inventory_sha256": sealed.handoff_payload["capture_inventory_sha256"],
        "manifest_sha256": materialized.manifest_sha256,
        "manifest_head_sha256": materialized.manifest_head_sha256,
        "market_source_id": inventory["market_source_id"],
        "screen_source_filename": sealed.screen_source_filename,
        "frozen_screen_sha256": sealed.screen_source_sha256,
        "frozen_screen_support_sha256": (sealed.screen_support_source_sha256),
        "handoff_verifier_sha256": sealed.handoff_source_sha256,
        "evaluator_source_sha256": EVALUATOR_SOURCE_IMAGE.sha256,
        "cost_source_sha256": sealed.cost_source_sha256,
        "source_sha256_by_symbol": dict(materialized.source_sha256_by_symbol),
    }
    if sealed.guard_identity_sha256 is not None:
        evidence_binding["guard_identity_sha256"] = sealed.guard_identity_sha256
    evidence_binding_sha = canonical_sha256(evidence_binding)
    handoff_sha = str(sealed.handoff_payload["handoff_body_sha256"])
    inventory_sha = str(sealed.handoff_payload["capture_inventory_sha256"])
    ledgers: dict[str, ArtifactIdentity] = {}
    for name, rows in (
        (
            "reservation",
            list(screen_result.get("reservation_ledger") or []),
        ),
        ("outcome", list(screen_result.get("outcome_ledger") or [])),
        ("cell", list(screen_result.get("cells") or [])),
    ):
        ledger_payload = _ledger_bytes(
            kind=name,
            rows=rows,
            evidence_binding_sha256=evidence_binding_sha,
            handoff_body_sha256=handoff_sha,
            capture_inventory_sha256=inventory_sha,
        )
        ledgers[name] = _publish_content_addressed(
            root=root,
            prefix=f"mtvclc_{name}_ledger",
            suffix=".jsonl",
            payload=ledger_payload,
            rows=len(rows),
        )
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "evaluator_schema_version": EVALUATOR_SCHEMA,
        "strategy_id": handoff.STRATEGY_ID,
        "strategy_version": handoff.STRATEGY_VERSION,
        "config_id": handoff.CONFIG_ID,
        "symbol_scope": list(handoff.SYMBOLS),
        "prospective_t0_utc_inclusive": sealed.binding.t0_utc,
        "prospective_end_utc_exclusive": sealed.binding.end_utc_exclusive,
        "window_closed_before_capture_access": True,
        "capture_snapshot_required_stopped_and_read_only": True,
        "capture_snapshot_active_hour_journal_absent": True,
        "capture_snapshot_data_writer_lock_required": (
            sealed.screen_source_filename == REPLACEMENT_SCREEN_FILENAME
        ),
        "capture_snapshot_data_writer_lock_proven_free": (
            sealed.screen_source_filename == REPLACEMENT_SCREEN_FILENAME
        ),
        "capture_snapshot_reverified_after_materialization": True,
        "capture_snapshot_fence_rechecked_before_publication": True,
        "bootstrap_context_bars_retained": (sealed.screen_module.BASELINE_M1_BARS),
        "pre_t0_signal_bars_evaluated": 0,
        "pre_t0_quotes_evaluated": 0,
        "frozen_screen_semantics_executed": True,
        "frozen_screen_hash_is_exact_executed_bytes": True,
        "handoff_verifier_hash_is_exact_executed_bytes": True,
        "screen_result_valid": True,
        "screen_source_scope_ready": screen_result.get("source_scope_ready"),
        "screen_source_errors": list(screen_result.get("source_errors") or []),
        "screen_all_cells_pass_fixed_screen": screen_result.get(
            "all_cells_pass_fixed_screen"
        ),
        "global_success_gates": dict(global_gates),
        "preregistered_success_criteria_observed": bool(
            global_gates.get("all_preregistered_success_gates_pass") is True
        ),
        "evidence_binding": evidence_binding,
        "evidence_binding_sha256": evidence_binding_sha,
        "ledgers": {name: item.to_dict() for name, item in ledgers.items()},
        "capture_materialization": {
            "schema_version": MATERIALIZATION_SCHEMA,
            "bounded_memory": True,
            "disk_backed_per_symbol": True,
            "full_universe_loaded_in_memory": False,
            "manifest_entries": materialized.manifest_entries,
            "projected_size_bytes": materialized.projected_size_bytes,
            "bar_rows_by_symbol": dict(materialized.bar_counts),
            "quote_rows_by_symbol": dict(materialized.quote_counts),
            "snapshot_files_verified_read_only": (
                materialized.snapshot_files_verified_read_only
            ),
            "snapshot_directories_verified_read_only": (
                materialized.snapshot_directories_verified_read_only
            ),
            "scratch_retained": False,
        },
        "pbo_dsr_lineage_evaluated": False,
        "issuer_adapter_present": False,
        "evaluation_performed": True,
        "performance_statistics_computed": True,
        "research_only": True,
        "authority": dict(FALSE_AUTHORITY),
        "success_claim_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "issuer_authorized": False,
        "signature_authorized": False,
        "broker_access_authorized": False,
        "order_authorized": False,
    }
    report["report_body_sha256"] = canonical_sha256(report)
    report_payload = canonical_json_bytes(report) + b"\n"
    report_identity = _publish_content_addressed(
        root=root,
        prefix="mtvclc_post_window_report",
        suffix=".json",
        payload=report_payload,
    )
    if report_identity.sha256 != hashlib.sha256(report_payload).hexdigest():
        raise EvaluationRefusal("research_report_identity_invalid")
    return root / report_identity.filename, report


def evaluate_post_window(
    *,
    preregistration_path: str | Path,
    handoff_path: str | Path,
    capture_root: str | Path,
    staging_root: str | Path,
    output_root: str | Path,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run the complete isolated evaluation after the sealed end only."""

    now = _finite(
        time.time() if now_epoch is None else now_epoch,
        "evaluation_clock_invalid",
    )
    # Critical order: neither capture_root nor any path derived from it is
    # inspected before load_sealed_inputs proves the exclusive end has passed.
    sealed = load_sealed_inputs(
        preregistration_path=preregistration_path,
        handoff_path=handoff_path,
        now_epoch=now,
    )
    requires_data_writer_lock = (
        sealed.screen_source_filename == REPLACEMENT_SCREEN_FILENAME
    )
    capture_resolved = _require_stopped_read_only_capture_root(
        capture_root,
        require_data_writer_lock=requires_data_writer_lock,
    )
    try:
        recomputed_handoff = handoff.verify_capture_handoff(
            preregistration_path=preregistration_path,
            capture_root=capture_resolved,
            now_epoch=now,
        )
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal("capture_handoff_reverification_failed") from exc
    if canonical_json_bytes(recomputed_handoff) != canonical_json_bytes(
        sealed.handoff_payload
    ):
        raise EvaluationRefusal("capture_handoff_binding_mismatch")
    snapshot_fence = _build_capture_snapshot_fence(
        capture_root=capture_resolved,
        verified_handoff=recomputed_handoff,
        require_data_writer_lock=requires_data_writer_lock,
    )

    staging_resolved = _resolved_directory(
        staging_root,
        reason="staging_root_invalid",
    )
    output_candidate = Path(output_root).expanduser().resolve(strict=False)
    if (
        _paths_overlap(capture_resolved, staging_resolved)
        or _paths_overlap(capture_resolved, output_candidate)
        or _paths_overlap(staging_resolved, output_candidate)
    ):
        raise EvaluationRefusal("isolated_path_roots_overlap")

    materialized = materialize_verified_capture(
        sealed=sealed,
        capture_root=capture_resolved,
        staging_root=staging_resolved,
        snapshot_fence=snapshot_fence,
    )
    try:
        _reverify_capture_snapshot(
            fence=snapshot_fence,
            sealed=sealed,
            preregistration_path=preregistration_path,
            now_epoch=now,
        )
        screen_result = _screen_result_from_materialization(
            sealed=sealed,
            materialized=materialized,
        )
        global_gates = enforce_global_gates(screen_result)
    except Exception:
        _cleanup_materialization(
            materialized.root,
            materialized.bar_paths,
            materialized.quote_paths,
        )
        raise
    if not _cleanup_materialization(
        materialized.root,
        materialized.bar_paths,
        materialized.quote_paths,
    ):
        raise EvaluationRefusal("materialization_cleanup_failed")
    _assert_capture_snapshot_fence(snapshot_fence, rehash_manifest=True)
    report_path, report = publish_research_artifacts(
        output_root=output_root,
        sealed=sealed,
        materialized=materialized,
        screen_result=screen_result,
        global_gates=global_gates,
    )
    return report_path, report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate one closed, sealed MTVCLC capture in a physically "
            "isolated research environment."
        )
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report_path, report = evaluate_post_window(
            preregistration_path=args.preregistration,
            handoff_path=args.handoff,
            capture_root=args.capture_root,
            staging_root=args.staging_root,
            output_root=args.output_root,
        )
    except (EvaluationRefusal, OSError, ValueError) as exc:
        print(f"MTVCLC post-window evaluation refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "evaluated_research_only",
                "report": str(report_path),
                "report_body_sha256": report["report_body_sha256"],
                "preregistered_success_criteria_observed": report[
                    "preregistered_success_criteria_observed"
                ],
                "success_claim_authorized": False,
                "authority_granted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
