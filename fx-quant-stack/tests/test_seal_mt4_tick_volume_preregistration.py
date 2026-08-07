from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration.py"
SPEC = importlib.util.spec_from_file_location(
    "seal_mt4_tick_volume_preregistration", TOOL_PATH
)
assert SPEC is not None and SPEC.loader is not None
seal = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = seal
SPEC.loader.exec_module(seal)

SEALED_AT = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _cost_capture(root: Path) -> tuple[Path, Path]:
    root.mkdir()
    npz = root / "ig_mt4_bid_ask_samples.npz"
    npz.write_bytes(b"sealed test npz bytes")
    market_source_audit = {
        "authenticated": True,
        "venue_id": seal.IG_MT4_VENUE_ID,
        "account_mode": "demo",
    }
    broker_contract_audit = {
        "venue_id": seal.IG_MT4_VENUE_ID,
        "symbol_scope": list(seal.IG_MT4_SCALP_SYMBOLS),
    }
    point_in_time_audit = {"passed": True, "errors": []}
    payload = {
        "schema_version": seal.COST_CAPTURE_SCHEMA,
        "capture_definition": "authenticated_ig_demo_live_quote_calibration.v1",
        "capture_mode": "authenticated_same_source_db_history",
        "source_errors": [],
        "venue_id": seal.IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "source_id": "bridge_authenticated_ig_mt4",
        "source_version": "v3.0.0",
        "scope_version": seal.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(seal.IG_MT4_SCALP_SYMBOLS),
        "capture_start_epoch": SEALED_AT.timestamp() - 600.0,
        "capture_end_epoch": SEALED_AT.timestamp() - 1.0,
        "created_at_epoch": SEALED_AT.timestamp() - 1.0,
        "account_scope_sha256": "1" * 64,
        "terminal_producer_instance_sha256": "2" * 64,
        "market_source_audit": market_source_audit,
        "market_source_audit_sha256": seal.canonical_sha256(
            market_source_audit
        ),
        "broker_contract_audit": broker_contract_audit,
        "broker_contract_snapshot_sha256": seal.canonical_sha256(
            broker_contract_audit
        ),
        "point_in_time_audit": point_in_time_audit,
        "point_in_time_audit_sha256": seal.canonical_sha256(
            point_in_time_audit
        ),
        "execution_contract": {},
        "npz_path": npz.name,
        "npz_sha256": _sha256(npz),
        "npz_size_bytes": npz.stat().st_size,
        "npz_arrays": {},
        "symbols": {
            symbol: {
                "trade_allowed": True,
                "p90_observed_spread_bps": 1.0 + index / 100.0,
            }
            for index, symbol in enumerate(seal.IG_MT4_SCALP_SYMBOLS)
        },
    }
    payload["capture_payload_sha256"] = seal.canonical_sha256(payload)
    return _write_json(root / "ig_mt4_bid_ask_capture.json", payload), npz


def _fee_attestation(root: Path, *, account_currency: str = "USD") -> Path:
    root.mkdir()
    documents: list[dict] = []
    retrieved_at = "2026-08-03T13:00:00Z"
    for role, url in seal.SOURCE_DOCUMENT_URLS.items():
        source = root / f"{role}.txt"
        source.write_text(f"frozen source for {role}\n", encoding="utf-8")
        documents.append(
            {
                "role": role,
                "url": url,
                "path": source.name,
                "sha256": _sha256(source),
                "retrieved_at_utc": retrieved_at,
            }
        )
    symbols = {}
    for symbol in seal.IG_MT4_SCALP_SYMBOLS:
        product_role = (
            "ig_mt4_crypto_product_details"
            if symbol in {"BTCUSD", "ETHUSD"}
            else "ig_mt4_forex_product_details"
        )
        symbols[symbol] = {
            "commission_bps_per_round_trip": 0.0,
            "commission_status": "explicit_source_attested",
            "commission_source_role": product_role,
            "financing_bps_per_trade": 0.0,
            "financing_status": (
                "structurally_avoided_by_fixed_rollover_guard"
            ),
            "financing_source_role": product_role,
            "profit_loss_currency": symbol[3:],
            "conversion_rate_of_absolute_profit_or_loss": 0.005,
            "conversion_status": (
                "debit_absolute_profit_or_loss_when_account_currency_differs"
            ),
            "conversion_source_role": "ig_mt4_forex_product_details",
        }
    payload = {
        "schema_version": seal.FEE_ATTESTATION_SCHEMA,
        "venue_id": seal.IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "account_currency": account_currency,
        "scope_version": seal.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(seal.IG_MT4_SCALP_SYMBOLS),
        "effective_at_utc": "2026-05-27T00:00:00Z",
        "attested_at_utc": retrieved_at,
        "source_errors": [],
        "source_documents": documents,
        "symbols": symbols,
    }
    payload["operator_attestation_sha256"] = seal.canonical_sha256(payload)
    return _write_json(root / "mtvclc_fee_attestation.json", payload)


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    capture, npz = _cost_capture(tmp_path / "capture")
    fee = _fee_attestation(tmp_path / "fees")
    return capture, npz, fee


