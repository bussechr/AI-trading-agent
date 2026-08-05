"""Collect prospective authenticated IG-MT4 M1 activity inputs.

This operator tool is deliberately GET-only and collection-only.  It reads
the loopback bridge state, current ticks, and one exact M1 bar history for
each member of the fixed 22-symbol scope.  It cannot inspect or mutate the
command queue, database, runtime authority, strategy, or broker account.

The portable output contains only direct MT4 bid OHLC/iVolume bars and
authenticated bid/ask transport observations.  Repeated unchanged quotes
are retained because their collector observation time proves continuous
transport coverage during quiet markets.  Outputs are immutable, sanitized,
hour-scoped chunks referenced by a hash-chained append-only manifest. Every
chunk is bound to one sealed preregistration, and no bridge GET occurs before
its T0 or at/after its exclusive end.
"""

from __future__ import annotations

# AGENT: ROLE: GET-only producer of prospective MTVCLC source inputs.
# AGENT: HANDSHAKE: authenticated IG-DEMO state/ticks/direct-M1-bars -> immutable isolation handoff.
# AGENT: ISOLATION: collection only; output cannot establish performance, success, or authority.
# AGENT: SIDE EFFECTS: loopback GET requests and atomic append-only local artifact writes only.

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import tempfile
import time
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)


COLLECTOR_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_collector.v2"
CHUNK_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_chunk.v2"
MANIFEST_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_manifest_entry.v2"
PREREGISTRATION_SCHEMA_VERSION = "fxstack.scalp.mtvclc_preregistration.v1"
SOURCE_CONTRACT_ID = (
    "authenticated_ig_mt4_bid_m1_ohlc_ivolume_plus_bid_ask_transport_snapshots.v1"
)
ACTIVITY_METRIC_ID = "mt4_m1_ivolume_tick_volume.v1"
MARKET_SOURCE_SCHEMA = "fxstack_authenticated_broker_market_source_v2"
BROKER_ACCOUNT_SCOPE_SCHEMA = "fxstack_mt4_account_scope_djb2_xor32_v1"
BROKER_ACCOUNT_SCOPE_VERSION = 1
BRIDGE_PROTOCOL_VERSION = "v3.0.0"
SCOPE_VERSION = "fxstack.ig_mt4.scalp_scope.v3"
VENUE_ID = "ig_mt4"
PRICE_BASIS = "mt4_bid_ohlc_v1"
VOLUME_SOURCE = "mt4_ivolume_tick_count_v1"
TIMEFRAME = "M1"

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
_SYMBOL_SET = frozenset(SYMBOLS)

