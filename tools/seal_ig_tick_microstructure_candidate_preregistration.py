"""Seal a narrow future-only tick-microstructure candidate declaration.

This tool turns the validation-only v12 discovery report into two fixed
prospective cells.  It reads local authority-free artifacts only, performs no
network or database access, and cannot evaluate outcomes or authorize orders.
"""

from __future__ import annotations

# AGENT: ROLE: offline prospective microstructure-candidate sealer.
# AGENT: HANDSHAKE: fixed discovery report + capture + fresh readiness -> future declaration.
# AGENT: ISOLATION: local JSON reads and one exclusive JSON publication only.

import argparse
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "fxstack.ig_tick_microstructure_candidate_preregistration.v1"
TOOL_REVISION = "fxstack.ig_tick_microstructure_candidate_sealer.v1"
DISCOVERY_REPORT_SCHEMA = "fxstack.ig_tick_microstructure_screen.v1"
DISCOVERY_REPORT_SHA256 = (
    "61cabc21612dc5d1f2395e1aa03ec9d1b47d150db63b3755d29544279009e72c"
)
CAPTURE_SCHEMA = "fxstack.external_ig_mt4_bid_ask_capture.v1"
READINESS_SCHEMA = "fxstack.ig_tick_history_readiness.v1"
SCOPE_VERSION = "fxstack.ig_mt4.scalp_scope.v3"
VENUE_ID = "ig_mt4"
WINDOW_DAYS = 30
MINIMUM_PUBLICATION_LEAD_SECONDS = 120
MAXIMUM_READINESS_AGE_SECONDS = 300
PRIOR_DISCOVERY_TRIALS = 2_772
CURRENT_PROSPECTIVE_CELLS = 2
SYMBOL_SCOPE = (
    "EURUSD", "USDJPY", "AUDUSD", "GBPUSD", "USDCAD", "USDCHF",
    "EURGBP", "EURJPY", "NZDUSD", "AUDJPY", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "GBPCAD", "GBPCHF", "GBPJPY",
    "BTCUSD", "ETHUSD", "AUDCAD", "NZDJPY",
)
FIXED_CANDIDATES = (
    {
        "symbol": "USDJPY",
        "model_family": "triangular_basis_reversion",
        "horizon_secs": 600.0,
        "threshold_bps": 0.3,
        "discovery_validation_trades": 28,
        "discovery_validation_buy_trades": 18,
        "discovery_validation_sell_trades": 10,
        "discovery_validation_mean_net_bps": 0.4053122727682271,
        "discovery_validation_total_net_bps": 11.34874363751036,
        "discovery_validation_profit_factor": 1.3635765155330024,
    },
    {
        "symbol": "AUDUSD",
        "model_family": "triangular_basis_reversion",
        "horizon_secs": 600.0,
        "threshold_bps": 0.5,
        "discovery_validation_trades": 21,
        "discovery_validation_buy_trades": 10,
        "discovery_validation_sell_trades": 11,
        "discovery_validation_mean_net_bps": 0.22115584943147654,
        "discovery_validation_total_net_bps": 4.644272838061007,
        "discovery_validation_profit_factor": 1.1657621053598854,
    },
)


class CandidatePreregistrationRefusal(RuntimeError):
    """Raised when prospective separation or source identity is not proven."""


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: str | Path, *, reason: str) -> tuple[dict[str, Any], str]:
    candidate = Path(path).expanduser().resolve()
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(reason)
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        file_sha = _file_sha256(candidate)
    except (OSError, ValueError):
        raise CandidatePreregistrationRefusal(reason) from None
    if not isinstance(payload, dict):
        raise CandidatePreregistrationRefusal(reason)
    return payload, file_sha


def _verify_embedded_hash(
    payload: Mapping[str, Any], *, field: str, reason: str
) -> str:
    expected = str(payload.get(field) or "").strip().lower()
    body = dict(payload)
    body.pop(field, None)
    if len(expected) != 64 or canonical_sha256(body) != expected:
        raise CandidatePreregistrationRefusal(reason)
    return expected


def _same_number(left: Any, right: Any) -> bool:
    try:
        return math.isclose(float(left), float(right), rel_tol=0.0, abs_tol=1e-12)
    except (TypeError, ValueError, OverflowError):
        return False


