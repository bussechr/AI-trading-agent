"""Authenticated broker-market source identity shared by bridge consumers.

The MT4 bridge API key authenticates a request, but it does not identify which
terminal/account produced the market data.  This contract binds quotes and
bars to the already provisioned singleton command-consumer lease, the
heartbeat account scope, and the server-derived broker venue.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import time
from typing import Any, Mapping


MARKET_SOURCE_SCHEMA = "fxstack_authenticated_broker_market_source_v2"
MARKET_SOURCE_FIELDS: tuple[str, ...] = (
    "market_source_schema",
    "market_source_id",
    "market_source_authenticated",
    "broker_account_scope",
    "broker_venue_id",
    "producer_identity",
    "producer_instance_id",
    "terminal_lease_scope",
    "credential_generation_id",
    "bridge_protocol_version",
)


def _token(value: Any, *, limit: int = 128, lower: bool = False) -> str:
    normalized = str(value or "").strip()[:limit]
    return normalized.lower() if lower else normalized


@dataclass(frozen=True, slots=True)
class AuthenticatedMarketSource:
    broker_account_scope: str
    broker_venue_id: str
    producer_identity: str
    producer_instance_id: str
    terminal_lease_scope: str
    credential_generation_id: str
    bridge_protocol_version: str

    @property
    def source_id(self) -> str:
        material = json.dumps(
            [
                MARKET_SOURCE_SCHEMA,
                self.broker_account_scope,
                self.broker_venue_id,
                self.producer_identity,
                self.producer_instance_id,
                self.terminal_lease_scope,
                self.credential_generation_id,
                self.bridge_protocol_version,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_fields(self) -> dict[str, Any]:
        return {
            "market_source_schema": MARKET_SOURCE_SCHEMA,
            "market_source_id": self.source_id,
            "market_source_authenticated": True,
            "broker_account_scope": self.broker_account_scope,
            "broker_venue_id": self.broker_venue_id,
            "producer_identity": self.producer_identity,
            "producer_instance_id": self.producer_instance_id,
            "terminal_lease_scope": self.terminal_lease_scope,
            "credential_generation_id": self.credential_generation_id,
            "bridge_protocol_version": self.bridge_protocol_version,
        }


def build_authenticated_market_source(
    *,
    broker_account_scope: Any,
    broker_venue_id: Any,
    producer_identity: Any,
    producer_instance_id: Any,
    terminal_lease_scope: Any,
    credential_generation_id: Any,
    bridge_protocol_version: Any,
) -> AuthenticatedMarketSource | None:
    values = (
        _token(broker_account_scope),
        _token(broker_venue_id, limit=64, lower=True),
        _token(producer_identity),
        _token(producer_instance_id),
        _token(terminal_lease_scope),
        _token(credential_generation_id),
        _token(bridge_protocol_version, limit=32),
    )
    if not all(values):
        return None
    return AuthenticatedMarketSource(*values)


def current_authenticated_market_source(
    state: Mapping[str, Any] | None,
    *,
    now_epoch: float | None = None,
    require_active_lease: bool = True,
    expected_protocol_version: str = "",
) -> tuple[AuthenticatedMarketSource | None, str]:
    """Project the current heartbeat identity only when its singleton lease agrees."""

    snapshot = dict(state or {})
    source = build_authenticated_market_source(
        broker_account_scope=snapshot.get("broker_account_scope"),
        broker_venue_id=snapshot.get("broker_venue_id"),
        producer_identity=snapshot.get("bridge_producer_identity"),
        producer_instance_id=snapshot.get("bridge_producer_instance_id"),
        terminal_lease_scope=snapshot.get("bridge_terminal_lease_scope"),
        credential_generation_id=snapshot.get("bridge_credential_generation_id"),
        bridge_protocol_version=snapshot.get("bridge_protocol_version"),
    )
    if source is None:
        return None, "market_source_heartbeat_identity_missing"
    expected_protocol = _token(expected_protocol_version, limit=32)
    if expected_protocol and source.bridge_protocol_version != expected_protocol:
        return None, "market_source_protocol_version_mismatch"

    lease = dict(snapshot.get("bridge_consumer_lease") or {})
    if not lease:
        return None, "market_source_consumer_lease_missing"
    lease_source = build_authenticated_market_source(
        broker_account_scope=source.broker_account_scope,
        broker_venue_id=source.broker_venue_id,
        producer_identity=lease.get("consumer_identity"),
        producer_instance_id=lease.get("producer_instance_id"),
        terminal_lease_scope=lease.get("terminal_lease_scope"),
        credential_generation_id=lease.get("credential_generation_id"),
        bridge_protocol_version=lease.get("bridge_protocol_version"),
    )
    if lease_source is None or lease_source != source:
        return None, "market_source_consumer_lease_identity_mismatch"
    if require_active_lease:
        try:
            expires_at = float(lease.get("expires_at") or 0.0)
            now = float(time.time() if now_epoch is None else now_epoch)
        except (TypeError, ValueError, OverflowError):
            return None, "market_source_consumer_lease_invalid"
        if not math.isfinite(expires_at) or not math.isfinite(now) or expires_at <= now:
            return None, "market_source_consumer_lease_expired"
    return source, ""


def authenticated_market_source_from_row(
    row: Mapping[str, Any] | None,
) -> tuple[AuthenticatedMarketSource | None, str]:
    item = dict(row or {})
    if item.get("market_source_authenticated") not in (True, 1):
        return None, "market_source_unauthenticated"
    if _token(item.get("market_source_schema"), limit=96) != MARKET_SOURCE_SCHEMA:
        return None, "market_source_schema_invalid"
    source = build_authenticated_market_source(
        broker_account_scope=item.get("broker_account_scope"),
        broker_venue_id=item.get("broker_venue_id"),
        producer_identity=item.get("producer_identity"),
        producer_instance_id=item.get("producer_instance_id"),
        terminal_lease_scope=item.get("terminal_lease_scope"),
        credential_generation_id=item.get("credential_generation_id"),
        bridge_protocol_version=item.get("bridge_protocol_version"),
    )
    if source is None:
        return None, "market_source_identity_incomplete"
    if _token(item.get("market_source_id"), limit=64, lower=True) != source.source_id:
        return None, "market_source_id_invalid"
    return source, ""


def market_source_row_error(
    row: Mapping[str, Any] | None,
    *,
    expected: AuthenticatedMarketSource,
) -> str:
    observed, error = authenticated_market_source_from_row(row)
    if error:
        return error
    if observed != expected:
        return "market_source_identity_mismatch"
    return ""


def market_source_row_matches(
    row: Mapping[str, Any] | None,
    *,
    expected: AuthenticatedMarketSource,
) -> bool:
    return not market_source_row_error(row, expected=expected)