def test_builds_exact_authority_free_prospective_preregistration(
    tmp_path: Path,
) -> None:
    capture, npz, fee = _inputs(tmp_path)
    payload = seal.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=SEALED_AT,
    )

    assert seal.validate_preregistration(payload) is True
    assert payload["strategy"]["strategy_id"] == seal.screen.STRATEGY_ID
    assert payload["strategy"]["config_id"] == seal.screen.CONFIG_ID
    assert payload["strategy"]["source_contract_id"] == (
        seal.screen.SOURCE_CONTRACT_ID
    )
    assert payload["scope"]["ordered_symbols"] == list(
        seal.IG_MT4_SCALP_SYMBOLS
    )
    assert len(payload["scope"]["cell_order"]) == 44
    assert payload["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_654,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_698,
    }
    assert payload["abandoned_preregistrations"] == [
        dict(row) for row in seal.ABANDONED_PREREGISTRATION_AUDIT
    ]
    assert all(
        row["attempted_cells_increment"] == 0
        and row["eligible_observations_emitted"] is False
        and row["manifest_entries_emitted"] == 0
        for row in payload["abandoned_preregistrations"]
    )
    t0 = seal._parse_utc_second(
        payload["prospective_window"]["t0_utc_inclusive"], label="t0"
    )
    end = seal._parse_utc_second(
        payload["prospective_window"]["end_utc_exclusive"], label="end"
    )
    assert t0 > SEALED_AT
    assert end - t0 == timedelta(days=180)
    assert payload["prospective_window"]["early_success_forbidden"] is True
    assert payload["authority"] == seal.FIXED_AUTHORITY_FLAGS
    assert not any(payload["authority"].values())
    assert payload["execution_contract"] == {
        "entry_type": "immediate_market",
        "pending_orders_forbidden": True,
        "maximum_entries_per_symbol_utc_day": 1,
        "outcome_horizon_m1_bars": 30,
        "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
        "rollover_entry_blackout_half_open": True,
        "signals_inside_blackout_reserve": False,
    }
    eurusd = payload["cost_policy"]["symbols"]["EURUSD"]
    usdjpy = payload["cost_policy"]["symbols"]["USDJPY"]
    assert eurusd["conversion_applies"] is False
    assert usdjpy["conversion_applies"] is True
    assert usdjpy["conversion_rate_of_absolute_profit_or_loss"] == 0.005
    assert eurusd["convert_on_close_charge_fraction_for_screen"] == 0.0
    assert usdjpy["convert_on_close_charge_fraction_for_screen"] == 0.005
    assert eurusd["conversion_adjusted_break_even_win_probability"] == 0.75
    assert usdjpy["conversion_adjusted_break_even_win_probability"] > 0.75
    assert payload["cost_policy"][
        "unknown_commission_financing_or_conversion_refuses_evaluation"
    ] is True
    runtime_context = payload["source_identities"][
        "production_runtime_context"
    ]
    assert runtime_context["relationship"] == (
        "context_only_successor_not_integrated_or_authorized"
    )
    assert len(runtime_context["engine_identity"]["engine_sha256"]) == 64