def _validate_discovery(
    report: Mapping[str, Any], *, expected_report_sha256: str
) -> None:
    report_sha = _verify_embedded_hash(
        report,
        field="report_sha256",
        reason="discovery_report_hash_mismatch",
    )
    if report_sha != str(expected_report_sha256).strip().lower():
        raise CandidatePreregistrationRefusal("discovery_report_identity_mismatch")
    summary = report.get("symbol_validation_summary")
    if (
        report.get("schema_version") != DISCOVERY_REPORT_SCHEMA
        or report.get("research_only") is not True
        or report.get("success_claim_authorized") is not False
        or report.get("activation_authorized") is not False
        or report.get("economic_test_pass") is not False
        or report.get("selected_test") is not None
        or not isinstance(summary, Mapping)
        or summary.get("held_out_test_opened") is not False
        or int(summary.get("trial_count") or 0) != PRIOR_DISCOVERY_TRIALS
        or list(report.get("symbol_scope") or []) != list(SYMBOL_SCOPE)
        or not _same_number(report.get("extra_round_trip_cost_bps"), 0.2)
        or int(report.get("fill_delay_events") or 0) != 1
        or report.get("future_data_access") != "forbidden"
        or report.get("execution_prices") != "buy_ask_sell_bid_exit_bid_ask"
    ):
        raise CandidatePreregistrationRefusal("discovery_report_contract_invalid")

    horizon = next(
        (
            item
            for item in list(report.get("results") or [])
            if isinstance(item, Mapping)
            and _same_number(item.get("horizon_secs"), 600.0)
        ),
        None,
    )
    if not isinstance(horizon, Mapping) or horizon.get("status") != (
        "validation_economics_rejected"
    ):
        raise CandidatePreregistrationRefusal("discovery_horizon_missing")
    trials = list(horizon.get("symbol_validation_trials") or [])
    for candidate in FIXED_CANDIDATES:
        trial = next(
            (
                item
                for item in trials
                if isinstance(item, Mapping)
                and item.get("symbol") == candidate["symbol"]
                and item.get("model_family") == candidate["model_family"]
                and _same_number(
                    item.get("threshold_bps"), candidate["threshold_bps"]
                )
            ),
            None,
        )
        if not isinstance(trial, Mapping):
            raise CandidatePreregistrationRefusal("fixed_candidate_missing")
        checks = (
            (trial.get("trades"), candidate["discovery_validation_trades"]),
            (trial.get("buy_trades"), candidate["discovery_validation_buy_trades"]),
            (trial.get("sell_trades"), candidate["discovery_validation_sell_trades"]),
            (trial.get("mean_bps"), candidate["discovery_validation_mean_net_bps"]),
            (trial.get("total_bps"), candidate["discovery_validation_total_net_bps"]),
            (trial.get("profit_factor"), candidate["discovery_validation_profit_factor"]),
        )
        if not all(_same_number(observed, expected) for observed, expected in checks):
            raise CandidatePreregistrationRefusal("fixed_candidate_metrics_changed")


def _validate_capture(report: Mapping[str, Any], capture: Mapping[str, Any]) -> str:
    capture_sha = _verify_embedded_hash(
        capture,
        field="capture_payload_sha256",
        reason="capture_payload_hash_mismatch",
    )
    audit = capture.get("market_source_audit")
    if (
        capture.get("schema_version") != CAPTURE_SCHEMA
        or capture.get("capture_mode")
        != "authenticated_same_source_db_history_full"
        or capture.get("account_mode") != "demo"
        or capture.get("venue_id") != VENUE_ID
        or capture.get("scope_version") != SCOPE_VERSION
        or list(capture.get("symbol_scope") or []) != list(SYMBOL_SCOPE)
        or capture.get("point_in_time_audit", {}).get("passed") is not True
        or list(capture.get("source_errors") or [])
        or not isinstance(audit, Mapping)
        or audit.get("authenticated") is not True
        or len(str(audit.get("market_source_id_sha256") or "")) != 64
        or report.get("capture_payload_sha256") != capture_sha
        or report.get("capture_npz_sha256") != capture.get("npz_sha256")
    ):
        raise CandidatePreregistrationRefusal("capture_contract_invalid")
    return str(audit["market_source_id_sha256"]).lower()


