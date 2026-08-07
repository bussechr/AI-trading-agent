from __future__ import annotations

from collections import deque
import copy
from datetime import UTC, datetime
import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

import pytest

from tools import capture_ig_mt4_m1_activity as capture


NOW = 2_000_000_100.75
API_KEY = "collector-test-api-key-secret"


def _binding(
    *,
    body_sha256: str = "1" * 64,
    artifact_sha256: str = "2" * 64,
    t0_epoch: float = NOW - 3_600.0,
    end_epoch: float | None = None,
) -> capture.ProspectiveBinding:
    resolved_end = (
        t0_epoch + capture.PROSPECTIVE_WINDOW_DAYS * 86_400.0
        if end_epoch is None
        else end_epoch
    )
    return capture.ProspectiveBinding(
        preregistration_body_sha256=body_sha256,
        preregistration_artifact_sha256=artifact_sha256,
        t0_utc=datetime.fromtimestamp(t0_epoch, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        end_utc_exclusive=datetime.fromtimestamp(resolved_end, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        t0_epoch=t0_epoch,
        end_epoch_exclusive=resolved_end,
    )


class ManualClock:
    def __init__(self, now: float = NOW) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now


class FakeBridgeTransport:
    """In-memory bridge script; it must never fall through to real HTTP."""

    def __init__(
        self,
        *,
        states: Sequence[Mapping[str, Any]] = (),
        ticks: Sequence[Mapping[str, Any]] = (),
        bar_rounds: Sequence[Mapping[str, Mapping[str, Any]]] = (),
        api_key: str = API_KEY,
    ) -> None:
        self.states = deque(copy.deepcopy(list(states)))
        self.ticks = deque(copy.deepcopy(list(ticks)))
        self.bar_rounds = copy.deepcopy(list(bar_rounds))
        self.api_key = api_key
        self.bar_calls = 0
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        request: Request,
        timeout_secs: float,
        maximum_bytes: int,
    ) -> tuple[int, bytes]:
        del timeout_secs, maximum_bytes
        parsed = urlsplit(request.full_url)
        headers = {key.lower(): value for key, value in request.header_items()}
        authenticated = headers.get("x-api-key") == self.api_key
        self.calls.append(
            {
                "method": request.get_method(),
                "scheme": parsed.scheme,
                "host": parsed.hostname,
                "path": parsed.path,
                "query": parse_qs(parsed.query),
                "authenticated": authenticated,
            }
        )
        assert request.get_method() == "GET"
        assert parsed.scheme == "http"
        assert parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        if not authenticated:
            return 401, b'{"detail":"Invalid or missing API key"}'
        if parsed.path == "/v2/state":
            assert self.states, "unexpected authenticated state GET"
            return 200, _json_bytes(self.states.popleft())
        if parsed.path == "/v2/market/ticks":
            assert self.ticks, "unexpected authenticated tick GET"
            return 200, _json_bytes(self.ticks.popleft())
        if parsed.path == "/v2/market/bars":
            query = parse_qs(parsed.query)
            assert set(query) == {"symbol", "timeframe", "limit"}
            assert query["timeframe"] == [capture.TIMEFRAME]
            assert int(query["limit"][0]) >= capture.MINIMUM_M1_BARS
            round_index, symbol_index = divmod(
                self.bar_calls,
                len(capture.SYMBOLS),
            )
            assert round_index < len(self.bar_rounds), "unexpected bar round"
            expected_symbol = capture.SYMBOLS[symbol_index]
            assert query["symbol"] == [expected_symbol]
            self.bar_calls += 1
            return 200, _json_bytes(self.bar_rounds[round_index][expected_symbol])
        raise AssertionError(f"collector attempted forbidden path {parsed.path!r}")


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _source(*, suffix: str = "a") -> capture.SourceIdentity:
    return capture.SourceIdentity(
        broker_account_scope=f"private-demo-account-scope-{suffix}",
        broker_venue_id=capture.VENUE_ID,
        producer_identity=f"terminal-consumer-{suffix}",
        producer_instance_id=f"terminal-instance-{suffix}",
        terminal_lease_scope=f"terminal-lease-{suffix}",
        credential_generation_id=f"credential-generation-{suffix}",
        bridge_protocol_version="v3.0.0",
    )


def _source_fields(source: capture.SourceIdentity) -> dict[str, Any]:
    return {
        "market_source_schema": capture.MARKET_SOURCE_SCHEMA,
        "market_source_id": source.source_id,
        "market_source_authenticated": True,
        "broker_account_scope": source.broker_account_scope,
        "broker_venue_id": source.broker_venue_id,
        "producer_identity": source.producer_identity,
        "producer_instance_id": source.producer_instance_id,
        "terminal_lease_scope": source.terminal_lease_scope,
        "credential_generation_id": source.credential_generation_id,
        "bridge_protocol_version": source.bridge_protocol_version,
    }


def _state(
    source: capture.SourceIdentity,
    *,
    now: float = NOW,
) -> dict[str, Any]:
    readiness = {
        symbol: {
            "broker_symbol": symbol,
            "supported": True,
            "selected": True,
            "mapping_ambiguous": False,
            "mapping_reason": "exact",
            "mapping_kind": "exact",
            "mapping_candidate_count": 1,
        }
        for symbol in capture.SYMBOLS
    }
    source_fields = _source_fields(source)
    return {
        "system_status": "connected",
        "database_ok": True,
        "last_heartbeat": datetime.fromtimestamp(now - 0.25, tz=UTC).isoformat(),
        "heartbeat_age_secs": 0.25,
        "heartbeat_stale_after_secs": 30.0,
        "broker_account_mode": "demo",
        "broker_account_scope": source.broker_account_scope,
        "broker_account_scope_schema": "fxstack_mt4_account_scope_djb2_xor32_v1",
        "broker_account_scope_version": 1,
        "broker_venue_id": source.broker_venue_id,
        "bridge_producer_identity": source.producer_identity,
        "bridge_producer_instance_id": source.producer_instance_id,
        "bridge_terminal_lease_scope": source.terminal_lease_scope,
        "bridge_credential_generation_id": source.credential_generation_id,
        "bridge_protocol_version": source.bridge_protocol_version,
        "bridge_consumer_lease": {
            "schema_version": "fxstack_bridge_consumer_lease_v2",
            "consumer_identity": source.producer_identity,
            "producer_instance_id": source.producer_instance_id,
            "terminal_lease_scope": source.terminal_lease_scope,
            "credential_generation_id": source.credential_generation_id,
            "bridge_protocol_version": source.bridge_protocol_version,
            "expires_at": now + 60.0,
        },
        "bridge_market_source": dict(source_fields),
        "bridge_status_market_source": dict(source_fields),
        "bridge_status_market_source_id": source.source_id,
        "configured_pairs": list(capture.SYMBOLS),
        "symbol_ready_count": len(capture.SYMBOLS),
        "symbol_readiness": readiness,
        "unsupported_pairs": [],
    }


def _ticks(
    source: capture.SourceIdentity,
    *,
    received_at: float,
    token_epoch: int,
    event_sequence: int = 7,
    market_event_fresh: bool = False,
) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for index, symbol in enumerate(capture.SYMBOLS):
        bid = 1.10000 + index * 0.00001
        ask = bid + 0.00002
        rows[symbol] = {
            "symbol": symbol,
            "broker_symbol": symbol,
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0,
            # Current BridgeEA does not send `time`; bridge `ts_epoch` is its
            # receipt time. MODE_TIME is carried only as an opaque comparable
            # source_event_token, not as a UTC epoch.
            "ts_epoch": received_at,
            "received_at_epoch": received_at,
            "transport_age_secs": max(0.0, NOW - received_at),
            "transport_fresh": True,
            "source_event_token": str(token_epoch),
            "source_event_last_token": str(token_epoch),
            "source_event_baseline_initialized": True,
            "market_event_identity_present": True,
            "market_event_received_at_epoch": (
                received_at if event_sequence > 0 else None
            ),
            "market_event_sequence": event_sequence,
            "market_event_fresh": market_event_fresh,
            "market_event_reason": (
                "ok" if market_event_fresh else "broker_market_event_stale"
            ),
            **_source_fields(source),
        }
    return rows


def _bar_rows(
    source: capture.SourceIdentity,
    *,
    count: int = capture.MINIMUM_M1_BARS,
    observed_at: float = NOW,
) -> list[dict[str, Any]]:
    latest = ((int(observed_at) - 120) // 60) * 60
    first = latest - (count - 1) * 60
    rows: list[dict[str, Any]] = []
    for index in range(count):
        epoch = first + index * 60
        bid_open = 1.10000 + index * 0.000001
        bid_close = bid_open + 0.00001
        rows.append(
            {
                "time": datetime.fromtimestamp(epoch, tz=UTC).isoformat(),
                "open": bid_open + 0.00001,
                "high": bid_close + 0.00002,
                "low": bid_open - 0.00002,
                "close": bid_close + 0.00001,
                "bid_open": bid_open,
                "bid_high": bid_close + 0.00001,
                "bid_low": bid_open - 0.00001,
                "bid_close": bid_close,
                "spread": 0.00002,
                "volume": 100 + index,
                "volume_source": capture.VOLUME_SOURCE,
                "price_basis": capture.PRICE_BASIS,
                "source_timeframe": capture.TIMEFRAME,
                **_source_fields(source),
            }
        )
    return rows


def _bar_payload(
    source: capture.SourceIdentity,
    symbol: str,
    *,
    count: int = capture.MINIMUM_M1_BARS,
    observed_at: float = NOW,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": capture.TIMEFRAME,
        "limit": capture.DEFAULT_BAR_LIMIT,
        "bars": _bar_rows(source, count=count, observed_at=observed_at),
    }


def _bar_round(
    source: capture.SourceIdentity,
    *,
    count: int = capture.MINIMUM_M1_BARS,
    observed_at: float = NOW,
) -> dict[str, dict[str, Any]]:
    return {
        symbol: _bar_payload(
            source,
            symbol,
            count=count,
            observed_at=observed_at,
        )
        for symbol in capture.SYMBOLS
    }


def _client(transport: FakeBridgeTransport) -> capture.BridgeReadClient:
    return capture.BridgeReadClient(
        base_url="http://127.0.0.1:58710",
        api_key=API_KEY,
        timeout_secs=1.0,
        transport=transport,
    )


def _collector(
    tmp_path: Path,
    transport: FakeBridgeTransport,
    *,
    clock: ManualClock | None = None,
    rollover_mode: str = "refuse",
    binding: capture.ProspectiveBinding | None = None,
) -> tuple[capture.ProspectiveActivityCollector, capture.ManifestLedger, ManualClock]:
    active_clock = clock or ManualClock()
    ledger = capture.ManifestLedger(tmp_path)
    policy = capture.CollectionPolicy(
        bar_limit=400,
        tick_interval_secs=5.0,
        bar_interval_secs=60.0,
        rollover_mode=rollover_mode,
    )
    return (
        capture.ProspectiveActivityCollector(
            client=_client(transport),
            ledger=ledger,
            binding=binding or _binding(),
            policy=policy,
            clock=active_clock,
        ),
        ledger,
        active_clock,
    )


def _chunk(ledger: capture.ManifestLedger, index: int = -1) -> dict[str, Any]:
    entry = ledger.entries[index]
    return json.loads((ledger.root / entry["chunk_path"]).read_text(encoding="utf-8"))


def _preregistration_payload() -> dict[str, Any]:
    sealed = datetime.fromtimestamp(math.floor(NOW) - 120, tz=UTC)
    t0 = sealed.timestamp() + 60
    end = t0 + capture.PROSPECTIVE_WINDOW_DAYS * 86_400
    body: dict[str, Any] = {
        "schema_version": capture.PREREGISTRATION_SCHEMA_VERSION,
        "research_only": True,
        "authority": dict(capture._FIXED_FALSE_AUTHORITY),
        "sealed_at_utc": sealed.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scope": {
            "scope_version": capture.SCOPE_VERSION,
            "venue_id": capture.VENUE_ID,
            "ordered_symbols": list(capture.SYMBOLS),
            "cell_order": [
                {"config_id": "fixed-test-config", "symbol": symbol, "side": side}
                for symbol in capture.SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "strategy": {
            "source_contract_id": capture.SOURCE_CONTRACT_ID,
            "activity_metric_id": capture.ACTIVITY_METRIC_ID,
        },
        "execution_contract": {
            "entry_type": "immediate_market",
            "pending_orders_forbidden": True,
            "maximum_entries_per_symbol_utc_day": 1,
        },
        "prospective_window": {
            "consecutive_days": capture.PROSPECTIVE_WINDOW_DAYS,
            "fixed_before_any_eligible_observation": True,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_or_outcome_evaluation_forbidden": True,
            "t0_utc_inclusive": datetime.fromtimestamp(t0, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "end_utc_exclusive": datetime.fromtimestamp(end, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
        "source_identities": {
            "collector_source": {
                "sha256": hashlib_sha256(capture.TOOL_PATH.read_bytes()),
            }
        },
    }
    return {**body, "preregistration_body_sha256": capture.canonical_sha256(body)}


def _write_preregistration(path: Path, payload: Mapping[str, Any]) -> Path:
    path.write_text(
        json.dumps(payload, allow_nan=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def test_preregistration_binding_validates_body_source_scope_and_window(
    tmp_path: Path,
) -> None:
    payload = _preregistration_payload()
    path = _write_preregistration(tmp_path / "prereg.json", payload)

    binding = capture.load_preregistration(path)

    assert binding.preregistration_body_sha256 == payload["preregistration_body_sha256"]
    assert binding.preregistration_artifact_sha256 == hashlib_sha256(path.read_bytes())
    assert binding.end_epoch_exclusive - binding.t0_epoch == 180 * 86_400

    tampered = copy.deepcopy(payload)
    tampered["execution_contract"]["pending_orders_forbidden"] = False
    tampered_path = _write_preregistration(tmp_path / "tampered.json", tampered)
    with pytest.raises(capture.CollectionRefusal, match="body_hash_invalid"):
        capture.load_preregistration(tampered_path)

    wrong_source = copy.deepcopy(payload)
    wrong_source["source_identities"]["collector_source"]["sha256"] = "f" * 64
    unsigned = {
        key: value
        for key, value in wrong_source.items()
        if key != "preregistration_body_sha256"
    }
    wrong_source["preregistration_body_sha256"] = capture.canonical_sha256(unsigned)
    wrong_path = _write_preregistration(tmp_path / "wrong-source.json", wrong_source)
    with pytest.raises(capture.CollectionRefusal, match="contract_invalid"):
        capture.load_preregistration(wrong_path)


def test_client_is_strict_get_only_and_proves_401_authentication() -> None:
    source = _source()
    transport = FakeBridgeTransport(states=[_state(source)])
    client = _client(transport)

    client.prove_authentication_required()
    assert client.get_state()["bridge_market_source"]["market_source_id"] == source.source_id

    calls_before = len(transport.calls)
    with pytest.raises(capture.CollectionRefusal, match="path_forbidden"):
        client._request("/v2/commands")
    with pytest.raises(capture.CollectionRefusal, match="query_invalid"):
        client.get_m1_bars("XRPUSD", limit=400)
    assert len(transport.calls) == calls_before
    assert [call["authenticated"] for call in transport.calls] == [False, True]
    assert all(call["method"] == "GET" for call in transport.calls)


def test_capture_cycle_refuses_outside_bound_window_before_any_get(
    tmp_path: Path,
) -> None:
    source = _source()
    before_transport = FakeBridgeTransport(
        states=[_state(source)],
        ticks=[
            _ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) + 3_600,
            )
        ],
    )
    before, _ledger, _clock = _collector(
        tmp_path / "before",
        before_transport,
        binding=_binding(t0_epoch=NOW + 10.0),
    )
    with pytest.raises(capture.CollectionRefusal, match="window_not_started"):
        before.capture_cycle(include_bars=False)
    assert before_transport.calls == []

    near_end_binding = _binding(
        t0_epoch=NOW - capture.PROSPECTIVE_WINDOW_DAYS * 86_400 + 2.0,
        end_epoch=NOW + 2.0,
    )
    near_end_transport = FakeBridgeTransport(
        states=[_state(source)],
        ticks=[
            _ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) + 3_600,
            )
        ],
    )
    near_end, _ledger, _clock = _collector(
        tmp_path / "near-end",
        near_end_transport,
        binding=near_end_binding,
    )
    with pytest.raises(capture.CollectionRefusal, match="deadline_insufficient"):
        near_end.capture_cycle(include_bars=False)
    assert near_end_transport.calls == []


@pytest.mark.parametrize(
    "case",
    [
        "account_not_demo",
        "venue_not_ig",
        "system_not_connected",
        "heartbeat_stale",
        "account_scope_schema_wrong",
        "scope_reordered",
        "symbol_not_ready",
        "bridge_status_source_wrong",
        "market_source_id_forged",
        "top_level_identity_wrong",
        "lease_identity_wrong",
        "lease_expired",
    ],
)
def test_state_requires_exact_fresh_authenticated_source_and_scope(case: str) -> None:
    source = _source()
    state = _state(source)
    if case == "account_not_demo":
        state["broker_account_mode"] = "real"
    elif case == "venue_not_ig":
        state["broker_venue_id"] = "other"
    elif case == "system_not_connected":
        state["system_status"] = "stale"
    elif case == "heartbeat_stale":
        state["heartbeat_age_secs"] = 31.0
    elif case == "account_scope_schema_wrong":
        state["broker_account_scope_schema"] = "legacy"
    elif case == "scope_reordered":
        state["configured_pairs"] = list(reversed(capture.SYMBOLS))
    elif case == "symbol_not_ready":
        state["symbol_readiness"]["EURUSD"]["selected"] = False
    elif case == "bridge_status_source_wrong":
        state["bridge_status_market_source"]["market_source_id"] = "0" * 64
    elif case == "market_source_id_forged":
        state["bridge_market_source"]["market_source_id"] = "0" * 64
    elif case == "top_level_identity_wrong":
        state["bridge_producer_instance_id"] = "other-instance"
    elif case == "lease_identity_wrong":
        state["bridge_consumer_lease"]["consumer_identity"] = "other-consumer"
    elif case == "lease_expired":
        state["bridge_consumer_lease"]["expires_at"] = NOW
    else:  # pragma: no cover - parameter list is exhaustive
        raise AssertionError(case)

    with pytest.raises(capture.CollectionRefusal):
        capture.source_from_state(state, observed_at_epoch=NOW)


def test_exact_direct_241_m1_bars_preserve_integer_ivolume() -> None:
    source = _source()
    payload = _bar_payload(source, "EURUSD")

    bars = capture.validate_m1_bar_response(
        payload,
        symbol="EURUSD",
        expected_source=source,
        observed_at_epoch=NOW,
    )

    assert len(bars) == capture.MINIMUM_M1_BARS
    assert all(isinstance(bar.tick_volume, int) for bar in bars)
    assert all(not isinstance(bar.tick_volume, bool) for bar in bars)
    assert all(bar.price_basis == capture.PRICE_BASIS for bar in bars)
    assert all(bar.volume_source == capture.VOLUME_SOURCE for bar in bars)
    assert all(
        right.minute_epoch == left.minute_epoch + 60
        for left, right in zip(bars, bars[1:])
    )


@pytest.mark.parametrize("invalid_volume", [100.0, 100.5, True, -1])
def test_direct_bar_rejects_non_integer_or_negative_ivolume(
    invalid_volume: Any,
) -> None:
    source = _source()
    payload = _bar_payload(source, "EURUSD")
    payload["bars"][10]["volume"] = invalid_volume

    with pytest.raises(capture.CollectionRefusal, match="volume"):
        capture.validate_m1_bar_response(
            payload,
            symbol="EURUSD",
            expected_source=source,
            observed_at_epoch=NOW,
        )


def test_bar_provenance_is_filtered_before_minimum_direct_row_check() -> None:
    source = _source()
    enough = _bar_payload(
        source,
        "EURUSD",
        count=capture.MINIMUM_M1_BARS + 1,
    )
    enough["bars"][0]["price_basis"] = "bridge_tick_mid_ohlc_v1"
    enough["bars"][0]["volume_source"] = "bridge_market_event_count_v1"

    bars = capture.validate_m1_bar_response(
        enough,
        symbol="EURUSD",
        expected_source=source,
        observed_at_epoch=NOW,
    )
    assert len(bars) == capture.MINIMUM_M1_BARS
    assert all(bar.price_basis == capture.PRICE_BASIS for bar in bars)

    insufficient = _bar_payload(source, "EURUSD")
    insufficient["bars"][0]["price_basis"] = "bridge_tick_mid_ohlc_v1"
    insufficient["bars"][0]["volume_source"] = "bridge_market_event_count_v1"
    with pytest.raises(capture.CollectionRefusal, match="insufficient"):
        capture.validate_m1_bar_response(
            insufficient,
            symbol="EURUSD",
            expected_source=source,
            observed_at_epoch=NOW,
        )


def test_tick_dedup_uses_source_received_at_and_retains_quiet_transport(
    tmp_path: Path,
) -> None:
    source = _source()
    first_receipt = NOW - 1.50
    next_receipt = first_receipt + 1.0
    token_epoch = int(NOW) - 20
    transport = FakeBridgeTransport(
        states=[_state(source)] * 6,
        ticks=[
            _ticks(
                source,
                received_at=first_receipt,
                token_epoch=token_epoch,
                market_event_fresh=False,
            ),
            _ticks(
                source,
                received_at=first_receipt,
                token_epoch=token_epoch,
                market_event_fresh=False,
            ),
            _ticks(
                source,
                received_at=next_receipt,
                token_epoch=token_epoch,
                market_event_fresh=False,
            ),
        ],
    )
    collector, ledger, clock = _collector(tmp_path, transport)

    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    duplicate_result = collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)

    assert duplicate_result is None
    assert [len(_chunk(ledger, index)["quotes"]) for index in range(2)] == [
        len(capture.SYMBOLS),
        len(capture.SYMBOLS),
    ]
    first = _chunk(ledger, 0)["quotes"][0]
    third = _chunk(ledger, 1)["quotes"][0]
    assert first["observation_epoch"] == math.floor(first_receipt)
    assert third["observation_epoch"] == math.floor(next_receipt)
    assert "broker_event_epoch" not in first
    assert "broker_event_epoch" not in third
    assert first["source_event_token_sha256"] == third["source_event_token_sha256"]


def test_api_local_market_event_sequence_reset_is_audited_not_used_as_clock(
    tmp_path: Path,
) -> None:
    source = _source()
    first_receipt = NOW - 2.0
    first_token = int(NOW) - 20
    transport = FakeBridgeTransport(
        states=[_state(source)] * 4,
        ticks=[
            _ticks(
                source,
                received_at=first_receipt,
                token_epoch=first_token,
                event_sequence=7,
            ),
            _ticks(
                source,
                received_at=first_receipt + 1.0,
                token_epoch=first_token + 1,
                event_sequence=0,
            ),
        ],
    )
    collector, ledger, clock = _collector(tmp_path, transport)
    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)

    assert len(ledger.entries) == 2
    assert _chunk(ledger, 1)["quotes"][0]["market_event_sequence"] == 0


def test_same_source_resume_reconstructs_manifest_and_continues_sequence(
    tmp_path: Path,
) -> None:
    source = _source()
    first_receipt = NOW - 2.0
    first_transport = FakeBridgeTransport(
        states=[_state(source)] * 2,
        ticks=[
            _ticks(
                source,
                received_at=first_receipt,
                token_epoch=int(NOW) + 3_600,
                event_sequence=9,
            )
        ],
    )
    collector, ledger, _clock = _collector(tmp_path, first_transport)
    collector.capture_cycle(include_bars=False)

    resumed_clock = ManualClock(NOW + 1.0)
    resumed_transport = FakeBridgeTransport(
        states=[_state(source, now=resumed_clock.now)] * 2,
        ticks=[
            _ticks(
                source,
                received_at=first_receipt + 1.0,
                token_epoch=int(NOW) + 3_599,
                event_sequence=0,
            )
        ],
    )
    resumed, resumed_ledger, _clock = _collector(
        tmp_path,
        resumed_transport,
        clock=resumed_clock,
    )

    assert len(resumed_ledger.entries) == 1
    resumed.capture_cycle(include_bars=False)
    assert len(resumed_ledger.entries) == 2
    quote = _chunk(resumed_ledger, 1)["quotes"][0]
    assert quote["observation_sequence"] == 2
    assert quote["market_event_sequence"] == 0


def test_same_source_resume_is_idempotent_for_same_transport_snapshot(
    tmp_path: Path,
) -> None:
    source = _source()
    receipt = NOW - 1.0
    snapshot = _ticks(
        source,
        received_at=receipt,
        token_epoch=int(NOW) + 3_600,
        event_sequence=9,
    )
    first_transport = FakeBridgeTransport(
        states=[_state(source)] * 2,
        ticks=[snapshot],
    )
    collector, ledger, _clock = _collector(tmp_path, first_transport)
    collector.capture_cycle(include_bars=False)

    resumed_transport = FakeBridgeTransport(
        states=[_state(source, now=NOW + 1.0)] * 2,
        ticks=[snapshot],
    )
    resumed, resumed_ledger, _clock = _collector(
        tmp_path,
        resumed_transport,
        clock=ManualClock(NOW + 1.0),
    )

    assert resumed.capture_cycle(include_bars=False) is None
    assert len(resumed_ledger.entries) == 1
    assert len(ledger.entries) == 1


def test_broker_server_token_is_opaque_and_may_be_ahead_or_step_back(
    tmp_path: Path,
) -> None:
    source = _source()
    first_receipt = NOW - 2.0
    broker_server_token = int(NOW) + 3_600
    transport = FakeBridgeTransport(
        states=[_state(source)] * 4,
        ticks=[
            _ticks(
                source,
                received_at=first_receipt,
                token_epoch=broker_server_token,
                event_sequence=7,
            ),
            _ticks(
                source,
                received_at=first_receipt + 1.0,
                token_epoch=broker_server_token - 1,
                event_sequence=8,
            ),
        ],
    )
    collector, ledger, clock = _collector(tmp_path, transport)

    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    collector.capture_cycle(include_bars=False)

    assert len(ledger.entries) == 2
    first = _chunk(ledger, 0)["quotes"][0]
    second = _chunk(ledger, 1)["quotes"][0]
    assert first["source_event_token_sha256"] != second["source_event_token_sha256"]
    assert first["market_event_sequence"] == 7
    assert second["market_event_sequence"] == 8


def test_market_event_receipt_cannot_be_after_its_transport_receipt(
    tmp_path: Path,
) -> None:
    source = _source()
    ticks = _ticks(
        source,
        received_at=NOW - 1.0,
        token_epoch=int(NOW) + 3_600,
    )
    ticks["EURUSD"]["market_event_received_at_epoch"] = NOW
    transport = FakeBridgeTransport(
        states=[_state(source)],
        ticks=[ticks],
    )
    collector, ledger, _clock = _collector(tmp_path, transport)

    with pytest.raises(capture.CollectionRefusal, match="receipt_future"):
        collector.capture_cycle(include_bars=False)
    assert ledger.entries == []


def test_pre_post_state_rollover_never_emits_even_in_split_mode(
    tmp_path: Path,
) -> None:
    source_a = _source(suffix="a")
    source_b = _source(suffix="b")
    transport = FakeBridgeTransport(
        states=[_state(source_a), _state(source_b)],
        ticks=[
            _ticks(
                source_a,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 10,
            )
        ],
    )
    collector, ledger, _clock = _collector(
        tmp_path,
        transport,
        rollover_mode="split",
    )

    with pytest.raises(capture.CollectionRefusal):
        collector.capture_cycle(include_bars=False)
    assert ledger.entries == []
    assert not ledger.manifest_path.exists()
    assert list(ledger.chunks_root.rglob("*.json")) == []


def test_mixed_tick_source_refuses_without_emit(tmp_path: Path) -> None:
    source_a = _source(suffix="a")
    source_b = _source(suffix="b")
    ticks = _ticks(
        source_a,
        received_at=NOW - 1.0,
        token_epoch=int(NOW) - 10,
    )
    ticks["EURUSD"].update(_source_fields(source_b))
    transport = FakeBridgeTransport(
        states=[_state(source_a)],
        ticks=[ticks],
    )
    collector, ledger, _clock = _collector(tmp_path, transport)

    with pytest.raises(capture.CollectionRefusal):
        collector.capture_cycle(include_bars=False)
    assert ledger.entries == []


@pytest.mark.parametrize("rollover_mode", ["refuse", "split"])
def test_source_change_between_cycles_is_refused_or_split(
    tmp_path: Path,
    rollover_mode: str,
) -> None:
    source_a = _source(suffix="a")
    source_b = _source(suffix="b")
    states = (
        [_state(source_a), _state(source_a), _state(source_b)]
        if rollover_mode == "refuse"
        else [
            _state(source_a),
            _state(source_a),
            _state(source_b),
            _state(source_b),
        ]
    )
    ticks = [
        _ticks(
            source_a,
            received_at=NOW - 2.0,
            token_epoch=int(NOW) - 20,
        )
    ]
    if rollover_mode == "split":
        ticks.append(
            _ticks(
                source_b,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 19,
            )
        )
    transport = FakeBridgeTransport(states=states, ticks=ticks)
    collector, ledger, clock = _collector(
        tmp_path,
        transport,
        rollover_mode=rollover_mode,
    )

    collector.capture_cycle(include_bars=False)
    clock.now += 1.0
    if rollover_mode == "refuse":
        with pytest.raises(capture.CollectionRefusal):
            collector.capture_cycle(include_bars=False)
        assert len(ledger.entries) == 1
    else:
        collector.capture_cycle(include_bars=False)
        assert [entry["segment_index"] for entry in ledger.entries] == [1, 2]
        assert [entry["market_source_id"] for entry in ledger.entries] == [
            source_a.source_id,
            source_b.source_id,
        ]


def test_completed_bar_overlap_mutation_refuses_without_second_emit(
    tmp_path: Path,
) -> None:
    source = _source()
    first_round = _bar_round(source)
    mutated_round = copy.deepcopy(first_round)
    mutated_round["EURUSD"]["bars"][-1]["volume"] += 1
    transport = FakeBridgeTransport(
        states=[_state(source)] * 4,
        ticks=[
            _ticks(
                source,
                received_at=NOW - 2.0,
                token_epoch=int(NOW) - 20,
            ),
            _ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 19,
                event_sequence=8,
            ),
        ],
        bar_rounds=[first_round, mutated_round],
    )
    collector, ledger, clock = _collector(tmp_path, transport)

    collector.capture_cycle(include_bars=True)
    assert len(_chunk(ledger)["bars"]) == len(capture.SYMBOLS) * capture.MINIMUM_M1_BARS
    clock.now += 1.0
    with pytest.raises(capture.CollectionRefusal):
        collector.capture_cycle(include_bars=True)
    assert len(ledger.entries) == 1


def _emit_sanitized_chunk(
    output: Path,
) -> tuple[capture.ManifestLedger, capture.SourceIdentity]:
    source = _source(suffix="secrets-must-not-leak")
    transport = FakeBridgeTransport(
        states=[_state(source), _state(source)],
        ticks=[
            _ticks(
                source,
                received_at=NOW - 1.0,
                token_epoch=int(NOW) - 10,
            )
        ],
    )
    collector, ledger, _clock = _collector(output, transport)
    collector.capture_cycle(include_bars=False)
    return ledger, source


def test_manifest_hashes_and_output_are_secret_sanitized(tmp_path: Path) -> None:
    ledger, source = _emit_sanitized_chunk(tmp_path)
    entry = ledger.entries[0]
    chunk_path = ledger.root / entry["chunk_path"]
    chunk_bytes = chunk_path.read_bytes()
    assert hashlib_sha256(chunk_bytes) == entry["chunk_sha256"]
    chunk = _chunk(ledger)
    assert entry["preregistration_body_sha256"] == "1" * 64
    assert chunk["preregistration_body_sha256"] == "1" * 64
    assert entry["preregistration_artifact_sha256"] == "2" * 64
    assert chunk["preregistration_artifact_sha256"] == "2" * 64

    all_output = b"\n".join(
        path.read_bytes() for path in ledger.root.rglob("*") if path.is_file()
    ).decode("utf-8")
    for secret in (
        API_KEY,
        source.broker_account_scope,
        source.producer_identity,
        source.producer_instance_id,
        source.terminal_lease_scope,
        source.credential_generation_id,
    ):
        assert secret not in all_output
    sanitized = _chunk(ledger)["source"]
    assert sanitized["market_source_id"] == source.source_id
    assert sanitized["broker_account_scope_sha256"]
    assert "broker_account_scope" not in sanitized

    with pytest.raises(
        capture.CollectionRefusal,
        match="ledger_preregistration_binding_mismatch",
    ):
        _collector(
            ledger.root,
            FakeBridgeTransport(),
            binding=_binding(body_sha256="3" * 64),
        )


def hashlib_sha256(payload: bytes) -> str:
    # Local helper keeps the assertion independent from the collector's
    # private byte-hash function.
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def test_chunk_and_manifest_tampering_are_detected(tmp_path: Path) -> None:
    chunk_root = tmp_path / "chunk-tamper"
    ledger, _source_value = _emit_sanitized_chunk(chunk_root)
    chunk_path = ledger.root / ledger.entries[0]["chunk_path"]
    chunk_path.write_bytes(chunk_path.read_bytes() + b" ")
    with pytest.raises(capture.CollectionRefusal):
        capture.ManifestLedger(chunk_root)

    manifest_root = tmp_path / "manifest-tamper"
    ledger, _source_value = _emit_sanitized_chunk(manifest_root)
    line = json.loads(ledger.manifest_path.read_text(encoding="utf-8"))
    line["quote_rows"] += 1
    ledger.manifest_path.write_text(
        json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(capture.CollectionRefusal):
        capture.ManifestLedger(manifest_root)
