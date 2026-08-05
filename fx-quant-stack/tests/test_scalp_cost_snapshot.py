from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.scalp_cost_snapshot import (
    RUNTIME_COST_SNAPSHOT_SCHEMA,
    load_runtime_cost_snapshot,
)


def _capture() -> dict:
    symbols = {
        symbol: {
            "duration_secs": 300.0,
            "observations": 100,
            "p90_observed_spread_bps": 1.0 + index / 100.0,
        }
        for index, symbol in enumerate(sorted(IG_MT4_SCALP_SYMBOLS))
    }
    audit_symbols = {
        symbol: {
            "duration_secs": 300.0,
            "observations": 100,
            "passed": True,
        }
        for symbol in sorted(IG_MT4_SCALP_SYMBOLS)
    }
    return {
        "schema_version": "fxstack.external_ig_mt4_bid_ask_capture.v1",
        "capture_definition": "authenticated_ig_demo_live_quote_calibration.v1",
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "source_errors": [],
        "capture_payload_sha256": "a" * 64,
        "source_id": "authenticated_ig_demo_mt4_bridge",
        "source_version": "v3.0.0",
        "symbols": symbols,
        "point_in_time_audit": {
            "passed": True,
            "errors": [],
            "database_read_only": True,
            "sample_source": "authenticated_same_source_db_history",
            "minimum_samples_per_symbol": 100,
            "minimum_duration_secs": 300.0,
            "symbols": audit_symbols,
        },
    }


def _write_capture(tmp_path: Path, payload: dict) -> tuple[Path, str]:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    path = tmp_path / "capture.json"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_valid_capture_projects_exact_22_runtime_costs(tmp_path: Path) -> None:
    path, digest = _write_capture(tmp_path, _capture())

    result = load_runtime_cost_snapshot(
        capture_path=path,
        expected_file_sha256=digest,
        account_currency="USD",
    )

    assert result.valid is True
    assert result.reason == ""
    assert result.errors == ()
    assert result.schema_version == RUNTIME_COST_SNAPSHOT_SCHEMA
    assert result.authority is False
    assert result.execution_eligible is False
    assert result.qualification_eligible is False
    assert tuple(result.costs_by_symbol) == tuple(IG_MT4_SCALP_SYMBOLS)
    assert result.capture_file_sha256 == digest
    assert result.commission_assumption_bps_per_round_trip == 0.0
    assert result.financing_assumption_bps_per_trade == 0.0
    assert result.costs_by_symbol["EURUSD"].pnl_currency == "USD"
    assert result.costs_by_symbol["EURUSD"].convert_on_close_charge_fraction == 0.0
    assert result.costs_by_symbol["USDJPY"].pnl_currency == "JPY"
    assert result.costs_by_symbol["USDJPY"].convert_on_close_charge_fraction > 0.0
    assert all(cost.row_sha256() for cost in result.costs_by_symbol.values())


def test_snapshot_rejects_hash_tamper_and_non_demo_capture(tmp_path: Path) -> None:
    payload = _capture()
    path, digest = _write_capture(tmp_path, payload)
    tampered = load_runtime_cost_snapshot(
        capture_path=path,
        expected_file_sha256="f" * 64,
        account_currency="USD",
    )
    assert tampered.valid is False
    assert tampered.reason == "runtime_cost_capture_sha256_mismatch"

    payload["account_mode"] = "real"
    path, digest = _write_capture(tmp_path, payload)
    wrong_mode = load_runtime_cost_snapshot(
        capture_path=path,
        expected_file_sha256=digest,
        account_currency="USD",
    )
    assert wrong_mode.valid is False
    assert "runtime_cost_capture_account_mode_invalid" in wrong_mode.errors


def test_snapshot_rejects_failed_audit_and_non_usd_account(tmp_path: Path) -> None:
    payload = _capture()
    payload["point_in_time_audit"]["passed"] = False
    path, digest = _write_capture(tmp_path, payload)

    result = load_runtime_cost_snapshot(
        capture_path=path,
        expected_file_sha256=digest,
        account_currency="EUR",
    )

    assert result.valid is False
    assert "runtime_cost_capture_point_in_time_failed" in result.errors
    assert "runtime_cost_snapshot_account_currency_unsupported" in result.errors