def _validate_readiness(
    readiness: Mapping[str, Any], *, source_id_sha256: str, sealed_at_epoch: float
) -> str:
    readiness_sha = _verify_embedded_hash(
        readiness,
        field="payload_sha256",
        reason="readiness_payload_hash_mismatch",
    )
    observed = float(readiness.get("observed_at_epoch") or 0.0)
    current_source = str(readiness.get("current_market_source_id") or "")
    if (
        readiness.get("schema_version") != READINESS_SCHEMA
        or readiness.get("collection_metadata_only") is not True
        or readiness.get("source_identity_authenticated") is not True
        or readiness.get("database_read_only") is not True
        or readiness.get("research_authorized") is not False
        or readiness.get("selection_authorized") is not False
        or readiness.get("activation_authorized") is not False
        or readiness.get("order_authorized") is not False
        or readiness.get("venue_id") != VENUE_ID
        or readiness.get("scope_version") != SCOPE_VERSION
        or list(readiness.get("symbol_scope") or []) != list(SYMBOL_SCOPE)
        or not current_source
        or hashlib.sha256(current_source.encode("utf-8")).hexdigest()
        != source_id_sha256
        or observed <= 0.0
        or observed > sealed_at_epoch
        or sealed_at_epoch - observed > MAXIMUM_READINESS_AGE_SECONDS
    ):
        raise CandidatePreregistrationRefusal("current_source_readiness_invalid")
    return readiness_sha


def build_preregistration(
    *,
    report: Mapping[str, Any],
    report_file_sha256: str,
    capture: Mapping[str, Any],
    capture_file_sha256: str,
    readiness: Mapping[str, Any],
    readiness_file_sha256: str,
    sealed_at: datetime,
    start_delay_seconds: int,
    expected_report_sha256: str = DISCOVERY_REPORT_SHA256,
) -> dict[str, Any]:
    if sealed_at.tzinfo is None:
        raise CandidatePreregistrationRefusal("sealed_at_timezone_missing")
    sealed = sealed_at.astimezone(UTC)
    if int(start_delay_seconds) < MINIMUM_PUBLICATION_LEAD_SECONDS:
        raise CandidatePreregistrationRefusal("publication_lead_too_short")
    _validate_discovery(report, expected_report_sha256=expected_report_sha256)
    source_id_sha = _validate_capture(report, capture)
    readiness_sha = _validate_readiness(
        readiness,
        source_id_sha256=source_id_sha,
        sealed_at_epoch=sealed.timestamp(),
    )
    earliest = sealed.timestamp() + int(start_delay_seconds)
    t0_epoch = int(math.ceil(earliest / 60.0) * 60)
    t0 = datetime.fromtimestamp(t0_epoch, tz=UTC)
    end = t0 + timedelta(days=WINDOW_DAYS)
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool_revision": TOOL_REVISION,
        "sealed_at_utc": sealed.isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "discovery_lineage": {
            "report_sha256": str(report["report_sha256"]),
            "report_file_sha256": report_file_sha256,
            "capture_payload_sha256": str(capture["capture_payload_sha256"]),
            "capture_file_sha256": capture_file_sha256,
            "readiness_payload_sha256": readiness_sha,
            "readiness_file_sha256": readiness_file_sha256,
            "prior_validation_trials_counted": PRIOR_DISCOVERY_TRIALS,
            "held_out_test_opened": False,
            "selection_role": "discovery_only_not_evidence",
        },
        "attempt_accounting": {
            "prior_discovery_trials": PRIOR_DISCOVERY_TRIALS,
            "current_prospective_cells": CURRENT_PROSPECTIVE_CELLS,
            "cumulative_trials_lower_bound": (
                PRIOR_DISCOVERY_TRIALS + CURRENT_PROSPECTIVE_CELLS
            ),
        },
        "source_binding": {
            "venue_id": VENUE_ID,
            "account_mode": "demo",
            "scope_version": SCOPE_VERSION,
            "ordered_symbols": list(SYMBOL_SCOPE),
            "market_source_id_sha256": source_id_sha,
            "source_rollover_refuses_evaluation": True,
        },
        "prospective_window": {
            "t0_utc_inclusive": t0.isoformat().replace("+00:00", "Z"),
            "end_utc_exclusive": end.isoformat().replace("+00:00", "Z"),
            "consecutive_days": WINDOW_DAYS,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_outcome_or_performance_evaluation_forbidden": True,
            "extension_restart_or_early_success_forbidden": True,
        },
        "fixed_candidates": [dict(item) for item in FIXED_CANDIDATES],
        "execution_contract": {
            "fill_delay_events": 1,
            "entry_and_exit_prices": "buy_ask_sell_bid_exit_bid_ask",
            "future_data_access": "forbidden",
            "extra_round_trip_cost_bps": 0.2,
            "overlapping_positions_per_candidate": 1,
            "pending_orders_forbidden": True,
        },
        "fixed_success_gates": {
            "all_two_cells_must_pass": True,
            "minimum_trades_per_cell": 100,
            "minimum_buy_trades_per_cell": 20,
            "minimum_sell_trades_per_cell": 20,
            "mean_net_bps_strictly_greater_than": 0.0,
            "total_net_bps_strictly_greater_than": 0.0,
            "profit_factor_strictly_greater_than": 1.0,
            "minimum_independent_utc_days_per_cell": 20,
            "one_sided_day_block_bootstrap_family_alpha": 0.025,
            "minimum_exact_scope_source_history_days": 30,
        },
        "authority": {
            "outcome_access_authorized": False,
            "research_process_authorized": False,
            "selection_authorized": False,
            "success_claim_authorized": False,
            "signature_authorized": False,
            "activation_authorized": False,
            "runtime_authorized": False,
            "broker_access_authorized": False,
            "order_authorized": False,
        },
    }
    payload["preregistration_body_sha256"] = canonical_sha256(payload)
    return payload


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    body_sha = str(payload.get("preregistration_body_sha256") or "")
    body = dict(payload)
    body.pop("preregistration_body_sha256", None)
    authority = payload.get("authority")
    window = payload.get("prospective_window")
    return bool(
        payload.get("schema_version") == SCHEMA_VERSION
        and payload.get("tool_revision") == TOOL_REVISION
        and body_sha == canonical_sha256(body)
        and payload.get("fixed_candidates")
        == [dict(item) for item in FIXED_CANDIDATES]
        and payload.get("attempt_accounting", {}).get(
            "cumulative_trials_lower_bound"
        )
        == PRIOR_DISCOVERY_TRIALS + CURRENT_PROSPECTIVE_CELLS
        and isinstance(window, Mapping)
        and window.get("consecutive_days") == WINDOW_DAYS
        and isinstance(authority, Mapping)
        and authority
        and not any(authority.values())
    )