def test_refuses_unknown_or_silently_zero_fee_treatment(tmp_path: Path) -> None:
    capture, npz, fee = _inputs(tmp_path)
    payload = json.loads(fee.read_text(encoding="utf-8"))
    payload["symbols"]["EURUSD"]["financing_status"] = "unknown"
    body = {
        key: value
        for key, value in payload.items()
        if key != "operator_attestation_sha256"
    }
    payload["operator_attestation_sha256"] = seal.canonical_sha256(body)
    _write_json(fee, payload)

    with pytest.raises(
        seal.PreregistrationRefusal, match="fee_symbol_invalid:EURUSD"
    ):
        seal.build_preregistration(
            cost_capture_json=capture,
            cost_capture_npz=npz,
            fee_attestation=fee,
            sealed_at=SEALED_AT,
        )


def test_refuses_changed_cost_or_source_bytes(tmp_path: Path) -> None:
    capture, npz, fee = _inputs(tmp_path)
    npz.write_bytes(npz.read_bytes() + b"tampered")
    with pytest.raises(
        seal.PreregistrationRefusal, match="cost_capture_contract_invalid"
    ):
        seal.build_preregistration(
            cost_capture_json=capture,
            cost_capture_npz=npz,
            fee_attestation=fee,
            sealed_at=SEALED_AT,
        )

    capture, npz = _cost_capture(tmp_path / "capture_2")
    fee = _fee_attestation(tmp_path / "fees_2")
    source = tmp_path / "fees_2" / "ig_mt4_forex_product_details.txt"
    source.write_text("changed after attestation\n", encoding="utf-8")
    with pytest.raises(
        seal.PreregistrationRefusal,
        match="fee_source_document_sha256_invalid",
    ):
        seal.build_preregistration(
            cost_capture_json=capture,
            cost_capture_npz=npz,
            fee_attestation=fee,
            sealed_at=SEALED_AT,
        )


def test_atomic_publish_is_content_addressed_and_refuses_overwrite(
    tmp_path: Path,
) -> None:
    capture, npz, fee = _inputs(tmp_path)
    output = tmp_path / "quarantine"
    output.mkdir()
    payload = seal.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=SEALED_AT,
    )
    target = seal.atomic_publish(
        output_root=output,
        payload=payload,
        input_paths=(capture, npz, fee),
    )
    try:
        assert payload["preregistration_body_sha256"] in target.name
        assert json.loads(target.read_text(encoding="utf-8")) == payload
        assert sorted(output.iterdir()) == [target]
        with pytest.raises(
            seal.PreregistrationRefusal, match="output_already_exists"
        ):
            seal.atomic_publish(
                output_root=output,
                payload=payload,
                input_paths=(capture, npz, fee),
            )
    finally:
        os.chmod(target, 0o600)


def test_refuses_backdated_costs_future_attestation_and_repo_output(
    tmp_path: Path,
) -> None:
    capture, npz, fee = _inputs(tmp_path)
    cost = json.loads(capture.read_text(encoding="utf-8"))
    cost["capture_end_epoch"] = SEALED_AT.timestamp() + 1.0
    body = {
        key: value
        for key, value in cost.items()
        if key != "capture_payload_sha256"
    }
    cost["capture_payload_sha256"] = seal.canonical_sha256(body)
    _write_json(capture, cost)
    with pytest.raises(
        seal.PreregistrationRefusal, match="cost_capture_contract_invalid"
    ):
        seal.build_preregistration(
            cost_capture_json=capture,
            cost_capture_npz=npz,
            fee_attestation=fee,
            sealed_at=SEALED_AT,
        )

    capture, npz = _cost_capture(tmp_path / "capture_3")
    payload = seal.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=SEALED_AT,
    )
    with pytest.raises(
        seal.PreregistrationRefusal,
        match="output_root_inside_repository_forbidden",
    ):
        seal.atomic_publish(
            output_root=REPO_ROOT,
            payload=payload,
            input_paths=(capture, npz, fee),
        )


def test_tool_has_no_network_credential_issuer_or_order_surface() -> None:
    source = TOOL_PATH.read_text(encoding="utf-8")
    forbidden = (
        "urlopen(",
        "requests.",
        "psycopg",
        "sqlalchemy",
        "Ed25519",
        "private_key",
        "certificate",
        "/v2/",
        "order_send",
        "command_queue",
    )
    assert all(token not in source for token in forbidden)
