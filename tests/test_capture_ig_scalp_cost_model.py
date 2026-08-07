from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.market_source_identity import AuthenticatedMarketSource
from tools import capture_ig_scalp_cost_model as capture


def _source() -> AuthenticatedMarketSource:
    return AuthenticatedMarketSource(
        broker_account_scope="private-demo-account-scope",
        broker_venue_id="ig_mt4",
        producer_identity="terminal-consumer",
        producer_instance_id="terminal-instance",
        terminal_lease_scope="terminal-lease",
        credential_generation_id="credential-generation",
        bridge_protocol_version="v3.0.0",
    )


def _specs() -> dict[str, capture.BrokerSpec]:
    return {
        symbol: capture.BrokerSpec(
            point=0.00001,
            price_tick_size=0.00001,
            digits=5,
            trade_allowed=True,
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }


def _samples() -> dict[str, list[capture.QuoteSample]]:
    rows: dict[str, list[capture.QuoteSample]] = {}
    for symbol_index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
        symbol_rows: list[capture.QuoteSample] = []
        for offset in range(2):
            token_hash = hashlib.sha256(
                f"{symbol}-event-{offset}".encode("ascii")
            ).hexdigest().encode("ascii")
            symbol_rows.append(
                capture.QuoteSample(
                    symbol_index=symbol_index,
                    sample_epoch=101.0 + offset,
                    broker_quote_epoch=100.0 + offset,
                    received_at_epoch=100.5 + offset,
                    market_event_received_at_epoch=100.5 + offset,
                    source_event_sequence=10 + offset,
                    source_event_token_sha256=token_hash,
                    bid=1.10000 + offset * 0.00001,
                    ask=1.10002 + offset * 0.00001,
                    point=0.00001,
                    price_tick_size=0.00001,
                    digits=5,
                    trade_allowed=True,
                )
            )
        rows[symbol] = symbol_rows
    return rows


def _audit_chain(epoch: float) -> capture.AuditChain:
    audit = capture.AuditChain()
    audit.observe({"safe": True}, observed_at=epoch)
    return audit


def test_bridge_client_restricts_credentials_to_loopback_get_routes():
    with pytest.raises(capture.CaptureRefusal, match="strict_loopback"):
        capture.BridgeReadClient(
            base_url="https://example.test:58710",
            api_key="secret",
            timeout_secs=1.0,
        )

    client = capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key="secret",
        timeout_secs=1.0,
    )
    with pytest.raises(capture.CaptureRefusal, match="read_path_forbidden"):
        client.get("/v2/commands")


def test_quote_sample_requires_fresh_unique_authenticated_event():
    source = _source()
    row = {
        "symbol": "EURUSD",
        "bid": 1.1,
        "ask": 1.10002,
        "ts_epoch": 100.0,
        "received_at_epoch": 100.5,
        "market_event_received_at_epoch": 100.5,
        "market_event_sequence": 7,
        "source_event_last_token": "100",
        "source_event_baseline_initialized": True,
        "transport_fresh": True,
        "market_event_identity_present": True,
        "market_event_fresh": True,
        **source.to_fields(),
    }
    spec = _specs()["EURUSD"]
    sample = capture._quote_sample(
        symbol="EURUSD",
        symbol_index=0,
        row=row,
        spec=spec,
        source=source,
        sample_epoch=101.0,
        previous_sequence=None,
    )
    assert sample is not None
    assert sample.source_event_token_sha256 == hashlib.sha256(b"100").hexdigest().encode("ascii")
    assert (
        capture._quote_sample(
            symbol="EURUSD",
            symbol_index=0,
            row=row,
            spec=spec,
            source=source,
            sample_epoch=101.1,
            previous_sequence=7,
        )
        is None
    )

    bad = dict(row)
    bad["market_source_id"] = "0" * 64
    with pytest.raises(capture.CaptureRefusal, match="source_invalid"):
        capture._quote_sample(
            symbol="EURUSD",
            symbol_index=0,
            row=bad,
            spec=spec,
            source=source,
            sample_epoch=101.0,
            previous_sequence=None,
        )


