from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "seal_ig_tick_microstructure_candidate_preregistration.py"
SPEC = importlib.util.spec_from_file_location("microstructure_candidate_sealer", TOOL)
assert SPEC is not None and SPEC.loader is not None
sealer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sealer)


def _hashed(payload: dict, field: str) -> dict:
    result = deepcopy(payload)
    result[field] = sealer.canonical_sha256(result)
    return result


def _inputs(now: datetime) -> tuple[dict, dict, dict]:
    trials = []
    for candidate in sealer.FIXED_CANDIDATES:
        trials.append(
            {
                "symbol": candidate["symbol"],
                "model_family": candidate["model_family"],
                "threshold_bps": candidate["threshold_bps"],
                "trades": candidate["discovery_validation_trades"],
                "buy_trades": candidate["discovery_validation_buy_trades"],
                "sell_trades": candidate["discovery_validation_sell_trades"],
                "mean_bps": candidate["discovery_validation_mean_net_bps"],
                "total_bps": candidate["discovery_validation_total_net_bps"],
                "profit_factor": candidate["discovery_validation_profit_factor"],
            }
        )
    report = _hashed(
        {
            "schema_version": sealer.DISCOVERY_REPORT_SCHEMA,
            "research_only": True,
            "success_claim_authorized": False,
            "activation_authorized": False,
            "economic_test_pass": False,
            "selected_test": None,
            "symbol_validation_summary": {
                "held_out_test_opened": False,
                "trial_count": sealer.PRIOR_DISCOVERY_TRIALS,
            },
            "symbol_scope": list(sealer.SYMBOL_SCOPE),
            "extra_round_trip_cost_bps": 0.2,
            "fill_delay_events": 1,
            "future_data_access": "forbidden",
            "execution_prices": "buy_ask_sell_bid_exit_bid_ask",
            "capture_payload_sha256": "c" * 64,
            "capture_npz_sha256": "d" * 64,
            "results": [
                {
                    "horizon_secs": 600.0,
                    "status": "validation_economics_rejected",
                    "symbol_validation_trials": trials,
                }
            ],
        },
        "report_sha256",
    )
    market_source_id = "source-1"
    capture = _hashed(
        {
            "schema_version": sealer.CAPTURE_SCHEMA,
            "capture_mode": "authenticated_same_source_db_history_full",
            "account_mode": "demo",
            "venue_id": sealer.VENUE_ID,
            "scope_version": sealer.SCOPE_VERSION,
            "symbol_scope": list(sealer.SYMBOL_SCOPE),
            "point_in_time_audit": {"passed": True},
            "source_errors": [],
            "npz_sha256": "d" * 64,
            "market_source_audit": {
                "authenticated": True,
                "market_source_id_sha256": hashlib.sha256(
                    market_source_id.encode("utf-8")
                ).hexdigest(),
            },
        },
        "capture_payload_sha256",
    )
    report["capture_payload_sha256"] = capture["capture_payload_sha256"]
    report.pop("report_sha256")
    report["report_sha256"] = sealer.canonical_sha256(report)
    readiness = _hashed(
        {
            "schema_version": sealer.READINESS_SCHEMA,
            "collection_metadata_only": True,
            "source_identity_authenticated": True,
            "database_read_only": True,
            "research_authorized": False,
            "selection_authorized": False,
            "activation_authorized": False,
            "order_authorized": False,
            "venue_id": sealer.VENUE_ID,
            "scope_version": sealer.SCOPE_VERSION,
            "symbol_scope": list(sealer.SYMBOL_SCOPE),
            "current_market_source_id": market_source_id,
            "observed_at_epoch": now.timestamp() - 10.0,
        },
        "payload_sha256",
    )
    return report, capture, readiness


def _build(now: datetime, report: dict, capture: dict, readiness: dict) -> dict:
    return sealer.build_preregistration(
        report=report,
        report_file_sha256="1" * 64,
        capture=capture,
        capture_file_sha256="2" * 64,
        readiness=readiness,
        readiness_file_sha256="3" * 64,
        sealed_at=now,
        start_delay_seconds=120,
        expected_report_sha256=report["report_sha256"],
    )


def test_seals_two_fixed_future_cells_with_all_authority_false() -> None:
    now = datetime(2026, 8, 4, 16, 0, 1, tzinfo=UTC)
    report, capture, readiness = _inputs(now)

    payload = _build(now, report, capture, readiness)

    assert sealer.validate_preregistration(payload) is True
    assert payload["prospective_window"]["t0_utc_inclusive"] == (
        "2026-08-04T16:03:00Z"
    )
    assert payload["prospective_window"]["end_utc_exclusive"] == (
        "2026-09-03T16:03:00Z"
    )
    assert payload["fixed_candidates"] == [
        dict(item) for item in sealer.FIXED_CANDIDATES
    ]
    assert payload["attempt_accounting"]["cumulative_trials_lower_bound"] == 2774
    assert not any(payload["authority"].values())


def test_refuses_current_source_rollover() -> None:
    now = datetime(2026, 8, 4, 16, 0, 1, tzinfo=UTC)
    report, capture, readiness = _inputs(now)
    readiness["current_market_source_id"] = "other-source"
    readiness.pop("payload_sha256")
    readiness["payload_sha256"] = sealer.canonical_sha256(readiness)

    with pytest.raises(
        sealer.CandidatePreregistrationRefusal,
        match="current_source_readiness_invalid",
    ):
        _build(now, report, capture, readiness)


def test_refuses_if_discovery_test_was_opened() -> None:
    now = datetime(2026, 8, 4, 16, 0, 1, tzinfo=UTC)
    report, capture, readiness = _inputs(now)
    report["symbol_validation_summary"]["held_out_test_opened"] = True
    report.pop("report_sha256")
    report["report_sha256"] = sealer.canonical_sha256(report)

    with pytest.raises(
        sealer.CandidatePreregistrationRefusal,
        match="discovery_report_contract_invalid",
    ):
        _build(now, report, capture, readiness)