MINIMUM_M1_BARS = 241
DEFAULT_BAR_LIMIT = 400
# Keep scheduling/HTTP jitter comfortably inside the evaluator's strict
# five-second raw transport-gap ceiling.  Five seconds itself has no margin.
DEFAULT_TICK_INTERVAL_SECS = 2.0
DEFAULT_BAR_INTERVAL_SECS = 60.0
DEFAULT_HTTP_TIMEOUT_SECS = 5.0
MAXIMUM_TICK_INTERVAL_SECS = 5.0
MAXIMUM_HTTP_RESPONSE_BYTES = 16 * 1024 * 1024
MAXIMUM_API_KEY_BYTES = 64 * 1024
MAXIMUM_PREREGISTRATION_BYTES = 1024 * 1024
PROSPECTIVE_WINDOW_DAYS = 180
MANIFEST_FILENAME = "manifest.sha256.jsonl"
CHUNK_DIRECTORY = "chunks"
TOOL_PATH = Path(__file__).resolve()
_ZERO_SHA256 = "0" * 64
_FIXED_FALSE_AUTHORITY = {
    "research_process_authorized": False,
    "outcome_access_authorized": False,
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
_ALLOWED_GET_PATHS = frozenset(
    {"/v2/state", "/v2/market/ticks", "/v2/market/bars"}
)


class CollectionRefusal(RuntimeError):
    """Fail-closed refusal carrying a stable non-secret reason."""


def canonical_json_bytes(value: Any) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CollectionRefusal("payload_not_canonical") from exc
    return encoded.encode("utf-8")


def _reject_duplicate_object_pairs(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CollectionRefusal("json_duplicate_key")
        result[key] = value
    return result


def _strict_json_object(raw: bytes, *, reason: str) -> dict[str, Any]:
    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                CollectionRefusal(reason)
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CollectionRefusal(reason) from None
    if not isinstance(parsed, dict):
        raise CollectionRefusal(reason)
    return parsed


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: Any, reason: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CollectionRefusal(reason)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _parse_utc_second(value: Any, reason: str) -> datetime:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise CollectionRefusal(reason) from None
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text:
        raise CollectionRefusal(reason)
    return parsed


@dataclass(frozen=True, slots=True)
class ProspectiveBinding:
    preregistration_body_sha256: str
    preregistration_artifact_sha256: str
    t0_utc: str
    end_utc_exclusive: str
    t0_epoch: float
    end_epoch_exclusive: float

    def chunk_fields(self) -> dict[str, Any]:
        return {
            "preregistration_body_sha256": self.preregistration_body_sha256,
            "preregistration_artifact_sha256": (
                self.preregistration_artifact_sha256
            ),
            "prospective_t0_utc_inclusive": self.t0_utc,
            "prospective_end_utc_exclusive": self.end_utc_exclusive,
        }


def _validated_binding_tuple(
    value: Mapping[str, Any],
    *,
    reason: str,
) -> tuple[str, str, str, str]:
    body_sha = str(value.get("preregistration_body_sha256") or "").lower()
    artifact_sha = str(
        value.get("preregistration_artifact_sha256") or ""
    ).lower()
    t0_text = str(value.get("prospective_t0_utc_inclusive") or "")
    end_text = str(value.get("prospective_end_utc_exclusive") or "")
    if not _is_sha256(body_sha) or not _is_sha256(artifact_sha):
        raise CollectionRefusal(reason)
    t0 = _parse_utc_second(t0_text, reason)
    end = _parse_utc_second(end_text, reason)
    if end - t0 != timedelta(days=PROSPECTIVE_WINDOW_DAYS):
        raise CollectionRefusal(reason)
    return body_sha, artifact_sha, t0_text, end_text


def load_preregistration(path: str | Path) -> ProspectiveBinding:
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_file() or target.is_symlink():
        raise CollectionRefusal("preregistration_file_invalid")
    try:
        size = target.stat().st_size
        if size <= 0 or size > MAXIMUM_PREREGISTRATION_BYTES:
            raise CollectionRefusal("preregistration_file_size_invalid")
        raw = target.read_bytes()
    except OSError as exc:
        raise CollectionRefusal("preregistration_file_unreadable") from exc
    payload = _strict_json_object(raw, reason="preregistration_json_invalid")
    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not _is_sha256(claimed) or claimed != canonical_sha256(body):
        raise CollectionRefusal("preregistration_body_hash_invalid")
    if (
        body.get("schema_version") != PREREGISTRATION_SCHEMA_VERSION
        or body.get("research_only") is not True
        or body.get("authority") != _FIXED_FALSE_AUTHORITY
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    scope = body.get("scope")
    strategy = body.get("strategy")
    execution = body.get("execution_contract")
    window = body.get("prospective_window")
    identities = body.get("source_identities")
    if not all(
        isinstance(value, Mapping)
        for value in (scope, strategy, execution, window, identities)
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    assert isinstance(scope, Mapping)
    assert isinstance(strategy, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(window, Mapping)
    assert isinstance(identities, Mapping)
    cells = scope.get("cell_order")
    expected_cells = [
        (symbol, side)
        for symbol in SYMBOLS
        for side in ("BUY", "SELL")
    ]
    observed_cells = []
    if isinstance(cells, list):
        for row in cells:
            if not isinstance(row, Mapping):
                raise CollectionRefusal("preregistration_scope_invalid")
            observed_cells.append(
                (
                    str(row.get("symbol") or "").strip().upper(),
                    str(row.get("side") or "").strip().upper(),
                )
            )
    collector_identity = identities.get("collector_source")
    current_source_sha256 = _sha256_bytes(TOOL_PATH.read_bytes())
    if (
        scope.get("ordered_symbols") != list(SYMBOLS)
        or scope.get("scope_version") != SCOPE_VERSION
        or scope.get("venue_id") != VENUE_ID
        or observed_cells != expected_cells
        or strategy.get("source_contract_id") != SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != ACTIVITY_METRIC_ID
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
        or not isinstance(collector_identity, Mapping)
        or str(collector_identity.get("sha256") or "").lower()
        != current_source_sha256
        or window.get("consecutive_days") != PROSPECTIVE_WINDOW_DAYS
        or window.get("fixed_before_any_eligible_observation") is not True
        or window.get("observations_before_t0_forbidden") is not True
        or window.get("observations_at_or_after_end_forbidden") is not True
        or window.get("interim_signal_or_outcome_evaluation_forbidden") is not True
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    sealed_at = _parse_utc_second(
        body.get("sealed_at_utc"),
        "preregistration_time_invalid",
    )
    t0 = _parse_utc_second(
        window.get("t0_utc_inclusive"),
        "preregistration_time_invalid",
    )
    end = _parse_utc_second(
        window.get("end_utc_exclusive"),
        "preregistration_time_invalid",
    )
    if t0 <= sealed_at or end - t0 != timedelta(days=PROSPECTIVE_WINDOW_DAYS):
        raise CollectionRefusal("preregistration_time_invalid")
    return ProspectiveBinding(
        preregistration_body_sha256=claimed,
        preregistration_artifact_sha256=_sha256_bytes(raw),
        t0_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        t0_epoch=t0.timestamp(),
        end_epoch_exclusive=end.timestamp(),
    )


def _finite_float(value: Any, reason: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise CollectionRefusal(reason) from None
    if not math.isfinite(number):
        raise CollectionRefusal(reason)
    return number


def _positive_float(value: Any, reason: str) -> float:
    number = _finite_float(value, reason)
    if number <= 0.0:
        raise CollectionRefusal(reason)
    return number


def _strict_nonnegative_int(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CollectionRefusal(reason)
    return int(value)


def _strict_positive_int(value: Any, reason: str) -> int:
    number = _strict_nonnegative_int(value, reason)
    if number <= 0:
        raise CollectionRefusal(reason)
    return number


def _parse_epoch(value: Any, reason: str) -> float:
    if isinstance(value, bool) or value is None:
        raise CollectionRefusal(reason)
    if isinstance(value, (int, float)):
        return _positive_float(value, reason)
    text = str(value).strip()
    if not text:
        raise CollectionRefusal(reason)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            return _positive_float(text, reason)
        except CollectionRefusal:
            raise CollectionRefusal(reason) from None
    if parsed.tzinfo is None:
        raise CollectionRefusal(reason)
    try:
        return _positive_float(parsed.astimezone(UTC).timestamp(), reason)
    except (OSError, OverflowError, ValueError):
        raise CollectionRefusal(reason) from None


def _validated_loopback_base_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        raise CollectionRefusal("bridge_url_invalid") from None
    if parsed.scheme.lower() != "http":
        raise CollectionRefusal("bridge_url_must_be_loopback_http")
    host = str(parsed.hostname or "").strip().lower()
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise CollectionRefusal("bridge_url_must_be_loopback_http")
    if parsed.username or parsed.password:
        raise CollectionRefusal("bridge_url_userinfo_forbidden")
    if port is None or not 1 <= int(port) <= 65_535:
        raise CollectionRefusal("bridge_url_port_invalid")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise CollectionRefusal("bridge_url_must_be_loopback_root")
    bracketed_host = f"[{host}]" if ":" in host else host
    return f"http://{bracketed_host}:{port}"


HttpTransport = Callable[[Request, float, int], tuple[int, bytes]]


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _urlopen_transport(
    request: Request,
    timeout_secs: float,
    maximum_bytes: int,
) -> tuple[int, bytes]:
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout_secs) as response:  # noqa: S310
            status = int(getattr(response, "status", 0) or 0)
            body = response.read(maximum_bytes + 1)
    except HTTPError as exc:
        return int(exc.code), b""
    except (URLError, TimeoutError, OSError):
        raise CollectionRefusal("bridge_read_failed") from None
    return status, body


class BridgeReadClient:
    """Strict loopback client with no route or method capable of mutation."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_secs: float = DEFAULT_HTTP_TIMEOUT_SECS,
        transport: HttpTransport | None = None,
    ) -> None:
        self.base_url = _validated_loopback_base_url(base_url)
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise CollectionRefusal("bridge_api_key_missing")
        self.timeout_secs = _positive_float(
            timeout_secs,
            "http_timeout_invalid",
        )
        self._transport = transport or _urlopen_transport

    def _request(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        if path not in _ALLOWED_GET_PATHS:
            raise CollectionRefusal("bridge_read_path_forbidden")
        params = dict(query or {})
        if path in {"/v2/state", "/v2/market/ticks"} and params:
            raise CollectionRefusal("bridge_read_query_forbidden")
        if path == "/v2/market/bars":
            if set(params) != {"symbol", "timeframe", "limit"}:
                raise CollectionRefusal("bridge_bar_query_invalid")
            symbol = str(params.get("symbol") or "").strip().upper()
            timeframe = str(params.get("timeframe") or "").strip().upper()
            limit = params.get("limit")
            if symbol not in _SYMBOL_SET or timeframe != TIMEFRAME:
                raise CollectionRefusal("bridge_bar_query_invalid")
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not MINIMUM_M1_BARS <= limit <= 2_000
            ):
                raise CollectionRefusal("bridge_bar_query_invalid")
            params = {"symbol": symbol, "timeframe": TIMEFRAME, "limit": limit}
        suffix = f"?{urlencode(params)}" if params else ""
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["X-API-Key"] = self._api_key
        request = Request(
            f"{self.base_url}{path}{suffix}",
            headers=headers,
            method="GET",
        )
        status, raw = self._transport(
            request,
            self.timeout_secs,
            MAXIMUM_HTTP_RESPONSE_BYTES,
        )
        if int(status) != 200:
            raise CollectionRefusal(f"bridge_http_status_{int(status)}")
        if not raw or len(raw) > MAXIMUM_HTTP_RESPONSE_BYTES:
            raise CollectionRefusal("bridge_response_size_invalid")
        try:
            return _strict_json_object(
                raw,
                reason="bridge_response_json_invalid",
            )
        except CollectionRefusal as exc:
            if str(exc) == "json_duplicate_key":
                raise CollectionRefusal("bridge_response_duplicate_key") from None
            raise

    def prove_authentication_required(self) -> None:
        try:
            self._request("/v2/state", authenticated=False)
        except CollectionRefusal as exc:
            if str(exc) == "bridge_http_status_401":
                return
            raise CollectionRefusal("bridge_authentication_not_proven") from None
        raise CollectionRefusal("bridge_authentication_not_required")

    def get_state(self) -> dict[str, Any]:
        return self._request("/v2/state")

    def get_ticks(self) -> dict[str, Any]:
        return self._request("/v2/market/ticks")

    def get_m1_bars(self, symbol: str, *, limit: int) -> dict[str, Any]:
        return self._request(
            "/v2/market/bars",
            query={"symbol": symbol, "timeframe": TIMEFRAME, "limit": limit},
        )


@dataclass(frozen=True, slots=True)
class SourceIdentity:
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

    def sanitized(self) -> dict[str, Any]:
        return {
            "market_source_schema": MARKET_SOURCE_SCHEMA,
            "market_source_id": self.source_id,
            "market_source_authenticated": True,
            "broker_account_mode": "demo",
            "broker_venue_id": VENUE_ID,
            "broker_account_scope_sha256": _sha256_text(
                self.broker_account_scope,
                "broker_account_scope_missing",
            ),
            "producer_identity_sha256": _sha256_text(
                self.producer_identity,
                "producer_identity_missing",
            ),
            "producer_instance_id_sha256": _sha256_text(
                self.producer_instance_id,
                "producer_instance_id_missing",
            ),
            "terminal_lease_scope_sha256": _sha256_text(
                self.terminal_lease_scope,
                "terminal_lease_scope_missing",
            ),
            "credential_generation_id_sha256": _sha256_text(
                self.credential_generation_id,
                "credential_generation_id_missing",
            ),
            "bridge_protocol_version": self.bridge_protocol_version,
        }


def _required_text(value: Any, reason: str, *, lower: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        raise CollectionRefusal(reason)
    return text.lower() if lower else text


def _source_from_fields(row: Mapping[str, Any]) -> SourceIdentity:
    item = dict(row or {})
    if item.get("market_source_authenticated") is not True:
        raise CollectionRefusal("market_source_unauthenticated")
    if str(item.get("market_source_schema") or "") != MARKET_SOURCE_SCHEMA:
        raise CollectionRefusal("market_source_schema_invalid")
    source = SourceIdentity(
        broker_account_scope=_required_text(
            item.get("broker_account_scope"),
            "broker_account_scope_missing",
        ),
        broker_venue_id=_required_text(
            item.get("broker_venue_id"),
            "broker_venue_id_missing",
            lower=True,
        ),
        producer_identity=_required_text(
            item.get("producer_identity"),
            "producer_identity_missing",
        ),
        producer_instance_id=_required_text(
            item.get("producer_instance_id"),
            "producer_instance_id_missing",
        ),
        terminal_lease_scope=_required_text(
            item.get("terminal_lease_scope"),
            "terminal_lease_scope_missing",
        ),
        credential_generation_id=_required_text(
            item.get("credential_generation_id"),
            "credential_generation_id_missing",
        ),
        bridge_protocol_version=_required_text(
            item.get("bridge_protocol_version"),
            "bridge_protocol_version_missing",
        ),
    )
    if source.broker_venue_id != VENUE_ID:
        raise CollectionRefusal("broker_venue_not_ig_mt4")
    if source.bridge_protocol_version != BRIDGE_PROTOCOL_VERSION:
        raise CollectionRefusal("bridge_protocol_version_invalid")
    if str(item.get("market_source_id") or "").strip().lower() != source.source_id:
        raise CollectionRefusal("market_source_id_invalid")
    return source


def source_from_state(
    state: Mapping[str, Any],
    *,
    observed_at_epoch: float,
) -> SourceIdentity:
    snapshot = dict(state or {})
    if str(snapshot.get("system_status") or "").strip().lower() != "connected":
        raise CollectionRefusal("bridge_state_not_connected")
    if snapshot.get("database_ok") is not True:
        raise CollectionRefusal("bridge_database_not_healthy")
    heartbeat_epoch = _parse_epoch(
        snapshot.get("last_heartbeat"),
        "bridge_heartbeat_missing",
    )
    if heartbeat_epoch > observed_at_epoch + 5.0:
        raise CollectionRefusal("bridge_heartbeat_time_future")
    heartbeat_age = _finite_float(
        snapshot.get("heartbeat_age_secs"),
        "bridge_heartbeat_age_invalid",
    )
    heartbeat_stale_after = _positive_float(
        snapshot.get("heartbeat_stale_after_secs"),
        "bridge_heartbeat_stale_after_invalid",
    )
    if heartbeat_age < 0.0 or heartbeat_age > heartbeat_stale_after:
        raise CollectionRefusal("bridge_heartbeat_stale")
    if observed_at_epoch - heartbeat_epoch > heartbeat_stale_after + 5.0:
        raise CollectionRefusal("bridge_heartbeat_stale")
    if str(snapshot.get("broker_account_mode") or "").strip().lower() != "demo":
        raise CollectionRefusal("broker_account_not_demo")
    if str(snapshot.get("broker_venue_id") or "").strip().lower() != VENUE_ID:
        raise CollectionRefusal("broker_venue_not_ig_mt4")
    if (
        str(snapshot.get("broker_account_scope_schema") or "").strip()
        != BROKER_ACCOUNT_SCOPE_SCHEMA
    ):
        raise CollectionRefusal("broker_account_scope_schema_invalid")
    if (
        _strict_nonnegative_int(
            snapshot.get("broker_account_scope_version"),
            "broker_account_scope_version_invalid",
        )
        != BROKER_ACCOUNT_SCOPE_VERSION
    ):
        raise CollectionRefusal("broker_account_scope_version_invalid")
    configured_pairs = snapshot.get("configured_pairs")
    if (
        not isinstance(configured_pairs, Sequence)
        or isinstance(configured_pairs, (str, bytes, bytearray, Mapping))
        or tuple(str(item or "").strip().upper() for item in configured_pairs)
        != SYMBOLS
    ):
        raise CollectionRefusal("configured_symbol_scope_invalid")
    if (
        _strict_nonnegative_int(
            snapshot.get("symbol_ready_count"),
            "symbol_ready_count_invalid",
        )
        != len(SYMBOLS)
    ):
        raise CollectionRefusal("symbol_ready_count_invalid")
    symbol_readiness = snapshot.get("symbol_readiness")
    if not isinstance(symbol_readiness, Mapping):
        raise CollectionRefusal("symbol_readiness_invalid")
    normalized_readiness = {
        str(key or "").strip().upper(): value
        for key, value in symbol_readiness.items()
    }
    if set(normalized_readiness) != _SYMBOL_SET:
        raise CollectionRefusal("symbol_readiness_scope_invalid")
    for symbol in SYMBOLS:
        readiness = normalized_readiness.get(symbol)
        if not isinstance(readiness, Mapping):
            raise CollectionRefusal("symbol_readiness_invalid")
        if (
            readiness.get("supported") is not True
            or readiness.get("selected") is not True
            or readiness.get("mapping_ambiguous") is not False
            or not str(readiness.get("broker_symbol") or "").strip()
        ):
            raise CollectionRefusal("symbol_not_ready")
    raw_source = snapshot.get("bridge_market_source")
    if not isinstance(raw_source, Mapping):
        raise CollectionRefusal("bridge_market_source_missing")
    source = _source_from_fields(raw_source)
    top_level_expectations = {
        "broker_account_scope": source.broker_account_scope,
        "broker_venue_id": source.broker_venue_id,
        "bridge_producer_identity": source.producer_identity,
        "bridge_producer_instance_id": source.producer_instance_id,
        "bridge_terminal_lease_scope": source.terminal_lease_scope,
        "bridge_credential_generation_id": source.credential_generation_id,
        "bridge_protocol_version": source.bridge_protocol_version,
    }
    for field, expected in top_level_expectations.items():
        actual = str(snapshot.get(field) or "").strip()
        if field == "broker_venue_id":
            actual = actual.lower()
        if actual != expected:
            raise CollectionRefusal("state_market_source_identity_mismatch")
    lease = snapshot.get("bridge_consumer_lease")
    if not isinstance(lease, Mapping):
        raise CollectionRefusal("bridge_consumer_lease_missing")
    lease_expectations = {
        "consumer_identity": source.producer_identity,
        "producer_instance_id": source.producer_instance_id,
        "terminal_lease_scope": source.terminal_lease_scope,
        "credential_generation_id": source.credential_generation_id,
        "bridge_protocol_version": source.bridge_protocol_version,
    }
    for field, expected in lease_expectations.items():
        if str(lease.get(field) or "").strip() != expected:
            raise CollectionRefusal("bridge_consumer_lease_identity_mismatch")
    expires_at = _positive_float(
        lease.get("expires_at"),
        "bridge_consumer_lease_invalid",
    )
    if expires_at <= observed_at_epoch:
        raise CollectionRefusal("bridge_consumer_lease_expired")
    bridge_status_source = snapshot.get("bridge_status_market_source")
    if not isinstance(bridge_status_source, Mapping):
        raise CollectionRefusal("bridge_status_market_source_missing")
    _require_expected_source(bridge_status_source, expected=source)
    if (
        str(snapshot.get("bridge_status_market_source_id") or "")
        .strip()
        .lower()
        != source.source_id
    ):
        raise CollectionRefusal("bridge_status_market_source_id_invalid")
    return source


def _require_expected_source(
    row: Mapping[str, Any],
    *,
    expected: SourceIdentity,
) -> None:
    observed = _source_from_fields(row)
    if observed != expected:
        raise CollectionRefusal("market_source_rollover_during_cycle")


@dataclass(frozen=True, slots=True)
class TickObservation:
    symbol: str
    observation_sequence: int
    observation_epoch: int
    observed_at_epoch: float
    bid: float
    ask: float
    transport_received_at_epoch: float
    market_event_received_at_epoch: float | None
    market_event_sequence: int
    source_event_token_sha256: str
    snapshot_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class M1ActivityBar:
    symbol: str
    minute_epoch: int
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    tick_volume: int
    price_basis: str = PRICE_BASIS
    volume_source: str = VOLUME_SOURCE

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_tick_snapshot(
    payload: Mapping[str, Any],
    *,
    expected_source: SourceIdentity,
    observed_at_epoch: float,
    prior_sequences: Mapping[str, int],
    prior_transport_epochs: Mapping[str, float] | None = None,
    prior_snapshot_sha256: Mapping[str, str] | None = None,
) -> tuple[TickObservation, ...]:
    rows = dict(payload or {})
    prior_transport_epochs = dict(prior_transport_epochs or {})
    prior_snapshot_sha256 = dict(prior_snapshot_sha256 or {})
    missing = [symbol for symbol in SYMBOLS if symbol not in rows]
    if missing:
        raise CollectionRefusal("tick_scope_incomplete")
    observations: list[TickObservation] = []
    for symbol in SYMBOLS:
        raw = rows.get(symbol)
        if not isinstance(raw, Mapping):
            raise CollectionRefusal("tick_row_invalid")
        row = dict(raw)
        if str(row.get("symbol") or "").strip().upper() != symbol:
            raise CollectionRefusal("tick_symbol_mismatch")
        _require_expected_source(row, expected=expected_source)
        if row.get("transport_fresh") is not True:
            raise CollectionRefusal("tick_transport_not_fresh")
        bid = _positive_float(row.get("bid"), "tick_bid_invalid")
        ask = _positive_float(row.get("ask"), "tick_ask_invalid")
        if ask < bid:
            raise CollectionRefusal("tick_quote_crossed")
        received = _positive_float(
            row.get("received_at_epoch"),
            "tick_transport_time_invalid",
        )
        if received > observed_at_epoch + 5.0:
            raise CollectionRefusal("tick_transport_time_future")
        transport_prior = _finite_float(
            prior_transport_epochs.get(symbol, 0.0),
            "prior_tick_transport_time_invalid",
        )
        if transport_prior < 0.0 or received < transport_prior:
            raise CollectionRefusal("tick_transport_time_regressed")
        if row.get("source_event_baseline_initialized") is not True:
            raise CollectionRefusal("tick_source_event_baseline_missing")
        source_token = str(row.get("source_event_token") or "").strip()
        if not source_token.isascii() or not source_token.isdecimal():
            raise CollectionRefusal("tick_source_event_token_invalid")
        if int(source_token) <= 0:
            raise CollectionRefusal("tick_source_event_token_invalid")
        # MODE_TIME is an opaque broker-server event identity.  Its integer
        # value follows the broker's wall-clock convention, so it may be ahead
        # of UTC or step at a broker DST boundary.  Never interpret or order
        # it as a UTC epoch.  Authenticated bridge receipt time and the API's
        # market_event_sequence provide the continuity clocks.
        normalized_ts_raw = row.get("ts_epoch")
        if normalized_ts_raw is not None:
            normalized_ts = _positive_float(
                normalized_ts_raw,
                "tick_normalized_time_invalid",
            )
            if normalized_ts > observed_at_epoch + 5.0:
                raise CollectionRefusal("tick_normalized_time_future")
        event_received_raw = row.get("market_event_received_at_epoch")
        market_event_received_at: float | None = None
        if event_received_raw is not None:
            market_event_received_at = _positive_float(
                event_received_raw,
                "tick_market_event_receipt_invalid",
            )
            if (
                market_event_received_at > received
                or market_event_received_at > observed_at_epoch + 5.0
            ):
                raise CollectionRefusal("tick_market_event_receipt_future")
        market_event_sequence = _strict_nonnegative_int(
            row.get("market_event_sequence"),
            "tick_market_event_sequence_invalid",
        )
        if (market_event_sequence == 0) != (market_event_received_at is None):
            raise CollectionRefusal("tick_market_event_receipt_sequence_mismatch")
        token_hash = hashlib.sha256(source_token.encode("utf-8")).hexdigest()
        snapshot_hash = canonical_sha256(
            {
                "symbol": symbol,
                "transport_received_at_epoch": received,
                "bid": bid,
                "ask": ask,
                "market_event_received_at_epoch": market_event_received_at,
                "market_event_sequence": market_event_sequence,
                "source_event_token_sha256": token_hash,
            }
        )
        if received == transport_prior:
            prior_hash = str(prior_snapshot_sha256.get(symbol) or _ZERO_SHA256)
            if prior_hash != snapshot_hash:
                raise CollectionRefusal("tick_same_receipt_mutated")
            continue
        observations.append(
            TickObservation(
                symbol=symbol,
                observation_sequence=int(prior_sequences.get(symbol, 0)) + 1,
                observation_epoch=int(math.floor(received)),
                observed_at_epoch=received,
                bid=bid,
                ask=ask,
                transport_received_at_epoch=received,
                market_event_received_at_epoch=market_event_received_at,
                market_event_sequence=market_event_sequence,
                source_event_token_sha256=token_hash,
                snapshot_sha256=snapshot_hash,
            )
        )
    return tuple(observations)


def validate_m1_bar_response(
    payload: Mapping[str, Any],
    *,
    symbol: str,
    expected_source: SourceIdentity,
    observed_at_epoch: float,
    minimum_rows: int = MINIMUM_M1_BARS,
    requested_limit: int | None = None,
) -> tuple[M1ActivityBar, ...]:
    response = dict(payload or {})
    if str(response.get("symbol") or "").strip().upper() != symbol:
        raise CollectionRefusal("bar_response_symbol_mismatch")
    if str(response.get("timeframe") or "").strip().upper() != TIMEFRAME:
        raise CollectionRefusal("bar_response_timeframe_mismatch")
    response_limit = _strict_positive_int(
        response.get("limit"),
        "bar_response_limit_invalid",
    )
    if not MINIMUM_M1_BARS <= response_limit <= 2_000:
        raise CollectionRefusal("bar_response_limit_invalid")
    if requested_limit is not None and response_limit != requested_limit:
        raise CollectionRefusal("bar_response_limit_mismatch")
    raw_bars = response.get("bars")
    if (
        not isinstance(raw_bars, Sequence)
        or isinstance(raw_bars, (str, bytes, bytearray, Mapping))
    ):
        raise CollectionRefusal("bar_response_shape_invalid")
    bars: list[M1ActivityBar] = []
    prior_minute: int | None = None
    for raw in raw_bars:
        if not isinstance(raw, Mapping):
            raise CollectionRefusal("bar_row_invalid")
        row = dict(raw)
        _require_expected_source(row, expected=expected_source)
        provenance = (
            str(row.get("price_basis") or ""),
            str(row.get("volume_source") or ""),
            str(row.get("source_timeframe") or "").strip().upper(),
        )
        if provenance != (PRICE_BASIS, VOLUME_SOURCE, TIMEFRAME):
            continue
        epoch_value = _parse_epoch(row.get("time"), "bar_time_invalid")
        rounded_epoch = round(epoch_value)
        if abs(epoch_value - rounded_epoch) > 1e-6:
            raise CollectionRefusal("bar_time_not_integral")
        minute_epoch = int(rounded_epoch)
        if minute_epoch % 60 != 0:
            raise CollectionRefusal("bar_time_not_m1_aligned")
        if minute_epoch + 60 > observed_at_epoch:
            raise CollectionRefusal("bar_not_completed")
        if prior_minute is not None and minute_epoch <= prior_minute:
            raise CollectionRefusal("bar_minutes_not_strictly_increasing")
        prior_minute = minute_epoch
        bid_open = _positive_float(row.get("bid_open"), "bar_bid_open_invalid")
        bid_high = _positive_float(row.get("bid_high"), "bar_bid_high_invalid")
        bid_low = _positive_float(row.get("bid_low"), "bar_bid_low_invalid")
        bid_close = _positive_float(row.get("bid_close"), "bar_bid_close_invalid")
        if bid_high < max(bid_open, bid_close, bid_low):
            raise CollectionRefusal("bar_bid_high_invalid")
        if bid_low > min(bid_open, bid_close, bid_high):
            raise CollectionRefusal("bar_bid_low_invalid")
        volume = _strict_nonnegative_int(
            row.get("volume"),
            "bar_tick_volume_invalid",
        )
        bars.append(
            M1ActivityBar(
                symbol=symbol,
                minute_epoch=minute_epoch,
                bid_open=bid_open,
                bid_high=bid_high,
                bid_low=bid_low,
                bid_close=bid_close,
                tick_volume=volume,
            )
        )
    if len(bars) < int(minimum_rows):
        raise CollectionRefusal("bar_history_insufficient_direct_m1")
    return tuple(bars)


def _atomic_write_new(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise CollectionRefusal("chunk_path_already_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise CollectionRefusal("chunk_directory_symlink_forbidden")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _symbol_int_map(value: Any, reason: str) -> dict[str, int]:
    if not isinstance(value, Mapping) or set(value) != _SYMBOL_SET:
        raise CollectionRefusal(reason)
    return {
        symbol: _strict_nonnegative_int(value.get(symbol), reason)
        for symbol in SYMBOLS
    }


def _symbol_float_map(value: Any, reason: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or set(value) != _SYMBOL_SET:
        raise CollectionRefusal(reason)
    result: dict[str, float] = {}
    for symbol in SYMBOLS:
        number = _finite_float(value.get(symbol), reason)
        if number < 0.0:
            raise CollectionRefusal(reason)
        result[symbol] = number
    return result


def _symbol_hash_map(value: Any, reason: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != _SYMBOL_SET:
        raise CollectionRefusal(reason)
    result: dict[str, str] = {}
    for symbol in SYMBOLS:
        digest = str(value.get(symbol) or "").strip().lower()
        if not _is_sha256(digest):
            raise CollectionRefusal(reason)
        result[symbol] = digest
    return result


class ManifestLedger:
    """Append-only manifest and immutable chunk writer with restart audit."""

    def __init__(self, output_root: str | Path) -> None:
        self.root = Path(output_root).expanduser().resolve(strict=False)
        if self.root.exists() and self.root.is_symlink():
            raise CollectionRefusal("output_root_symlink_forbidden")
        self.root.mkdir(parents=True, exist_ok=True)
        self.chunks_root = self.root / CHUNK_DIRECTORY
        self.chunks_root.mkdir(parents=True, exist_ok=True)
        if self.chunks_root.is_symlink():
            raise CollectionRefusal("chunk_directory_symlink_forbidden")
        self.manifest_path = self.root / MANIFEST_FILENAME
        if self.manifest_path.is_symlink():
            raise CollectionRefusal("manifest_symlink_forbidden")
        self.entries: list[dict[str, Any]] = []
        self.next_sequence = 1
        self.last_entry_sha256 = _ZERO_SHA256
        self.last_source_id = ""
        self.last_segment_index = 0
        self.binding_tuple: tuple[str, str, str, str] | None = None
        self.last_bar_epoch_by_symbol = {symbol: 0 for symbol in SYMBOLS}
        self.last_tick_sequence_by_symbol = {symbol: 0 for symbol in SYMBOLS}
        self.last_tick_transport_epoch_by_symbol = {
            symbol: 0.0 for symbol in SYMBOLS
        }
        self.last_tick_snapshot_sha256_by_symbol = {
            symbol: _ZERO_SHA256 for symbol in SYMBOLS
        }
        self.recent_bar_hashes_by_symbol: dict[str, dict[int, str]] = {
            symbol: {} for symbol in SYMBOLS
        }
        self._load_and_verify()

    def _load_and_verify(self) -> None:
        referenced: set[str] = set()
        loaded_source_id = ""
        loaded_segment_index = 0
        loaded_last_bar = {symbol: 0 for symbol in SYMBOLS}
        loaded_last_tick_sequence = {symbol: 0 for symbol in SYMBOLS}
        loaded_last_tick_transport = {symbol: 0.0 for symbol in SYMBOLS}
        loaded_last_snapshot_hash = {
            symbol: _ZERO_SHA256 for symbol in SYMBOLS
        }
        if self.manifest_path.exists():
            try:
                lines = self.manifest_path.read_bytes().splitlines()
            except OSError as exc:
                raise CollectionRefusal("manifest_unreadable") from exc
            prior_hash = _ZERO_SHA256
            for expected_sequence, raw_line in enumerate(lines, start=1):
                if not raw_line:
                    raise CollectionRefusal("manifest_blank_line")
                try:
                    parsed = _strict_json_object(
                        raw_line,
                        reason="manifest_invalid",
                    )
                except CollectionRefusal as exc:
                    if str(exc) == "json_duplicate_key":
                        raise CollectionRefusal("manifest_duplicate_key") from None
                    raise
                entry = dict(parsed)
                claimed = str(entry.pop("manifest_entry_sha256", "")).lower()
                if not _is_sha256(claimed) or canonical_sha256(entry) != claimed:
                    raise CollectionRefusal("manifest_entry_hash_invalid")
                if entry.get("schema_version") != MANIFEST_SCHEMA_VERSION:
                    raise CollectionRefusal("manifest_schema_invalid")
                if (
                    _strict_positive_int(
                        entry.get("sequence"),
                        "manifest_sequence_invalid",
                    )
                    != expected_sequence
                ):
                    raise CollectionRefusal("manifest_sequence_invalid")
                if entry.get("previous_entry_sha256") != prior_hash:
                    raise CollectionRefusal("manifest_chain_invalid")
                relative = str(entry.get("chunk_path") or "")
                pure = PurePosixPath(relative)
                if (
                    pure.is_absolute()
                    or ".." in pure.parts
                    or not relative.startswith(f"{CHUNK_DIRECTORY}/")
                ):
                    raise CollectionRefusal("manifest_chunk_path_invalid")
                chunk_path = self.root.joinpath(*pure.parts)
                try:
                    chunk_bytes = chunk_path.read_bytes()
                except OSError as exc:
                    raise CollectionRefusal("manifest_chunk_missing") from exc
                if len(chunk_bytes) != entry.get("chunk_size_bytes"):
                    raise CollectionRefusal("manifest_chunk_size_mismatch")
                if _sha256_bytes(chunk_bytes) != entry.get("chunk_sha256"):
                    raise CollectionRefusal("manifest_chunk_hash_mismatch")
                try:
                    chunk = _strict_json_object(
                        chunk_bytes,
                        reason="manifest_chunk_invalid",
                    )
                except CollectionRefusal as exc:
                    if str(exc) == "json_duplicate_key":
                        raise CollectionRefusal("manifest_chunk_duplicate_key") from None
                    raise
                entry_binding = _validated_binding_tuple(
                    entry,
                    reason="manifest_preregistration_binding_invalid",
                )
                chunk_binding = _validated_binding_tuple(
                    chunk,
                    reason="manifest_preregistration_binding_invalid",
                )
                if entry_binding != chunk_binding:
                    raise CollectionRefusal(
                        "manifest_preregistration_binding_mismatch"
                    )
                if self.binding_tuple is None:
                    self.binding_tuple = entry_binding
                elif self.binding_tuple != entry_binding:
                    raise CollectionRefusal(
                        "manifest_preregistration_binding_changed"
                    )
                source_id = str(entry.get("market_source_id") or "").lower()
                segment_index = _strict_positive_int(
                    entry.get("segment_index"),
                    "manifest_segment_invalid",
                )
                utc_hour = str(entry.get("utc_hour") or "")
                try:
                    parsed_hour = datetime.strptime(utc_hour, "%Y%m%dT%H")
                except ValueError:
                    raise CollectionRefusal("manifest_utc_hour_invalid") from None
                if parsed_hour.strftime("%Y%m%dT%H") != utc_hour:
                    raise CollectionRefusal("manifest_utc_hour_invalid")
                if (
                    not _is_sha256(source_id)
                    or chunk.get("schema_version") != CHUNK_SCHEMA_VERSION
                    or chunk.get("utc_hour") != utc_hour
                    or chunk.get("segment_index") != segment_index
                    or str(
                        dict(chunk.get("source") or {}).get(
                            "market_source_id"
                        )
                        or ""
                    ).lower()
                    != source_id
                    or len(pure.parts) < 3
                    or pure.parts[1] != utc_hour
                ):
                    raise CollectionRefusal("manifest_chunk_identity_mismatch")
                if chunk.get("collection_only") is not True or any(
                    chunk.get(flag) is not False
                    for flag in (
                        "evaluation_performed",
                        "success_claim_authorized",
                        "authority_granted",
                        "activation_authorized",
                        "order_authorized",
                    )
                ):
                    raise CollectionRefusal("manifest_chunk_authority_invalid")
                bars = chunk.get("bars")
                quotes = chunk.get("quotes")
                if not isinstance(bars, list) or not isinstance(quotes, list):
                    raise CollectionRefusal("manifest_chunk_rows_invalid")
                if (
                    entry.get("bar_rows") != len(bars)
                    or entry.get("quote_rows") != len(quotes)
                ):
                    raise CollectionRefusal("manifest_chunk_row_count_mismatch")

                if not loaded_source_id:
                    if segment_index != 1:
                        raise CollectionRefusal("manifest_segment_invalid")
                    loaded_source_id = source_id
                    loaded_segment_index = segment_index
                elif source_id != loaded_source_id:
                    if segment_index != loaded_segment_index + 1:
                        raise CollectionRefusal("manifest_segment_transition_invalid")
                    loaded_source_id = source_id
                    loaded_segment_index = segment_index
                    loaded_last_bar = {symbol: 0 for symbol in SYMBOLS}
                    loaded_last_tick_sequence = {
                        symbol: 0 for symbol in SYMBOLS
                    }
                    loaded_last_tick_transport = {
                        symbol: 0.0 for symbol in SYMBOLS
                    }
                    loaded_last_snapshot_hash = {
                        symbol: _ZERO_SHA256 for symbol in SYMBOLS
                    }
                    self.recent_bar_hashes_by_symbol = {
                        symbol: {} for symbol in SYMBOLS
                    }
                elif segment_index != loaded_segment_index:
                    raise CollectionRefusal("manifest_segment_transition_invalid")

                for raw_bar in bars:
                    if not isinstance(raw_bar, Mapping):
                        raise CollectionRefusal("manifest_bar_invalid")
                    bar = dict(raw_bar)
                    symbol = str(bar.get("symbol") or "").strip().upper()
                    if symbol not in _SYMBOL_SET:
                        raise CollectionRefusal("manifest_bar_invalid")
                    minute_epoch = _strict_positive_int(
                        bar.get("minute_epoch"),
                        "manifest_bar_invalid",
                    )
                    if (
                        minute_epoch % 60 != 0
                        or minute_epoch <= loaded_last_bar[symbol]
                    ):
                        raise CollectionRefusal("manifest_bar_not_monotonic")
                    loaded_last_bar[symbol] = minute_epoch
                    recent = self.recent_bar_hashes_by_symbol[symbol]
                    recent[minute_epoch] = canonical_sha256(bar)
                    while len(recent) > 2_000:
                        recent.pop(min(recent))

                for raw_quote in quotes:
                    if not isinstance(raw_quote, Mapping):
                        raise CollectionRefusal("manifest_quote_invalid")
                    quote = dict(raw_quote)
                    symbol = str(quote.get("symbol") or "").strip().upper()
                    if symbol not in _SYMBOL_SET:
                        raise CollectionRefusal("manifest_quote_invalid")
                    sequence = _strict_positive_int(
                        quote.get("observation_sequence"),
                        "manifest_quote_invalid",
                    )
                    transport_epoch = _positive_float(
                        quote.get("transport_received_at_epoch"),
                        "manifest_quote_invalid",
                    )
                    _strict_nonnegative_int(
                        quote.get("market_event_sequence"),
                        "manifest_quote_invalid",
                    )
                    snapshot_hash = str(
                        quote.get("snapshot_sha256") or ""
                    ).lower()
                    if (
                        sequence != loaded_last_tick_sequence[symbol] + 1
                        or transport_epoch
                        <= loaded_last_tick_transport[symbol]
                        or not _is_sha256(snapshot_hash)
                        or datetime.fromtimestamp(
                            transport_epoch,
                            tz=UTC,
                        ).strftime("%Y%m%dT%H")
                        != utc_hour
                    ):
                        raise CollectionRefusal("manifest_quote_not_monotonic")
                    loaded_last_tick_sequence[symbol] = sequence
                    loaded_last_tick_transport[symbol] = transport_epoch
                    loaded_last_snapshot_hash[symbol] = snapshot_hash

                entry_last_bar = _symbol_int_map(
                    entry.get("last_bar_epoch_by_symbol"),
                    "manifest_last_bar_map_invalid",
                )
                entry_last_tick = _symbol_int_map(
                    entry.get("last_tick_sequence_by_symbol"),
                    "manifest_last_tick_map_invalid",
                )
                entry_last_transport = _symbol_float_map(
                    entry.get("last_tick_transport_epoch_by_symbol"),
                    "manifest_last_tick_transport_map_invalid",
                )
                entry_last_snapshot = _symbol_hash_map(
                    entry.get("last_tick_snapshot_sha256_by_symbol"),
                    "manifest_last_tick_snapshot_map_invalid",
                )
                chunk_maps = (
                    ("last_bar_epoch_by_symbol", entry_last_bar),
                    ("last_tick_sequence_by_symbol", entry_last_tick),
                    (
                        "last_tick_transport_epoch_by_symbol",
                        entry_last_transport,
                    ),
                    (
                        "last_tick_snapshot_sha256_by_symbol",
                        entry_last_snapshot,
                    ),
                )
                if any(chunk.get(name) != value for name, value in chunk_maps):
                    raise CollectionRefusal("manifest_chunk_state_map_mismatch")
                if (
                    entry_last_bar != loaded_last_bar
                    or entry_last_tick != loaded_last_tick_sequence
                    or entry_last_transport != loaded_last_tick_transport
                    or entry_last_snapshot != loaded_last_snapshot_hash
                ):
                    raise CollectionRefusal("manifest_state_map_invalid")
                referenced.add(relative)
                prior_hash = claimed
                entry["manifest_entry_sha256"] = claimed
                self.entries.append(entry)
            if self.entries:
                latest = self.entries[-1]
                self.next_sequence = int(latest["sequence"]) + 1
                self.last_entry_sha256 = str(latest["manifest_entry_sha256"])
                self.last_source_id = str(latest["market_source_id"])
                self.last_segment_index = int(latest["segment_index"])
                self.last_bar_epoch_by_symbol = loaded_last_bar
                self.last_tick_sequence_by_symbol = loaded_last_tick_sequence
                self.last_tick_transport_epoch_by_symbol = (
                    loaded_last_tick_transport
                )
                self.last_tick_snapshot_sha256_by_symbol = (
                    loaded_last_snapshot_hash
                )
        actual = {
            path.relative_to(self.root).as_posix()
            for path in self.chunks_root.rglob("*.json")
            if path.is_file()
        }
        if actual != referenced:
            raise CollectionRefusal("orphan_or_missing_chunk_detected")

    def emit(self, chunk: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(chunk or {})
        if payload.get("schema_version") != CHUNK_SCHEMA_VERSION:
            raise CollectionRefusal("chunk_schema_invalid")
        sequence = self.next_sequence
        utc_hour = str(payload.get("utc_hour") or "")
        segment_index = int(payload.get("segment_index") or 0)
        source_id = str(
            dict(payload.get("source") or {}).get("market_source_id") or ""
        )
        binding_tuple = _validated_binding_tuple(
            payload,
            reason="chunk_preregistration_binding_invalid",
        )
        if self.binding_tuple is not None and self.binding_tuple != binding_tuple:
            raise CollectionRefusal("chunk_preregistration_binding_changed")
        if not utc_hour or segment_index <= 0 or not _is_sha256(source_id):
            raise CollectionRefusal("chunk_identity_invalid")
        try:
            parsed_hour = datetime.strptime(utc_hour, "%Y%m%dT%H")
        except ValueError:
            raise CollectionRefusal("chunk_utc_hour_invalid") from None
        if parsed_hour.strftime("%Y%m%dT%H") != utc_hour:
            raise CollectionRefusal("chunk_utc_hour_invalid")
        filename = (
            f"ig-mt4-m1-activity-s{segment_index:04d}-"
            f"q{sequence:010d}.json"
        )
        relative = PurePosixPath(CHUNK_DIRECTORY, utc_hour, filename).as_posix()
        chunk_path = self.root.joinpath(*PurePosixPath(relative).parts)
        chunk_bytes = canonical_json_bytes(payload) + b"\n"
        bars = list(payload.get("bars") or [])
        quotes = list(payload.get("quotes") or [])
        last_bars = _symbol_int_map(
            payload.get("last_bar_epoch_by_symbol"),
            "chunk_last_bar_map_invalid",
        )
        last_ticks = _symbol_int_map(
            payload.get("last_tick_sequence_by_symbol"),
            "chunk_last_tick_map_invalid",
        )
        last_transport = _symbol_float_map(
            payload.get("last_tick_transport_epoch_by_symbol"),
            "chunk_last_tick_transport_map_invalid",
        )
        last_snapshot_hash = _symbol_hash_map(
            payload.get("last_tick_snapshot_sha256_by_symbol"),
            "chunk_last_tick_snapshot_map_invalid",
        )
        _atomic_write_new(chunk_path, chunk_bytes)
        entry_body = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "sequence": sequence,
            "previous_entry_sha256": self.last_entry_sha256,
            "chunk_path": relative,
            "chunk_sha256": _sha256_bytes(chunk_bytes),
            "chunk_size_bytes": len(chunk_bytes),
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "utc_hour": utc_hour,
            "segment_index": segment_index,
            "market_source_id": source_id,
            "preregistration_body_sha256": binding_tuple[0],
            "preregistration_artifact_sha256": binding_tuple[1],
            "prospective_t0_utc_inclusive": binding_tuple[2],
            "prospective_end_utc_exclusive": binding_tuple[3],
            "bar_rows": len(bars),
            "quote_rows": len(quotes),
            "last_bar_epoch_by_symbol": {
                symbol: int(last_bars.get(symbol, 0)) for symbol in SYMBOLS
            },
            "last_tick_sequence_by_symbol": {
                symbol: int(last_ticks.get(symbol, 0)) for symbol in SYMBOLS
            },
            "last_tick_transport_epoch_by_symbol": {
                symbol: float(last_transport[symbol]) for symbol in SYMBOLS
            },
            "last_tick_snapshot_sha256_by_symbol": {
                symbol: last_snapshot_hash[symbol] for symbol in SYMBOLS
            },
        }
        entry_hash = canonical_sha256(entry_body)
        entry = {**entry_body, "manifest_entry_sha256": entry_hash}
        line = canonical_json_bytes(entry) + b"\n"
        try:
            descriptor = os.open(
                self.manifest_path,
                os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            try:
                os.write(descriptor, line)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise CollectionRefusal("manifest_append_failed") from exc
        self.entries.append(entry)
        self.next_sequence += 1
        self.last_entry_sha256 = entry_hash
        self.binding_tuple = binding_tuple
        source_changed = bool(
            self.last_source_id and self.last_source_id != source_id
        )
        self.last_source_id = source_id
        self.last_segment_index = segment_index
        self.last_bar_epoch_by_symbol = {
            symbol: int(last_bars.get(symbol, 0)) for symbol in SYMBOLS
        }
        self.last_tick_sequence_by_symbol = {
            symbol: int(last_ticks.get(symbol, 0)) for symbol in SYMBOLS
        }
        self.last_tick_transport_epoch_by_symbol = {
            symbol: float(last_transport[symbol]) for symbol in SYMBOLS
        }
        self.last_tick_snapshot_sha256_by_symbol = {
            symbol: last_snapshot_hash[symbol] for symbol in SYMBOLS
        }
        if source_changed:
            self.recent_bar_hashes_by_symbol = {
                symbol: {} for symbol in SYMBOLS
            }
        for raw_bar in bars:
            bar = dict(raw_bar or {})
            symbol = str(bar.get("symbol") or "").strip().upper()
            minute_epoch = int(bar.get("minute_epoch") or 0)
            if symbol in _SYMBOL_SET and minute_epoch > 0:
                recent = self.recent_bar_hashes_by_symbol[symbol]
                recent[minute_epoch] = canonical_sha256(bar)
                while len(recent) > 2_000:
                    recent.pop(min(recent))
        return entry


@dataclass(frozen=True, slots=True)
class CollectionPolicy:
    bar_limit: int = DEFAULT_BAR_LIMIT
    tick_interval_secs: float = DEFAULT_TICK_INTERVAL_SECS
    bar_interval_secs: float = DEFAULT_BAR_INTERVAL_SECS
    rollover_mode: Literal["refuse", "split"] = "refuse"

    def validate(self) -> None:
        if (
            isinstance(self.bar_limit, bool)
            or not isinstance(self.bar_limit, int)
            or not MINIMUM_M1_BARS <= self.bar_limit <= 2_000
        ):
            raise CollectionRefusal("bar_limit_invalid")
        tick_interval = _positive_float(
            self.tick_interval_secs,
            "tick_interval_invalid",
        )
        if tick_interval > MAXIMUM_TICK_INTERVAL_SECS:
            raise CollectionRefusal("tick_interval_exceeds_mtvclc_gap")
        if _positive_float(
            self.bar_interval_secs,
            "bar_interval_invalid",
        ) > 60.0:
            raise CollectionRefusal("bar_interval_exceeds_m1_cadence")
        if self.rollover_mode not in {"refuse", "split"}:
            raise CollectionRefusal("rollover_mode_invalid")


class ProspectiveActivityCollector:
    """Validate one-source snapshots and emit sanitized immutable chunks."""

    def __init__(
        self,
        *,
        client: BridgeReadClient,
        ledger: ManifestLedger,
        binding: ProspectiveBinding,
        policy: CollectionPolicy = CollectionPolicy(),
        clock: Callable[[], float] = time.time,
    ) -> None:
        policy.validate()
        self.client = client
        self.ledger = ledger
        self.binding = binding
        self.policy = policy
        self.clock = clock
        expected_binding = (
            binding.preregistration_body_sha256,
            binding.preregistration_artifact_sha256,
            binding.t0_utc,
            binding.end_utc_exclusive,
        )
        if ledger.binding_tuple is not None and ledger.binding_tuple != expected_binding:
            raise CollectionRefusal("ledger_preregistration_binding_mismatch")
        self.current_source_id = ledger.last_source_id
        self.segment_index = ledger.last_segment_index
        self.last_bar_epoch_by_symbol = dict(ledger.last_bar_epoch_by_symbol)
        self.last_tick_sequence_by_symbol = dict(
            ledger.last_tick_sequence_by_symbol
        )
        self.last_tick_transport_epoch_by_symbol = dict(
            ledger.last_tick_transport_epoch_by_symbol
        )
        self.last_tick_snapshot_sha256_by_symbol = dict(
            ledger.last_tick_snapshot_sha256_by_symbol
        )
        self.recent_bar_hashes_by_symbol = {
            symbol: dict(ledger.recent_bar_hashes_by_symbol[symbol])
            for symbol in SYMBOLS
        }

    def _candidate_segment(self, source: SourceIdentity) -> tuple[int, bool]:
        if not self.current_source_id:
            return max(1, self.segment_index + 1), True
        if self.current_source_id == source.source_id:
            return self.segment_index, False
        if self.policy.rollover_mode == "refuse":
            raise CollectionRefusal("market_source_rollover_refused")
        return self.segment_index + 1, True

    def capture_cycle(self, *, include_bars: bool) -> dict[str, Any] | None:
        cycle_started_at = _positive_float(
            self.clock(),
            "collector_clock_invalid",
        )
        if cycle_started_at < self.binding.t0_epoch:
            raise CollectionRefusal("prospective_window_not_started")
        request_count = 3 + (len(SYMBOLS) if include_bars else 0)
        latest_safe_start = self.binding.end_epoch_exclusive - (
            request_count * self.client.timeout_secs
        )
        if cycle_started_at >= latest_safe_start:
            raise CollectionRefusal("prospective_cycle_deadline_insufficient")
        state = self.client.get_state()
        source = source_from_state(
            state,
            observed_at_epoch=cycle_started_at,
        )
        candidate_segment, reset_segment = self._candidate_segment(source)
        base_last_bar = (
            {symbol: 0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_bar_epoch_by_symbol)
        )
        base_tick_sequence = (
            {symbol: 0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_sequence_by_symbol)
        )
        base_tick_transport = (
            {symbol: 0.0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_transport_epoch_by_symbol)
        )
        base_snapshot_hash = (
            {symbol: _ZERO_SHA256 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_snapshot_sha256_by_symbol)
        )
        base_recent_bar_hashes = (
            {symbol: {} for symbol in SYMBOLS}
            if reset_segment
            else {
                symbol: dict(self.recent_bar_hashes_by_symbol[symbol])
                for symbol in SYMBOLS
            }
        )

        ticks_payload = self.client.get_ticks()
        quotes = validate_tick_snapshot(
            ticks_payload,
            expected_source=source,
            observed_at_epoch=cycle_started_at,
            prior_sequences=base_tick_sequence,
            prior_transport_epochs=base_tick_transport,
            prior_snapshot_sha256=base_snapshot_hash,
        )

        candidate_bars: dict[str, tuple[M1ActivityBar, ...]] = {}
        final_last_bar = dict(base_last_bar)
        final_recent_bar_hashes = {
            symbol: dict(base_recent_bar_hashes[symbol])
            for symbol in SYMBOLS
        }
        if include_bars:
            for symbol in SYMBOLS:
                raw = self.client.get_m1_bars(
                    symbol,
                    limit=self.policy.bar_limit,
                )
                validated = validate_m1_bar_response(
                    raw,
                    symbol=symbol,
                    expected_source=source,
                    observed_at_epoch=cycle_started_at,
                    requested_limit=self.policy.bar_limit,
                )
                last_epoch = int(base_last_bar.get(symbol, 0))
                fresh_rows: list[M1ActivityBar] = []
                recent = final_recent_bar_hashes[symbol]
                for bar in validated:
                    digest = canonical_sha256(bar.to_dict())
                    prior_digest = recent.get(bar.minute_epoch)
                    if prior_digest is not None:
                        if prior_digest != digest:
                            raise CollectionRefusal("completed_bar_mutated")
                        continue
                    if bar.minute_epoch <= last_epoch:
                        raise CollectionRefusal(
                            "completed_bar_overlap_unverifiable"
                        )
                    fresh_rows.append(bar)
                    recent[bar.minute_epoch] = digest
                while len(recent) > 2_000:
                    recent.pop(min(recent))
                fresh = tuple(fresh_rows)
                candidate_bars[symbol] = fresh
                if fresh:
                    final_last_bar[symbol] = fresh[-1].minute_epoch

        cycle_completed_at = _positive_float(
            self.clock(),
            "collector_clock_invalid",
        )
        if cycle_completed_at < cycle_started_at:
            raise CollectionRefusal("collector_clock_regressed")
        if cycle_completed_at >= self.binding.end_epoch_exclusive:
            raise CollectionRefusal("prospective_window_closed_during_cycle")
        final_state = self.client.get_state()
        final_source = source_from_state(
            final_state,
            observed_at_epoch=cycle_completed_at,
        )
        if final_source != source:
            raise CollectionRefusal("market_source_rollover_during_cycle")

        flat_bars = [
            bar.to_dict()
            for symbol in SYMBOLS
            for bar in candidate_bars.get(symbol, ())
        ]
        quote_groups: dict[str, list[TickObservation]] = {}
        for quote in quotes:
            hour = datetime.fromtimestamp(
                quote.transport_received_at_epoch,
                tz=UTC,
            ).strftime("%Y%m%dT%H")
            quote_groups.setdefault(hour, []).append(quote)
        bar_hour = datetime.fromtimestamp(
            cycle_completed_at,
            tz=UTC,
        ).strftime("%Y%m%dT%H")
        hours = sorted(set(quote_groups) | ({bar_hour} if flat_bars else set()))
        if not hours:
            return None

        running_last_bar = dict(base_last_bar)
        running_tick_sequence = dict(base_tick_sequence)
        running_tick_transport = dict(base_tick_transport)
        running_snapshot_hash = dict(base_snapshot_hash)
        latest_entry: dict[str, Any] | None = None
        for utc_hour in hours:
            hour_quotes = quote_groups.get(utc_hour, [])
            for quote in hour_quotes:
                running_tick_sequence[quote.symbol] = (
                    quote.observation_sequence
                )
                running_tick_transport[quote.symbol] = (
                    quote.transport_received_at_epoch
                )
                running_snapshot_hash[quote.symbol] = quote.snapshot_sha256
            hour_bars = flat_bars if utc_hour == bar_hour else []
            if hour_bars:
                running_last_bar = dict(final_last_bar)
            chunk = {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "collector_schema_version": COLLECTOR_SCHEMA_VERSION,
                "source_contract_id": SOURCE_CONTRACT_ID,
                "activity_metric_id": ACTIVITY_METRIC_ID,
                "scope_version": SCOPE_VERSION,
                "symbol_scope": list(SYMBOLS),
                "timeframe": TIMEFRAME,
                "minimum_m1_history_bars": MINIMUM_M1_BARS,
                "maximum_quote_gap_seconds": MAXIMUM_TICK_INTERVAL_SECS,
                "requested_bar_limit": self.policy.bar_limit,
                "configured_tick_interval_seconds": (
                    self.policy.tick_interval_secs
                ),
                "utc_hour": utc_hour,
                "segment_index": candidate_segment,
                "collector_cycle_started_at_epoch": cycle_started_at,
                "collector_cycle_completed_at_epoch": cycle_completed_at,
                "observed_at_epoch": cycle_completed_at,
                **self.binding.chunk_fields(),
                "source": source.sanitized(),
                "bars": hour_bars,
                "quotes": [quote.to_dict() for quote in hour_quotes],
                "last_bar_epoch_by_symbol": dict(running_last_bar),
                "last_tick_sequence_by_symbol": dict(
                    running_tick_sequence
                ),
                "last_tick_transport_epoch_by_symbol": dict(
                    running_tick_transport
                ),
                "last_tick_snapshot_sha256_by_symbol": dict(
                    running_snapshot_hash
                ),
                "collection_only": True,
                "evaluation_performed": False,
                "success_claim_authorized": False,
                "authority_granted": False,
                "activation_authorized": False,
                "order_authorized": False,
            }
            latest_entry = self.ledger.emit(chunk)
            self.current_source_id = source.source_id
            self.segment_index = candidate_segment
            self.last_bar_epoch_by_symbol = dict(running_last_bar)
            self.last_tick_sequence_by_symbol = dict(running_tick_sequence)
            self.last_tick_transport_epoch_by_symbol = dict(
                running_tick_transport
            )
            self.last_tick_snapshot_sha256_by_symbol = dict(
                running_snapshot_hash
            )
            if hour_bars:
                self.recent_bar_hashes_by_symbol = {
                    symbol: dict(final_recent_bar_hashes[symbol])
                    for symbol in SYMBOLS
                }
            elif reset_segment:
                self.recent_bar_hashes_by_symbol = {
                    symbol: dict(base_recent_bar_hashes[symbol])
                    for symbol in SYMBOLS
                }
        return latest_entry


def read_api_key_file(path: str | Path) -> str:
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_file() or target.is_symlink():
        raise CollectionRefusal("bridge_api_key_file_invalid")
    try:
        size = target.stat().st_size
        if size <= 0 or size > MAXIMUM_API_KEY_BYTES:
            raise CollectionRefusal("bridge_api_key_file_size_invalid")
        key = target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise CollectionRefusal("bridge_api_key_file_unreadable") from exc
    if not key:
        raise CollectionRefusal("bridge_api_key_missing")
    return key


def run_collection(
    *,
    collector: ProspectiveActivityCollector,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    """Collect only inside the hash-bound absolute prospective window."""

    binding = collector.binding
    now = _positive_float(clock(), "collector_clock_invalid")
    while now < binding.t0_epoch:
        sleep(min(1.0, binding.t0_epoch - now))
        now = _positive_float(clock(), "collector_clock_invalid")
    if now >= binding.end_epoch_exclusive:
        raise CollectionRefusal("prospective_window_closed")
    next_bar_capture = now
    cycles = 0
    tick_budget = 3 * collector.client.timeout_secs
    bar_budget = (3 + len(SYMBOLS)) * collector.client.timeout_secs
    while True:
        cycle_started = _positive_float(clock(), "collector_clock_invalid")
        if cycle_started >= binding.end_epoch_exclusive - tick_budget:
            break
        include_bars = cycle_started >= next_bar_capture
        if include_bars and cycle_started >= binding.end_epoch_exclusive - bar_budget:
            include_bars = False
        collector.capture_cycle(include_bars=include_bars)
        cycles += 1
        if include_bars:
            next_bar_capture = cycle_started + float(
                collector.policy.bar_interval_secs
            )
        current = _positive_float(clock(), "collector_clock_invalid")
        remaining = binding.end_epoch_exclusive - tick_budget - current
        if remaining <= 0.0:
            break
        sleep_for = min(
            max(
                0.0,
                float(collector.policy.tick_interval_secs)
                - (current - cycle_started),
            ),
            remaining,
        )
        if sleep_for > 0.0:
            sleep(sleep_for)
    return cycles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect prospective authenticated IG-MT4 M1 activity inputs."
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--tick-interval-secs", type=float, default=DEFAULT_TICK_INTERVAL_SECS)
    parser.add_argument("--bar-interval-secs", type=float, default=DEFAULT_BAR_INTERVAL_SECS)
    parser.add_argument("--bar-limit", type=int, default=DEFAULT_BAR_LIMIT)
    parser.add_argument("--http-timeout-secs", type=float, default=DEFAULT_HTTP_TIMEOUT_SECS)
    parser.add_argument(
        "--rollover-mode",
        choices=("refuse", "split"),
        default="refuse",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        binding = load_preregistration(args.preregistration)
        api_key = read_api_key_file(args.api_key_file)
        client = BridgeReadClient(
            base_url=args.base_url,
            api_key=api_key,
            timeout_secs=args.http_timeout_secs,
        )
        ledger = ManifestLedger(args.output_dir)
        policy = CollectionPolicy(
            bar_limit=args.bar_limit,
            tick_interval_secs=args.tick_interval_secs,
            bar_interval_secs=args.bar_interval_secs,
            rollover_mode=args.rollover_mode,
        )
        collector = ProspectiveActivityCollector(
            client=client,
            ledger=ledger,
            binding=binding,
            policy=policy,
        )
        now = time.time()
        while now < binding.t0_epoch:
            time.sleep(min(1.0, binding.t0_epoch - now))
            now = time.time()
        if now >= binding.end_epoch_exclusive:
            raise CollectionRefusal("prospective_window_closed")
        client.prove_authentication_required()
        cycles = run_collection(collector=collector)
    except CollectionRefusal as exc:
        print(f"capture refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "collected",
                "collection_only": True,
                "cycles": cycles,
                "preregistration_body_sha256": (
                    binding.preregistration_body_sha256
                ),
                "manifest": str((Path(args.output_dir) / MANIFEST_FILENAME).resolve()),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
