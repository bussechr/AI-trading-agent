from __future__ import annotations

from pathlib import Path
import sqlite3

from tools import capture_ig_scalp_cost_model as capture
from tools import check_ig_tick_history_readiness as readiness


def _source() -> capture.AuthenticatedMarketSource:
    return capture.AuthenticatedMarketSource(
        broker_account_scope="scope",
        broker_venue_id="ig_mt4",
        producer_identity="consumer",
        producer_instance_id="producer",
        terminal_lease_scope="lease",
        credential_generation_id="generation",
        bridge_protocol_version="v3.0.0",
    )


def test_readiness_uses_shortest_exact_scope_symbol_and_has_no_authority() -> None:
    source = _source()
    full = readiness.REQUIRED_DURATION_SECS
    aggregates = {
        symbol: {
            "observations": 100,
            "first_sequence": index * 100 + 1,
            "last_sequence": index * 100 + 100,
            "first_quote_epoch": 1_700_000_000.0,
            "last_quote_epoch": 1_700_000_000.0 + full,
        }
        for index, symbol in enumerate(capture.IG_MT4_SCALP_SYMBOLS)
    }
    aggregates["AUDCAD"] = {
        **aggregates["AUDCAD"],
        "last_quote_epoch": 1_700_000_000.0 + full - 1.0,
    }

    payload = readiness._build_readiness_payload(
        aggregates=aggregates,
        source=source,
        observed_at_epoch=1_703_000_000.0,
    )

    assert payload["minimum_symbol_duration_secs"] == full - 1.0
    assert payload["ready_symbol_count"] == len(capture.IG_MT4_SCALP_SYMBOLS) - 1
    assert payload["exact_scope_ready"] is False
    assert payload["symbols"]["AUDCAD"]["ready"] is False
    assert payload["research_authorized"] is False
    assert payload["selection_authorized"] is False
    assert payload["activation_authorized"] is False
    assert payload["order_authorized"] is False


def test_readiness_aggregate_query_is_exact_source_and_read_only(tmp_path: Path) -> None:
    source = _source()
    database = tmp_path / "runtime.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE market_ticks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
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
                bridge_protocol_version TEXT
            )
            """
        )
        fields = source.to_fields()
        for symbol in capture.IG_MT4_SCALP_SYMBOLS:
            for epoch in (100.0, 200.0):
                connection.execute(
                    """
                    INSERT INTO market_ticks (
                        symbol, ts, market_source_schema, market_source_id,
                        market_source_authenticated, broker_account_scope,
                        broker_venue_id, producer_identity, producer_instance_id,
                        terminal_lease_scope, credential_generation_id,
                        bridge_protocol_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        symbol,
                        epoch,
                        fields["market_source_schema"],
                        fields["market_source_id"],
                        1,
                        fields["broker_account_scope"],
                        fields["broker_venue_id"],
                        fields["producer_identity"],
                        fields["producer_instance_id"],
                        fields["terminal_lease_scope"],
                        fields["credential_generation_id"],
                        fields["bridge_protocol_version"],
                    ),
                )
        connection.execute(
            """
            INSERT INTO market_ticks (
                symbol, ts, market_source_schema, market_source_id,
                market_source_authenticated, broker_account_scope,
                broker_venue_id, producer_identity, producer_instance_id,
                terminal_lease_scope, credential_generation_id,
                bridge_protocol_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "EURUSD",
                999.0,
                fields["market_source_schema"],
                "0" * 64,
                1,
                fields["broker_account_scope"],
                fields["broker_venue_id"],
                fields["producer_identity"],
                fields["producer_instance_id"],
                fields["terminal_lease_scope"],
                fields["credential_generation_id"],
                fields["bridge_protocol_version"],
            ),
        )
        connection.commit()

    aggregates = readiness._read_history_aggregates(
        database_url=f"sqlite:///{database.as_posix()}",
        source=source,
    )

    assert set(aggregates) == set(capture.IG_MT4_SCALP_SYMBOLS)
    assert all(row["observations"] == 2 for row in aggregates.values())
    assert aggregates["EURUSD"]["last_quote_epoch"] == 200.0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM market_ticks").fetchone()[0] == (
            len(capture.IG_MT4_SCALP_SYMBOLS) * 2 + 1
        )