def atomic_publish(*, output_root: str | Path, payload: Mapping[str, Any]) -> Path:
    if not validate_preregistration(payload):
        raise CandidatePreregistrationRefusal("preregistration_invalid")
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    body_sha = str(payload["preregistration_body_sha256"])
    output = root / f"ig_tick_microstructure_candidate_prereg_{body_sha}.json"
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError:
        raise CandidatePreregistrationRefusal("preregistration_output_exists") from None
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--discovery-report", required=True)
    parser.add_argument("--capture-json", required=True)
    parser.add_argument("--readiness-json", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--start-delay-seconds", type=int, default=300)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report, report_file_sha = _read_json(
            args.discovery_report, reason="discovery_report_unreadable"
        )
        capture, capture_file_sha = _read_json(
            args.capture_json, reason="capture_payload_unreadable"
        )
        readiness, readiness_file_sha = _read_json(
            args.readiness_json, reason="readiness_payload_unreadable"
        )
        payload = build_preregistration(
            report=report,
            report_file_sha256=report_file_sha,
            capture=capture,
            capture_file_sha256=capture_file_sha,
            readiness=readiness,
            readiness_file_sha256=readiness_file_sha,
            sealed_at=datetime.now(UTC),
            start_delay_seconds=args.start_delay_seconds,
        )
        output = atomic_publish(output_root=args.output_root, payload=payload)
    except (CandidatePreregistrationRefusal, OSError) as exc:
        print(f"microstructure candidate preregistration refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "preregistration_body_sha256": payload[
                    "preregistration_body_sha256"
                ],
                "t0_utc_inclusive": payload["prospective_window"][
                    "t0_utc_inclusive"
                ],
                "end_utc_exclusive": payload["prospective_window"][
                    "end_utc_exclusive"
                ],
                "candidate_cells": CURRENT_PROSPECTIVE_CELLS,
                "prior_discovery_trials_counted": PRIOR_DISCOVERY_TRIALS,
                "authority_granted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