def test_atomic_capture_is_portable_hashed_and_contains_no_plaintext_identity(
    tmp_path: Path,
):
    source = _source()
    policy = capture.CapturePolicy(
        minimum_samples_per_symbol=2,
        minimum_duration_secs=1.0,
        maximum_sample_gap_secs=2.0,
        capture_timeout_secs=5.0,
        poll_interval_secs=0.1,
        identity_recheck_secs=1.0,
        specs_recheck_secs=1.0,
    )
    output = capture.atomic_emit_capture(
        output_dir=tmp_path / "capture",
        samples=_samples(),
        specs=_specs(),
        source=source,
        identity_audit=_audit_chain(100.0),
        contract_audit=_audit_chain(100.0),
        policy=policy,
        capture_start_epoch=99.0,
        capture_end_epoch=103.0,
        created_at_epoch=104.0,
    )

    raw_json = (output / capture.CAPTURE_FILENAME).read_text(encoding="utf-8")
    payload = json.loads(raw_json)
    assert "private-demo-account-scope" not in raw_json
    assert "terminal-instance" not in raw_json
    assert payload["symbol_scope"] == list(IG_MT4_SCALP_SYMBOLS)
    assert payload["execution_contract"] == {
        "schema_version": capture.EXECUTION_CONTRACT_SCHEMA,
        "max_slippage_points": 20,
        "semantics": capture.EXECUTION_TOLERANCE_SEMANTICS,
        "used_as_observed_cost": False,
    }
    expected_payload_hash = payload.pop("capture_payload_sha256")
    assert capture.canonical_sha256(payload) == expected_payload_hash

    npz_path = output / payload["npz_path"]
    assert capture._sha256_file(npz_path) == payload["npz_sha256"]
    with np.load(npz_path, allow_pickle=False) as arrays:
        assert set(arrays.files) == set(capture.NPZ_ARRAY_DTYPES)
        assert arrays["source_event_token_sha256"].dtype == np.dtype("S64")
        assert arrays["trade_allowed"].dtype == np.dtype("bool")
        assert arrays["symbol_index"].shape == (len(IG_MT4_SCALP_SYMBOLS) * 2,)

    with pytest.raises(capture.CaptureRefusal, match="already_exists"):
        capture.atomic_emit_capture(
            output_dir=output,
            samples=_samples(),
            specs=_specs(),
            source=source,
            identity_audit=_audit_chain(100.0),
            contract_audit=_audit_chain(100.0),
            policy=policy,
            capture_start_epoch=99.0,
            capture_end_epoch=103.0,
            created_at_epoch=104.0,
        )


def test_db_history_reads_only_same_authenticated_source_and_discloses_gaps(
    tmp_path: Path,
):
    source = _source()
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                bid REAL,
                ask REAL,
                ts REAL NOT NULL,
                market_source_schema TEXT,
                market_source_id TEXT,
                market_source_authenticated INTEGER NOT NULL,
                broker_account_scope TEXT,
                broker_venue_id TEXT,
                producer_identity TEXT,
                producer_instance_id TEXT,
                terminal_lease_scope TEXT,
                credential_generation_id TEXT,
                bridge_protocol_version TEXT,
                raw_json TEXT
            )
            """
        )
        for symbol in IG_MT4_SCALP_SYMBOLS:
            for offset in range(3):
                received_at = 100.0 + offset * 10.0
                raw_json = json.dumps(
                    {
                        "raw": {
                            "source_event_token": f"{symbol}-{offset}",
                            "received_at_epoch": received_at,
                        }
                    }
                )
                connection.execute(
                    """
                    INSERT INTO market_ticks (
                        symbol, bid, ask, ts, market_source_schema,
                        market_source_id, market_source_authenticated,
                        broker_account_scope, broker_venue_id, producer_identity,
                        producer_instance_id, terminal_lease_scope,
                        credential_generation_id, bridge_protocol_version, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        1.1,
                        1.10002,
                        received_at,
                        source.to_fields()["market_source_schema"],
                        source.source_id,
                        1,
                        source.broker_account_scope,
                        source.broker_venue_id,
                        source.producer_identity,
                        source.producer_instance_id,
                        source.terminal_lease_scope,
                        source.credential_generation_id,
                        source.bridge_protocol_version,
                        raw_json,
                    ),
                )
        connection.commit()

    policy = capture.CapturePolicy(
        minimum_samples_per_symbol=2,
        minimum_duration_secs=5.0,
        maximum_sample_gap_secs=1.0,
        capture_timeout_secs=10.0,
        poll_interval_secs=0.1,
        identity_recheck_secs=1.0,
        specs_recheck_secs=1.0,
        enforce_maximum_sample_gap=False,
    )
    samples = capture._read_same_source_history(
        database_url=f"sqlite:///{database.as_posix()}",
        source=source,
        specs=_specs(),
        policy=policy,
    )
    assert set(samples) == set(IG_MT4_SCALP_SYMBOLS)
    assert all(len(rows) == 2 for rows in samples.values())
    audit = capture._point_in_time_audit(
        samples,
        policy,
        capture_mode="authenticated_same_source_db_history",
        latest_scope_market_event_fresh=True,
    )
    assert audit["passed"] is True
    assert audit["maximum_sample_gap_enforced"] is False
    assert audit["symbols"]["EURUSD"]["max_intersample_gap_secs"] == 10.0

    full_samples = capture._read_same_source_history(
        database_url=f"sqlite:///{database.as_posix()}",
        source=source,
        specs=_specs(),
        policy=policy,
        full_span=True,
        max_rows_per_symbol=3,
    )
    assert all(len(rows) == 3 for rows in full_samples.values())
    full_audit = capture._point_in_time_audit(
        full_samples,
        policy,
        capture_mode=capture.FULL_HISTORY_CAPTURE_MODE,
        latest_scope_market_event_fresh=True,
    )
    assert full_audit["passed"] is True
    assert full_audit["database_read_only"] is True
    assert full_audit["history_scope_complete"] is True
    assert full_audit["history_selection"] == (
        "complete_repeatable_read_current_source"
    )

    with pytest.raises(capture.CaptureRefusal, match="full_row_limit_exceeded"):
        capture._read_same_source_history(
            database_url=f"sqlite:///{database.as_posix()}",
            source=source,
            specs=_specs(),
            policy=policy,
            full_span=True,
            max_rows_per_symbol=2,
        )
    with sqlite3.connect(database) as connection:
        count = connection.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0]
    assert count == len(IG_MT4_SCALP_SYMBOLS) * 3
