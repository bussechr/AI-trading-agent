"""Capture authenticated IG-DEMO executable quotes for external cost calibration.

This operator tool is deliberately read-only.  It permits only authenticated
GETs of bridge state, current ticks, and broker symbol specifications.  The
portable output contains no bridge credential or plaintext broker-account
scope, and it cannot submit, poll, acknowledge, or otherwise inspect commands.
"""

from __future__ import annotations

# AGENT: ROLE: Read-only producer of sanitized IG-DEMO cost-calibration inputs.
# AGENT: HANDSHAKE: Authenticated bridge ticks/specs -> isolated external validation.
# AGENT: ISOLATION: Output is an immutable portable input; this tool never enters the evaluator.
# AGENT: SIDE EFFECTS: Authenticated GETs and one atomic local artifact-directory creation only.

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import numpy as np
from sqlalchemy import create_engine, text


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _PROJECT_ROOT / "fx-quant-stack" / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from fxstack.providers.ig_mt4_catalog import (  # noqa: E402
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.market_source_identity import (  # noqa: E402
    AuthenticatedMarketSource,
    authenticated_market_source_from_row,
    current_authenticated_market_source,
    market_source_row_error,
)


CAPTURE_SCHEMA_VERSION = "fxstack.external_ig_mt4_bid_ask_capture.v1"
CAPTURE_DEFINITION = "authenticated_ig_demo_live_quote_calibration.v1"
FULL_HISTORY_CAPTURE_DEFINITION = (
    "authenticated_ig_demo_tick_microstructure_snapshot.v1"
)
FULL_HISTORY_CAPTURE_MODE = "authenticated_same_source_db_history_full"
SOURCE_ID = "authenticated_ig_demo_mt4_bridge"
MARKET_SOURCE_AUDIT_SCHEMA = "fxstack.external_ig_mt4_market_source_audit.v1"
BROKER_CONTRACT_AUDIT_SCHEMA = "fxstack.external_ig_mt4_contract_audit.v1"
POINT_IN_TIME_AUDIT_SCHEMA = "fxstack.external_ig_mt4_capture_point_in_time_audit.v1"
EXECUTION_CONTRACT_SCHEMA = "fxstack.production_scalp_execution_tolerance.v1"
PRODUCTION_MAX_SLIPPAGE_POINTS = 20
EXECUTION_TOLERANCE_SEMANTICS = (
    "configured_broker_execution_tolerance_not_observed_slippage"
)

MINIMUM_SAMPLES_PER_SYMBOL = 300
HISTORY_MINIMUM_SAMPLES_PER_SYMBOL = 100
DEFAULT_FULL_HISTORY_MAX_ROWS_PER_SYMBOL = 250_000
MINIMUM_CAPTURE_DURATION_SECS = 300.0
MAXIMUM_SAMPLE_GAP_SECS = 5.0
DEFAULT_CAPTURE_TIMEOUT_SECS = 1_800.0
DEFAULT_POLL_INTERVAL_SECS = 0.2
DEFAULT_IDENTITY_RECHECK_SECS = 10.0
DEFAULT_SPECS_RECHECK_SECS = 30.0
DEFAULT_HTTP_TIMEOUT_SECS = 3.0
MAX_HTTP_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_API_KEY_BYTES = 64 * 1024
_CLOCK_TOLERANCE_SECS = 5.0

NPZ_FILENAME = "ig_mt4_bid_ask_samples.npz"
CAPTURE_FILENAME = "ig_mt4_bid_ask_capture.json"
NPZ_ARRAY_DTYPES: dict[str, str] = {
    "symbol_index": "int64",
    "sample_epoch": "float64",
    "broker_quote_epoch": "float64",
    "received_at_epoch": "float64",
    "market_event_received_at_epoch": "float64",
    "source_event_sequence": "int64",
    "source_event_token_sha256": "S64",
    "bid": "float64",
    "ask": "float64",
    "point": "float64",
    "price_tick_size": "float64",
    "digits": "int64",
    "trade_allowed": "bool",
}

_ALLOWED_GET_PATHS = frozenset(
    {"/v2/state", "/v2/market/ticks", "/v2/market/specs"}
)


class CaptureRefusal(RuntimeError):
    """Fail-closed refusal carrying a non-secret stable reason."""


@dataclass(frozen=True, slots=True)
class BrokerSpec:
    point: float
    price_tick_size: float
    digits: int
    trade_allowed: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "price_tick_size": self.price_tick_size,
            "digits": self.digits,
            "trade_allowed": self.trade_allowed,
        }


@dataclass(frozen=True, slots=True)
class QuoteSample:
    symbol_index: int
    sample_epoch: float
    broker_quote_epoch: float
    received_at_epoch: float
    market_event_received_at_epoch: float
    source_event_sequence: int
    source_event_token_sha256: bytes
    bid: float
    ask: float
    point: float
    price_tick_size: float
    digits: int
    trade_allowed: bool


@dataclass(frozen=True, slots=True)
class CapturePolicy:
    minimum_samples_per_symbol: int = MINIMUM_SAMPLES_PER_SYMBOL
    minimum_duration_secs: float = MINIMUM_CAPTURE_DURATION_SECS
    maximum_sample_gap_secs: float = MAXIMUM_SAMPLE_GAP_SECS
    capture_timeout_secs: float = DEFAULT_CAPTURE_TIMEOUT_SECS
    poll_interval_secs: float = DEFAULT_POLL_INTERVAL_SECS
    identity_recheck_secs: float = DEFAULT_IDENTITY_RECHECK_SECS
    specs_recheck_secs: float = DEFAULT_SPECS_RECHECK_SECS
    enforce_maximum_sample_gap: bool = True


@dataclass(slots=True)
class AuditChain:
    count: int = 0
    first_epoch: float = 0.0
    last_epoch: float = 0.0
    digest: str = "0" * 64

    def observe(self, payload: Mapping[str, Any], *, observed_at: float) -> None:
        epoch = _finite_positive_float(observed_at, "audit_observed_at_invalid")
        body_hash = canonical_sha256(dict(payload))
        self.digest = hashlib.sha256(
            bytes.fromhex(self.digest) + bytes.fromhex(body_hash)
        ).hexdigest()
        self.count += 1
        if self.first_epoch <= 0.0:
            self.first_epoch = epoch
        self.last_epoch = epoch


class BridgeReadClient:
    """Small fail-closed client whose route allowlist cannot reach commands."""

    def __init__(self, *, base_url: str, api_key: str, timeout_secs: float) -> None:
        self.base_url = _validated_loopback_base_url(base_url)
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise CaptureRefusal("bridge_api_key_missing")
        self.timeout_secs = max(0.1, float(timeout_secs))

    def _request(self, path: str, *, authenticated: bool) -> dict[str, Any]:
        if path not in _ALLOWED_GET_PATHS:
            raise CaptureRefusal("bridge_read_path_forbidden")
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["X-API-Key"] = self._api_key
        request = Request(f"{self.base_url}{path}", headers=headers, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_secs) as response:  # noqa: S310
                status = int(getattr(response, "status", 0) or 0)
                raw = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            raise CaptureRefusal(f"bridge_http_status_{int(exc.code)}") from None
        except (URLError, TimeoutError, OSError):
            raise CaptureRefusal("bridge_read_failed") from None
        if status != 200:
            raise CaptureRefusal(f"bridge_http_status_{status}")
        if not raw or len(raw) > MAX_HTTP_RESPONSE_BYTES:
            raise CaptureRefusal("bridge_response_size_invalid")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise CaptureRefusal("bridge_response_json_invalid") from None
        if not isinstance(payload, dict):
            raise CaptureRefusal("bridge_response_shape_invalid")
        return payload

    def prove_authentication_required(self) -> None:
        """Require the protected state route to reject a request without the key."""

        try:
            self._request("/v2/state", authenticated=False)
        except CaptureRefusal as exc:
            if str(exc) == "bridge_http_status_401":
                return
            raise CaptureRefusal("bridge_authentication_not_proven") from None
        raise CaptureRefusal("bridge_authentication_not_required")

    def get(self, path: str) -> dict[str, Any]:
        return self._request(path, authenticated=True)


def canonical_json_bytes(payload: Any) -> bytes:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise CaptureRefusal("capture_payload_not_canonical") from exc
    return encoded.encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _sha256_text(value: Any, reason: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise CaptureRefusal(reason)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_float(value: Any, reason: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise CaptureRefusal(reason) from None
    if not math.isfinite(number):
        raise CaptureRefusal(reason)
    return number


def _finite_positive_float(value: Any, reason: str) -> float:
    number = _finite_float(value, reason)
    if number <= 0.0:
        raise CaptureRefusal(reason)
    return number


def _strict_integer(value: Any, reason: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise CaptureRefusal(reason)
    number = _finite_float(value, reason)
    integer = int(number)
    if float(integer) != number or integer < minimum:
        raise CaptureRefusal(reason)
    return integer


def _validated_loopback_base_url(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or str(parsed.hostname or "").lower() not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CaptureRefusal("bridge_url_not_strict_loopback_root")
    try:
        port = parsed.port
    except ValueError:
        raise CaptureRefusal("bridge_url_port_invalid") from None
    if port is None or not (1 <= port <= 65535):
        raise CaptureRefusal("bridge_url_port_missing")
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    return f"{parsed.scheme}://{host}:{port}"


def _validate_policy(policy: CapturePolicy, *, production: bool) -> None:
    if policy.minimum_samples_per_symbol < 1:
        raise CaptureRefusal("capture_minimum_samples_invalid")
    if not math.isfinite(policy.minimum_duration_secs) or policy.minimum_duration_secs <= 0:
        raise CaptureRefusal("capture_minimum_duration_invalid")
    if not math.isfinite(policy.maximum_sample_gap_secs) or policy.maximum_sample_gap_secs <= 0:
        raise CaptureRefusal("capture_maximum_gap_invalid")
    if policy.capture_timeout_secs < policy.minimum_duration_secs:
        raise CaptureRefusal("capture_timeout_too_short")
    if policy.poll_interval_secs <= 0 or (
        policy.enforce_maximum_sample_gap
        and policy.poll_interval_secs > policy.maximum_sample_gap_secs
    ):
        raise CaptureRefusal("capture_poll_interval_invalid")
    if policy.identity_recheck_secs <= 0 or policy.specs_recheck_secs <= 0:
        raise CaptureRefusal("capture_recheck_interval_invalid")
    if production and (
        policy.minimum_samples_per_symbol < MINIMUM_SAMPLES_PER_SYMBOL
        or policy.minimum_duration_secs < MINIMUM_CAPTURE_DURATION_SECS
        or policy.maximum_sample_gap_secs > MAXIMUM_SAMPLE_GAP_SECS
        or not policy.enforce_maximum_sample_gap
    ):
        raise CaptureRefusal("capture_policy_weaker_than_production_minimum")


def _validated_state_source(
    state: Mapping[str, Any],
    *,
    expected: AuthenticatedMarketSource | None = None,
    now_epoch: float | None = None,
) -> AuthenticatedMarketSource:
    if str(state.get("broker_account_mode") or "").strip().lower() != "demo":
        raise CaptureRefusal("broker_account_mode_not_demo")
    if str(state.get("broker_venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        raise CaptureRefusal("broker_venue_not_ig_mt4")
    source, error = current_authenticated_market_source(
        state,
        now_epoch=now_epoch,
        require_active_lease=True,
    )
    if source is None or error:
        raise CaptureRefusal(f"broker_market_source_invalid:{error or 'missing'}")
    heartbeat_source, heartbeat_error = authenticated_market_source_from_row(
        state.get("bridge_market_source")
        if isinstance(state.get("bridge_market_source"), Mapping)
        else None
    )
    if heartbeat_source is None or heartbeat_error or heartbeat_source != source:
        raise CaptureRefusal(
            f"broker_heartbeat_source_invalid:{heartbeat_error or 'mismatch'}"
        )
    if expected is not None and source != expected:
        raise CaptureRefusal("broker_market_source_identity_changed")
    return source


def _validated_specs(
    payload: Mapping[str, Any],
    *,
    source: AuthenticatedMarketSource,
    expected: Mapping[str, BrokerSpec] | None = None,
) -> dict[str, BrokerSpec]:
    raw_source = payload.get("market_source")
    source_error = market_source_row_error(
        raw_source if isinstance(raw_source, Mapping) else None,
        expected=source,
    )
    if source_error:
        raise CaptureRefusal(f"broker_specs_source_invalid:{source_error}")
    if str(payload.get("market_source_id") or "").strip().lower() != source.source_id:
        raise CaptureRefusal("broker_specs_source_id_mismatch")
    raw_specs = payload.get("specs")
    if not isinstance(raw_specs, Mapping):
        raise CaptureRefusal("broker_specs_missing")
    out: dict[str, BrokerSpec] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw = raw_specs.get(symbol)
        if not isinstance(raw, Mapping):
            raise CaptureRefusal(f"broker_spec_missing:{symbol}")
        point = _finite_positive_float(raw.get("point"), f"broker_spec_point_invalid:{symbol}")
        tick_size = _finite_positive_float(
            raw.get("tick_size"), f"broker_spec_tick_size_invalid:{symbol}"
        )
        digits = _strict_integer(raw.get("digits"), f"broker_spec_digits_invalid:{symbol}")
        trade_allowed = raw.get("trade_allowed")
        if trade_allowed is not True:
            raise CaptureRefusal(f"broker_spec_trade_not_allowed:{symbol}")
        if digits > 12 or tick_size + 1e-15 < point:
            raise CaptureRefusal(f"broker_spec_geometry_invalid:{symbol}")
        out[symbol] = BrokerSpec(
            point=point,
            price_tick_size=tick_size,
            digits=digits,
            trade_allowed=True,
        )
    if expected is not None and out != dict(expected):
        raise CaptureRefusal("broker_specs_changed_during_capture")
    return out


def _quote_sample(
    *,
    symbol: str,
    symbol_index: int,
    row: Mapping[str, Any],
    spec: BrokerSpec,
    source: AuthenticatedMarketSource,
    sample_epoch: float,
    previous_sequence: int | None,
) -> QuoteSample | None:
    source_error = market_source_row_error(row, expected=source)
    if source_error:
        raise CaptureRefusal(f"broker_tick_source_invalid:{symbol}:{source_error}")
    if str(row.get("symbol") or "").strip().upper() != symbol:
        raise CaptureRefusal(f"broker_tick_symbol_invalid:{symbol}")
    if row.get("transport_fresh") is not True:
        raise CaptureRefusal(f"broker_tick_transport_stale:{symbol}")
    if row.get("market_event_identity_present") is not True:
        raise CaptureRefusal(f"broker_tick_event_identity_missing:{symbol}")
    if row.get("market_event_fresh") is not True:
        raise CaptureRefusal(f"broker_tick_event_stale:{symbol}")
    if row.get("source_event_baseline_initialized") is not True:
        raise CaptureRefusal(f"broker_tick_event_baseline_missing:{symbol}")

    sequence = _strict_integer(
        row.get("market_event_sequence"),
        f"broker_tick_event_sequence_invalid:{symbol}",
        minimum=1,
    )
    if previous_sequence is not None:
        if sequence < previous_sequence:
            raise CaptureRefusal(f"broker_tick_event_sequence_regressed:{symbol}")
        if sequence == previous_sequence:
            return None

    token = str(
        row.get("source_event_last_token") or row.get("source_event_token") or ""
    ).strip()
    if not token:
        raise CaptureRefusal(f"broker_tick_event_token_missing:{symbol}")
    bid = _finite_positive_float(row.get("bid"), f"broker_tick_bid_invalid:{symbol}")
    ask = _finite_positive_float(row.get("ask"), f"broker_tick_ask_invalid:{symbol}")
    if ask < bid:
        raise CaptureRefusal(f"broker_tick_crossed_quote:{symbol}")
    broker_quote_epoch = _finite_positive_float(
        row.get("ts_epoch"), f"broker_tick_quote_time_invalid:{symbol}"
    )
    received_at = _finite_positive_float(
        row.get("received_at_epoch"), f"broker_tick_received_time_invalid:{symbol}"
    )
    event_received_at = _finite_positive_float(
        row.get("market_event_received_at_epoch"),
        f"broker_tick_event_time_invalid:{symbol}",
    )
    if (
        broker_quote_epoch > received_at + _CLOCK_TOLERANCE_SECS
        or event_received_at > received_at + 1e-6
        or received_at > sample_epoch + _CLOCK_TOLERANCE_SECS
    ):
        raise CaptureRefusal(f"broker_tick_time_order_invalid:{symbol}")
    # A valid bridge row can be older than this stricter calibration cadence
    # while still satisfying the runtime's own freshness threshold.  Wait for
    # its next event instead of misclassifying the old row as a capture fault.
    if (
        sample_epoch - received_at > MAXIMUM_SAMPLE_GAP_SECS
        or sample_epoch - event_received_at > MAXIMUM_SAMPLE_GAP_SECS
    ):
        return None
    return QuoteSample(
        symbol_index=symbol_index,
        sample_epoch=sample_epoch,
        broker_quote_epoch=broker_quote_epoch,
        received_at_epoch=received_at,
        market_event_received_at_epoch=event_received_at,
        source_event_sequence=sequence,
        source_event_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii"),
        bid=bid,
        ask=ask,
        point=spec.point,
        price_tick_size=spec.price_tick_size,
        digits=spec.digits,
        trade_allowed=spec.trade_allowed,
    )


def _samples_complete(
    samples: Mapping[str, Sequence[QuoteSample]], policy: CapturePolicy
) -> bool:
    for symbol in IG_MT4_SCALP_SYMBOLS:
        rows = samples.get(symbol, ())
        if len(rows) < policy.minimum_samples_per_symbol:
            return False
        if rows[-1].sample_epoch - rows[0].sample_epoch < policy.minimum_duration_secs:
            return False
    return True


def _safe_source_material(source: AuthenticatedMarketSource) -> dict[str, Any]:
    return {
        "market_source_schema": "fxstack_authenticated_broker_market_source_v2",
        "market_source_id_sha256": hashlib.sha256(source.source_id.encode("ascii")).hexdigest(),
        "account_scope_sha256": _sha256_text(
            source.broker_account_scope, "broker_account_scope_missing"
        ),
        "producer_identity_sha256": _sha256_text(
            source.producer_identity, "broker_producer_identity_missing"
        ),
        "terminal_producer_instance_sha256": _sha256_text(
            source.producer_instance_id, "broker_producer_instance_missing"
        ),
        "terminal_lease_scope_sha256": _sha256_text(
            source.terminal_lease_scope, "broker_terminal_lease_scope_missing"
        ),
        "credential_generation_id_sha256": _sha256_text(
            source.credential_generation_id, "broker_credential_generation_missing"
        ),
        "bridge_protocol_version": str(source.bridge_protocol_version),
    }


def _observe_identity(
    audit: AuditChain,
    *,
    source: AuthenticatedMarketSource,
    endpoint: str,
    observed_at: float,
) -> None:
    audit.observe(
        {
            "endpoint": endpoint,
            "source": _safe_source_material(source),
            "observed_at_epoch": observed_at,
        },
        observed_at=observed_at,
    )


def _observe_contract(
    audit: AuditChain,
    *,
    specs: Mapping[str, BrokerSpec],
    source: AuthenticatedMarketSource,
    observed_at: float,
) -> None:
    audit.observe(
        {
            "market_source_id_sha256": hashlib.sha256(
                source.source_id.encode("ascii")
            ).hexdigest(),
            "symbols": {symbol: specs[symbol].as_dict() for symbol in IG_MT4_SCALP_SYMBOLS},
            "observed_at_epoch": observed_at,
        },
        observed_at=observed_at,
    )


def collect_capture(
    *,
    client: BridgeReadClient,
    policy: CapturePolicy,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[
    dict[str, list[QuoteSample]],
    dict[str, BrokerSpec],
    AuthenticatedMarketSource,
    AuditChain,
    AuditChain,
    float,
    float,
]:
    _validate_policy(policy, production=False)
    client.prove_authentication_required()
    capture_start = float(clock())
    state = client.get("/v2/state")
    source = _validated_state_source(state, now_epoch=capture_start)
    identity_audit = AuditChain()
    contract_audit = AuditChain()
    _observe_identity(identity_audit, source=source, endpoint="state", observed_at=capture_start)
    specs_payload = client.get("/v2/market/specs")
    specs = _validated_specs(specs_payload, source=source)
    specs_at = float(clock())
    _observe_identity(identity_audit, source=source, endpoint="specs", observed_at=specs_at)
    _observe_contract(contract_audit, specs=specs, source=source, observed_at=specs_at)

    samples: dict[str, list[QuoteSample]] = {
        symbol: [] for symbol in IG_MT4_SCALP_SYMBOLS
    }
    previous_sequence: dict[str, int] = {}
    start_mono = monotonic()
    next_identity = start_mono + policy.identity_recheck_secs
    next_specs = start_mono + policy.specs_recheck_secs
    next_progress = start_mono

    while True:
        loop_mono = monotonic()
        if loop_mono - start_mono > policy.capture_timeout_secs:
            raise CaptureRefusal("capture_timeout_before_policy_satisfied")
        if loop_mono >= next_identity:
            state_now = float(clock())
            observed = _validated_state_source(
                client.get("/v2/state"), expected=source, now_epoch=state_now
            )
            _observe_identity(
                identity_audit, source=observed, endpoint="state", observed_at=state_now
            )
            next_identity = loop_mono + policy.identity_recheck_secs
        if loop_mono >= next_specs:
            spec_now = float(clock())
            _validated_specs(
                client.get("/v2/market/specs"), source=source, expected=specs
            )
            _observe_identity(
                identity_audit, source=source, endpoint="specs", observed_at=spec_now
            )
            _observe_contract(
                contract_audit, specs=specs, source=source, observed_at=spec_now
            )
            next_specs = loop_mono + policy.specs_recheck_secs

        ticks = client.get("/v2/market/ticks")
        sample_epoch = float(clock())
        _observe_identity(
            identity_audit, source=source, endpoint="ticks", observed_at=sample_epoch
        )
        for symbol_index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
            row = ticks.get(symbol)
            if not isinstance(row, Mapping):
                raise CaptureRefusal(f"broker_tick_missing:{symbol}")
            sample = _quote_sample(
                symbol=symbol,
                symbol_index=symbol_index,
                row=row,
                spec=specs[symbol],
                source=source,
                sample_epoch=sample_epoch,
                previous_sequence=previous_sequence.get(symbol),
            )
            if sample is not None:
                samples[symbol].append(sample)
                previous_sequence[symbol] = sample.source_event_sequence

        if _samples_complete(samples, policy):
            break
        if progress is not None and loop_mono >= next_progress:
            progress(
                {
                    "status": "capturing",
                    "elapsed_secs": round(loop_mono - start_mono, 1),
                    "minimum_observations": min(len(rows) for rows in samples.values()),
                    "maximum_observations": max(len(rows) for rows in samples.values()),
                    "required_observations": policy.minimum_samples_per_symbol,
                }
            )
            next_progress = loop_mono + 10.0
        sleep(policy.poll_interval_secs)

    capture_end = float(clock())
    final_source = _validated_state_source(
        client.get("/v2/state"), expected=source, now_epoch=capture_end
    )
    _observe_identity(
        identity_audit, source=final_source, endpoint="state", observed_at=capture_end
    )
    _validated_specs(client.get("/v2/market/specs"), source=source, expected=specs)
    specs_end = float(clock())
    _observe_identity(identity_audit, source=source, endpoint="specs", observed_at=specs_end)
    _observe_contract(contract_audit, specs=specs, source=source, observed_at=specs_end)
    return (
        samples,
        specs,
        source,
        identity_audit,
        contract_audit,
        capture_start,
        max(capture_end, specs_end),
    )


def _validate_latest_scope_market_events(
    ticks: Mapping[str, Any], *, source: AuthenticatedMarketSource
) -> None:
    """Require current production-fresh evidence before accepting DB history."""

    for symbol in IG_MT4_SCALP_SYMBOLS:
        row = ticks.get(symbol)
        if not isinstance(row, Mapping):
            raise CaptureRefusal(f"broker_tick_missing:{symbol}")
        source_error = market_source_row_error(row, expected=source)
        if source_error:
            raise CaptureRefusal(f"broker_tick_source_invalid:{symbol}:{source_error}")
        if row.get("transport_fresh") is not True:
            raise CaptureRefusal(f"broker_tick_transport_stale:{symbol}")
        if row.get("market_event_identity_present") is not True:
            raise CaptureRefusal(f"broker_tick_event_identity_missing:{symbol}")
        if row.get("market_event_fresh") is not True:
            raise CaptureRefusal(f"broker_tick_event_stale:{symbol}")
        if row.get("source_event_baseline_initialized") is not True:
            raise CaptureRefusal(f"broker_tick_event_baseline_missing:{symbol}")
        if not str(
            row.get("source_event_last_token") or row.get("source_event_token") or ""
        ).strip():
            raise CaptureRefusal(f"broker_tick_event_token_missing:{symbol}")
        bid = _finite_positive_float(
            row.get("bid"), f"broker_tick_bid_invalid:{symbol}"
        )
        ask = _finite_positive_float(
            row.get("ask"), f"broker_tick_ask_invalid:{symbol}"
        )
        if ask < bid:
            raise CaptureRefusal(f"broker_tick_crossed_quote:{symbol}")
        age = _finite_float(
            row.get("market_event_age_secs"),
            f"broker_tick_event_age_invalid:{symbol}",
        )
        stale_after = _finite_positive_float(
            row.get("market_event_stale_after_secs"),
            f"broker_tick_event_threshold_invalid:{symbol}",
        )
        if age < 0.0 or age > stale_after:
            raise CaptureRefusal(f"broker_tick_event_stale:{symbol}")


def _decoded_json_mapping(value: Any, reason: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            raise CaptureRefusal(reason) from None
        if isinstance(decoded, Mapping):
            return dict(decoded)
    raise CaptureRefusal(reason)


def _history_sample_from_row(
    *,
    symbol: str,
    symbol_index: int,
    row: Mapping[str, Any],
    spec: BrokerSpec,
    source: AuthenticatedMarketSource,
) -> QuoteSample:
    source_error = market_source_row_error(row, expected=source)
    if source_error:
        raise CaptureRefusal(f"history_market_source_invalid:{symbol}:{source_error}")
    if str(row.get("symbol") or "").strip().upper() != symbol:
        raise CaptureRefusal(f"history_symbol_invalid:{symbol}")
    sequence = _strict_integer(
        row.get("id"), f"history_event_sequence_invalid:{symbol}", minimum=1
    )
    payload = _decoded_json_mapping(
        row.get("raw_json"), f"history_raw_json_invalid:{symbol}"
    )
    nested = _decoded_json_mapping(
        payload.get("raw"), f"history_nested_raw_invalid:{symbol}"
    )
    token = str(
        nested.get("source_event_token")
        or nested.get("source_event_last_token")
        or payload.get("source_event_token")
        or ""
    ).strip()
    if not token:
        raise CaptureRefusal(f"history_event_token_missing:{symbol}")
    bid = _finite_positive_float(row.get("bid"), f"history_bid_invalid:{symbol}")
    ask = _finite_positive_float(row.get("ask"), f"history_ask_invalid:{symbol}")
    if ask < bid:
        raise CaptureRefusal(f"history_crossed_quote:{symbol}")
    broker_quote_epoch = _finite_positive_float(
        row.get("ts"), f"history_quote_time_invalid:{symbol}"
    )
    received_at = _finite_positive_float(
        nested.get("received_at_epoch"), f"history_received_time_invalid:{symbol}"
    )
    if broker_quote_epoch > received_at + _CLOCK_TOLERANCE_SECS:
        raise CaptureRefusal(f"history_time_order_invalid:{symbol}")
    return QuoteSample(
        symbol_index=symbol_index,
        sample_epoch=received_at,
        broker_quote_epoch=broker_quote_epoch,
        received_at_epoch=received_at,
        market_event_received_at_epoch=received_at,
        source_event_sequence=sequence,
        source_event_token_sha256=hashlib.sha256(token.encode("utf-8")).hexdigest().encode("ascii"),
        bid=bid,
        ask=ask,
        point=spec.point,
        price_tick_size=spec.price_tick_size,
        digits=spec.digits,
        trade_allowed=spec.trade_allowed,
    )


_HISTORY_SELECT = """
SELECT id, symbol, bid, ask, ts, market_source_schema, market_source_id,
       market_source_authenticated, broker_account_scope, broker_venue_id,
       producer_identity, producer_instance_id, terminal_lease_scope,
       credential_generation_id, bridge_protocol_version, raw_json
FROM market_ticks
WHERE symbol = :symbol
  AND market_source_id = :market_source_id
  AND market_source_authenticated = 1
ORDER BY id DESC
LIMIT :row_limit
"""


def _read_same_source_history(
    *,
    database_url: str,
    source: AuthenticatedMarketSource,
    specs: Mapping[str, BrokerSpec],
    policy: CapturePolicy,
    full_span: bool = False,
    max_rows_per_symbol: int = DEFAULT_FULL_HISTORY_MAX_ROWS_PER_SYMBOL,
) -> dict[str, list[QuoteSample]]:
    url = str(database_url or "").strip()
    if not url:
        raise CaptureRefusal("history_database_url_missing")
    maximum_rows = int(max_rows_per_symbol)
    if full_span and maximum_rows < policy.minimum_samples_per_symbol:
        raise CaptureRefusal("history_full_max_rows_too_small")
    engine = None
    try:
        engine = create_engine(url, future=True, pool_pre_ping=True)
        samples: dict[str, list[QuoteSample]] = {}
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                dialect = str(connection.dialect.name or "").lower()
                if dialect == "postgresql":
                    connection.exec_driver_sql(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                elif dialect == "sqlite":
                    connection.exec_driver_sql("PRAGMA query_only = ON")
                    query_only = connection.exec_driver_sql("PRAGMA query_only").scalar()
                    if int(query_only or 0) != 1:
                        raise CaptureRefusal("history_database_read_only_not_proven")
                else:
                    raise CaptureRefusal("history_database_dialect_unsupported")
                row_limit = (
                    maximum_rows + 1
                    if full_span
                    else max(10_000, policy.minimum_samples_per_symbol * 100)
                )
                for symbol_index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS):
                    result = connection.execute(
                        text(_HISTORY_SELECT),
                        {
                            "symbol": symbol,
                            "market_source_id": source.source_id,
                            "row_limit": row_limit,
                        },
                    ).mappings()
                    descending: list[QuoteSample] = []
                    seen: set[tuple[int, bytes]] = set()
                    newest_epoch: float | None = None
                    raw_count = 0
                    for raw_row in result:
                        raw_count += 1
                        if full_span and raw_count > maximum_rows:
                            raise CaptureRefusal(
                                f"history_full_row_limit_exceeded:{symbol}:{maximum_rows}"
                            )
                        sample = _history_sample_from_row(
                            symbol=symbol,
                            symbol_index=symbol_index,
                            row=raw_row,
                            spec=specs[symbol],
                            source=source,
                        )
                        key = (
                            sample.source_event_sequence,
                            sample.source_event_token_sha256,
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        descending.append(sample)
                        if newest_epoch is None:
                            newest_epoch = sample.sample_epoch
                        if (
                            not full_span
                            and
                            len(descending) >= policy.minimum_samples_per_symbol
                            and newest_epoch - sample.sample_epoch
                            >= policy.minimum_duration_secs
                        ):
                            break
                    selected = list(reversed(descending))
                    if len(selected) < policy.minimum_samples_per_symbol:
                        raise CaptureRefusal(
                            f"history_unique_events_insufficient:{symbol}:{len(selected)}"
                        )
                    if (
                        selected[-1].sample_epoch - selected[0].sample_epoch
                        < policy.minimum_duration_secs
                    ):
                        raise CaptureRefusal(f"history_duration_insufficient:{symbol}")
                    samples[symbol] = selected
            finally:
                transaction.rollback()
        return samples
    except CaptureRefusal:
        raise
    except Exception:
        raise CaptureRefusal("history_database_read_failed") from None
    finally:
        if engine is not None:
            engine.dispose()


def collect_history_capture(
    *,
    client: BridgeReadClient,
    database_url: str,
    policy: CapturePolicy,
    full_span: bool = False,
    max_rows_per_symbol: int = DEFAULT_FULL_HISTORY_MAX_ROWS_PER_SYMBOL,
    clock: Callable[[], float] = time.time,
) -> tuple[
    dict[str, list[QuoteSample]],
    dict[str, BrokerSpec],
    AuthenticatedMarketSource,
    AuditChain,
    AuditChain,
    float,
    float,
]:
    _validate_policy(policy, production=False)
    if policy.enforce_maximum_sample_gap:
        raise CaptureRefusal("history_maximum_gap_must_be_telemetry_only")
    if policy.minimum_samples_per_symbol < HISTORY_MINIMUM_SAMPLES_PER_SYMBOL:
        raise CaptureRefusal("history_minimum_samples_too_weak")
    if policy.minimum_duration_secs < MINIMUM_CAPTURE_DURATION_SECS:
        raise CaptureRefusal("history_minimum_duration_too_weak")
    client.prove_authentication_required()
    capture_start = float(clock())
    source = _validated_state_source(
        client.get("/v2/state"), now_epoch=capture_start
    )
    identity_audit = AuditChain()
    contract_audit = AuditChain()
    _observe_identity(
        identity_audit, source=source, endpoint="state", observed_at=capture_start
    )
    specs = _validated_specs(client.get("/v2/market/specs"), source=source)
    specs_at = float(clock())
    _observe_identity(
        identity_audit, source=source, endpoint="specs", observed_at=specs_at
    )
    _observe_contract(
        contract_audit, specs=specs, source=source, observed_at=specs_at
    )
    _validate_latest_scope_market_events(client.get("/v2/market/ticks"), source=source)
    ticks_at = float(clock())
    _observe_identity(
        identity_audit, source=source, endpoint="ticks", observed_at=ticks_at
    )
    samples = _read_same_source_history(
        database_url=database_url,
        source=source,
        specs=specs,
        policy=policy,
        full_span=full_span,
        max_rows_per_symbol=max_rows_per_symbol,
    )
    history_at = float(clock())
    _observe_identity(
        identity_audit,
        source=source,
        endpoint="database_history_read_only",
        observed_at=history_at,
    )
    final_source = _validated_state_source(
        client.get("/v2/state"), expected=source, now_epoch=history_at
    )
    _observe_identity(
        identity_audit, source=final_source, endpoint="state", observed_at=history_at
    )
    _validate_latest_scope_market_events(client.get("/v2/market/ticks"), source=source)
    final_ticks_at = float(clock())
    _observe_identity(
        identity_audit, source=source, endpoint="ticks", observed_at=final_ticks_at
    )
    _validated_specs(client.get("/v2/market/specs"), source=source, expected=specs)
    capture_end = float(clock())
    _observe_identity(
        identity_audit, source=source, endpoint="specs", observed_at=capture_end
    )
    _observe_contract(
        contract_audit, specs=specs, source=source, observed_at=capture_end
    )
    return (
        samples,
        specs,
        source,
        identity_audit,
        contract_audit,
        capture_start,
        capture_end,
    )


def _point_in_time_audit(
    samples: Mapping[str, Sequence[QuoteSample]],
    policy: CapturePolicy,
    *,
    capture_mode: str,
    latest_scope_market_event_fresh: bool,
) -> dict[str, Any]:
    symbols: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not latest_scope_market_event_fresh:
        errors.append("latest_scope_market_event_not_fresh")
    for symbol in IG_MT4_SCALP_SYMBOLS:
        rows = list(samples.get(symbol, ()))
        sample_epochs = [row.sample_epoch for row in rows]
        sequences = [row.source_event_sequence for row in rows]
        event_keys = {
            (row.source_event_sequence, row.source_event_token_sha256) for row in rows
        }
        duration = sample_epochs[-1] - sample_epochs[0] if len(rows) >= 2 else 0.0
        max_gap = max(
            (right - left for left, right in zip(sample_epochs, sample_epochs[1:])),
            default=0.0,
        )
        strictly_increasing_time = all(
            right > left for left, right in zip(sample_epochs, sample_epochs[1:])
        )
        strictly_increasing_sequence = all(
            right > left for left, right in zip(sequences, sequences[1:])
        )
        passed = bool(
            len(rows) >= policy.minimum_samples_per_symbol
            and duration >= policy.minimum_duration_secs
            and (
                not policy.enforce_maximum_sample_gap
                or max_gap <= policy.maximum_sample_gap_secs
            )
            and strictly_increasing_time
            and strictly_increasing_sequence
            and len(event_keys) == len(rows)
        )
        if not passed:
            errors.append(f"capture_policy_failed:{symbol}")
        symbols[symbol] = {
            "observations": len(rows),
            "duration_secs": duration,
            "max_intersample_gap_secs": max_gap,
            "first_source_event_sequence": sequences[0] if sequences else 0,
            "last_source_event_sequence": sequences[-1] if sequences else 0,
            "unique_source_event_count": len(event_keys),
            "passed": passed,
        }
    return {
        "schema_version": POINT_IN_TIME_AUDIT_SCHEMA,
        "passed": not errors,
        "errors": errors,
        "minimum_samples_per_symbol": policy.minimum_samples_per_symbol,
        "minimum_duration_secs": policy.minimum_duration_secs,
        "maximum_sample_gap_secs": policy.maximum_sample_gap_secs,
        "maximum_sample_gap_enforced": policy.enforce_maximum_sample_gap,
        "requires_fresh_authenticated_source_events": True,
        "latest_scope_market_event_fresh": latest_scope_market_event_fresh,
        "database_read_only": capture_mode != "live_endpoint",
        "history_scope_complete": capture_mode == FULL_HISTORY_CAPTURE_MODE,
        "history_selection": (
            "complete_repeatable_read_current_source"
            if capture_mode == FULL_HISTORY_CAPTURE_MODE
            else (
                "minimum_qualification_tail"
                if capture_mode == "authenticated_same_source_db_history"
                else "live_endpoint_observation"
            )
        ),
        "sample_source": capture_mode,
        "symbols": symbols,
    }


def _symbol_summaries(
    samples: Mapping[str, Sequence[QuoteSample]], specs: Mapping[str, BrokerSpec]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        rows = list(samples[symbol])
        spread_bps = np.asarray(
            [((row.ask - row.bid) / ((row.ask + row.bid) / 2.0)) * 10_000.0 for row in rows],
            dtype=np.float64,
        )
        spread_points = np.asarray(
            [(row.ask - row.bid) / row.point for row in rows], dtype=np.float64
        )
        epochs = [row.sample_epoch for row in rows]
        max_gap = max(
            (right - left for left, right in zip(epochs, epochs[1:])), default=0.0
        )
        spec = specs[symbol]
        out[symbol] = {
            "observations": len(rows),
            "duration_secs": epochs[-1] - epochs[0],
            "max_intersample_gap_secs": max_gap,
            "median_observed_spread_bps": float(np.median(spread_bps)),
            "p90_observed_spread_bps": float(np.percentile(spread_bps, 90)),
            "max_observed_spread_bps": float(np.max(spread_bps)),
            "median_observed_spread_points": float(np.median(spread_points)),
            "p90_observed_spread_points": float(np.percentile(spread_points, 90)),
            "max_observed_spread_points": float(np.max(spread_points)),
            **spec.as_dict(),
        }
    return out


def _npz_arrays(samples: Mapping[str, Sequence[QuoteSample]]) -> dict[str, np.ndarray]:
    rows = [row for symbol in IG_MT4_SCALP_SYMBOLS for row in samples[symbol]]
    return {
        "symbol_index": np.asarray([row.symbol_index for row in rows], dtype=np.int64),
        "sample_epoch": np.asarray([row.sample_epoch for row in rows], dtype=np.float64),
        "broker_quote_epoch": np.asarray([row.broker_quote_epoch for row in rows], dtype=np.float64),
        "received_at_epoch": np.asarray([row.received_at_epoch for row in rows], dtype=np.float64),
        "market_event_received_at_epoch": np.asarray(
            [row.market_event_received_at_epoch for row in rows], dtype=np.float64
        ),
        "source_event_sequence": np.asarray(
            [row.source_event_sequence for row in rows], dtype=np.int64
        ),
        "source_event_token_sha256": np.asarray(
            [row.source_event_token_sha256 for row in rows], dtype="S64"
        ),
        "bid": np.asarray([row.bid for row in rows], dtype=np.float64),
        "ask": np.asarray([row.ask for row in rows], dtype=np.float64),
        "point": np.asarray([row.point for row in rows], dtype=np.float64),
        "price_tick_size": np.asarray(
            [row.price_tick_size for row in rows], dtype=np.float64
        ),
        "digits": np.asarray([row.digits for row in rows], dtype=np.int64),
        "trade_allowed": np.asarray(
            [row.trade_allowed for row in rows], dtype=np.bool_
        ),
    }


def build_capture_payload(
    *,
    samples: Mapping[str, Sequence[QuoteSample]],
    specs: Mapping[str, BrokerSpec],
    source: AuthenticatedMarketSource,
    identity_audit: AuditChain,
    contract_audit: AuditChain,
    policy: CapturePolicy,
    capture_start_epoch: float,
    capture_end_epoch: float,
    created_at_epoch: float,
    npz_sha256: str,
    npz_size_bytes: int,
    capture_mode: str = "live_endpoint",
    latest_scope_market_event_fresh: bool = True,
) -> dict[str, Any]:
    if capture_mode not in {
        "live_endpoint",
        "authenticated_same_source_db_history",
        FULL_HISTORY_CAPTURE_MODE,
    }:
        raise CaptureRefusal("capture_mode_invalid")
    safe_source = _safe_source_material(source)
    market_source_audit = {
        "schema_version": MARKET_SOURCE_AUDIT_SCHEMA,
        "authenticated": True,
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": "demo",
        **safe_source,
        "identity_observation_count": identity_audit.count,
        "first_observed_epoch": identity_audit.first_epoch,
        "last_observed_epoch": identity_audit.last_epoch,
        "identity_observation_chain_sha256": identity_audit.digest,
    }
    contract_symbols = {
        symbol: specs[symbol].as_dict() for symbol in IG_MT4_SCALP_SYMBOLS
    }
    broker_contract_audit = {
        "schema_version": BROKER_CONTRACT_AUDIT_SCHEMA,
        "market_source_id_sha256": safe_source["market_source_id_sha256"],
        "observation_count": contract_audit.count,
        "first_observed_epoch": contract_audit.first_epoch,
        "last_observed_epoch": contract_audit.last_epoch,
        "contract_observation_chain_sha256": contract_audit.digest,
        "symbols": contract_symbols,
    }
    point_in_time_audit = _point_in_time_audit(
        samples,
        policy,
        capture_mode=capture_mode,
        latest_scope_market_event_fresh=latest_scope_market_event_fresh,
    )
    if point_in_time_audit["passed"] is not True:
        raise CaptureRefusal("capture_point_in_time_audit_failed")
    execution_contract = {
        "schema_version": EXECUTION_CONTRACT_SCHEMA,
        "max_slippage_points": PRODUCTION_MAX_SLIPPAGE_POINTS,
        "semantics": EXECUTION_TOLERANCE_SEMANTICS,
        "used_as_observed_cost": False,
    }
    payload: dict[str, Any] = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "capture_definition": (
            FULL_HISTORY_CAPTURE_DEFINITION
            if capture_mode == FULL_HISTORY_CAPTURE_MODE
            else CAPTURE_DEFINITION
        ),
        "capture_mode": capture_mode,
        "source_errors": [],
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "source_id": SOURCE_ID,
        "source_version": str(source.bridge_protocol_version),
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "capture_start_epoch": capture_start_epoch,
        "capture_end_epoch": capture_end_epoch,
        "created_at_epoch": created_at_epoch,
        "account_scope_sha256": safe_source["account_scope_sha256"],
        "terminal_producer_instance_sha256": safe_source[
            "terminal_producer_instance_sha256"
        ],
        "market_source_audit": market_source_audit,
        "market_source_audit_sha256": canonical_sha256(market_source_audit),
        "broker_contract_audit": broker_contract_audit,
        "broker_contract_snapshot_sha256": canonical_sha256(broker_contract_audit),
        "point_in_time_audit": point_in_time_audit,
        "point_in_time_audit_sha256": canonical_sha256(point_in_time_audit),
        "execution_contract": execution_contract,
        "npz_path": NPZ_FILENAME,
        "npz_sha256": str(npz_sha256).lower(),
        "npz_size_bytes": int(npz_size_bytes),
        "npz_arrays": dict(NPZ_ARRAY_DTYPES),
        "symbols": _symbol_summaries(samples, specs),
    }
    payload["capture_payload_sha256"] = canonical_sha256(payload)
    return payload


def atomic_emit_capture(
    *,
    output_dir: Path,
    samples: Mapping[str, Sequence[QuoteSample]],
    specs: Mapping[str, BrokerSpec],
    source: AuthenticatedMarketSource,
    identity_audit: AuditChain,
    contract_audit: AuditChain,
    policy: CapturePolicy,
    capture_start_epoch: float,
    capture_end_epoch: float,
    created_at_epoch: float,
    capture_mode: str = "live_endpoint",
    latest_scope_market_event_fresh: bool = True,
) -> Path:
    target = output_dir.expanduser().resolve()
    if target.exists():
        raise CaptureRefusal("capture_output_already_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        npz_path = temporary / NPZ_FILENAME
        arrays = _npz_arrays(samples)
        if set(arrays) != set(NPZ_ARRAY_DTYPES):
            raise CaptureRefusal("capture_npz_array_scope_invalid")
        np.savez_compressed(npz_path, **arrays)
        with npz_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        payload = build_capture_payload(
            samples=samples,
            specs=specs,
            source=source,
            identity_audit=identity_audit,
            contract_audit=contract_audit,
            policy=policy,
            capture_start_epoch=capture_start_epoch,
            capture_end_epoch=capture_end_epoch,
            created_at_epoch=created_at_epoch,
            npz_sha256=_sha256_file(npz_path),
            npz_size_bytes=npz_path.stat().st_size,
            capture_mode=capture_mode,
            latest_scope_market_event_fresh=latest_scope_market_event_fresh,
        )
        json_path = temporary / CAPTURE_FILENAME
        with json_path.open("wb") as handle:
            handle.write(
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8")
            )
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def _load_api_key(api_key_file: str) -> str:
    path_text = str(api_key_file or "").strip()
    if path_text:
        path = Path(path_text).expanduser()
        try:
            if path.stat().st_size <= 0 or path.stat().st_size > MAX_API_KEY_BYTES:
                raise CaptureRefusal("bridge_api_key_file_size_invalid")
            key = path.read_text(encoding="utf-8").strip()
        except CaptureRefusal:
            raise
        except OSError:
            raise CaptureRefusal("bridge_api_key_file_unreadable") from None
    else:
        key = str(os.environ.get("FXSTACK_BRIDGE_API_KEY", "")).strip()
    if not key:
        raise CaptureRefusal("bridge_api_key_missing")
    if len(key.encode("utf-8")) > MAX_API_KEY_BYTES:
        raise CaptureRefusal("bridge_api_key_size_invalid")
    return key


def _load_database_url(database_url_file: str) -> str:
    path_text = str(database_url_file or "").strip()
    if path_text:
        path = Path(path_text).expanduser()
        try:
            if path.stat().st_size <= 0 or path.stat().st_size > MAX_API_KEY_BYTES:
                raise CaptureRefusal("history_database_url_file_size_invalid")
            value = path.read_text(encoding="utf-8").strip()
        except CaptureRefusal:
            raise
        except OSError:
            raise CaptureRefusal("history_database_url_file_unreadable") from None
    else:
        value = str(os.environ.get("FXSTACK_DATABASE_URL", "")).strip()
        if not value:
            try:
                from fxstack.settings import Settings

                value = str(Settings().database_url or "").strip()
            except Exception:
                raise CaptureRefusal("history_database_url_missing") from None
    if not value or len(value.encode("utf-8")) > MAX_API_KEY_BYTES:
        raise CaptureRefusal("history_database_url_invalid")
    return value


def run(args: argparse.Namespace) -> int:
    try:
        capture_mode = str(args.capture_mode)
        history_mode = capture_mode in {"db-history", "db-history-full"}
        full_history_mode = capture_mode == "db-history-full"
        policy = CapturePolicy(
            minimum_samples_per_symbol=(
                HISTORY_MINIMUM_SAMPLES_PER_SYMBOL
                if history_mode
                else MINIMUM_SAMPLES_PER_SYMBOL
            ),
            minimum_duration_secs=MINIMUM_CAPTURE_DURATION_SECS,
            maximum_sample_gap_secs=MAXIMUM_SAMPLE_GAP_SECS,
            capture_timeout_secs=float(args.capture_timeout_secs),
            poll_interval_secs=float(args.poll_interval_secs),
            identity_recheck_secs=float(args.identity_recheck_secs),
            specs_recheck_secs=float(args.specs_recheck_secs),
            enforce_maximum_sample_gap=not history_mode,
        )
        _validate_policy(policy, production=not history_mode)
        client = BridgeReadClient(
            base_url=args.base_url,
            api_key=_load_api_key(args.api_key_file),
            timeout_secs=float(args.http_timeout_secs),
        )
        if history_mode:
            result = collect_history_capture(
                client=client,
                database_url=_load_database_url(args.database_url_file),
                policy=policy,
                full_span=full_history_mode,
                max_rows_per_symbol=int(args.history_max_rows_per_symbol),
            )
            artifact_capture_mode = (
                FULL_HISTORY_CAPTURE_MODE
                if full_history_mode
                else "authenticated_same_source_db_history"
            )
        else:
            result = collect_capture(
                client=client,
                policy=policy,
                progress=lambda item: print(json.dumps(item, sort_keys=True), flush=True),
            )
            artifact_capture_mode = "live_endpoint"
        output = atomic_emit_capture(
            output_dir=Path(args.output_dir),
            samples=result[0],
            specs=result[1],
            source=result[2],
            identity_audit=result[3],
            contract_audit=result[4],
            policy=policy,
            capture_start_epoch=result[5],
            capture_end_epoch=result[6],
            created_at_epoch=time.time(),
            capture_mode=artifact_capture_mode,
            latest_scope_market_event_fresh=True,
        )
        print(str(output))
        return 0
    except CaptureRefusal as exc:
        print(f"capture refused: {exc}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Capture strict authenticated IG-DEMO bid/ask and contract calibration "
            "evidence without touching command or egress routes."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("MT4_BRIDGE_URL", "http://127.0.0.1:58710"),
    )
    parser.add_argument("--api-key-file", default="")
    parser.add_argument(
        "--capture-mode",
        choices=("live", "db-history", "db-history-full"),
        default="live",
    )
    parser.add_argument(
        "--database-url-file",
        default="",
        help=(
            "Optional secret file containing the runtime database URL. Used only "
            "by --capture-mode db-history; otherwise FXSTACK_DATABASE_URL/Settings is used."
        ),
    )
    parser.add_argument(
        "--history-max-rows-per-symbol",
        type=int,
        default=DEFAULT_FULL_HISTORY_MAX_ROWS_PER_SYMBOL,
        help=(
            "Hard refusal cap for --capture-mode db-history-full; the exporter "
            "never silently truncates a current-source history snapshot."
        ),
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument(
        "--capture-timeout-secs", type=float, default=DEFAULT_CAPTURE_TIMEOUT_SECS
    )
    parser.add_argument(
        "--poll-interval-secs", type=float, default=DEFAULT_POLL_INTERVAL_SECS
    )
    parser.add_argument(
        "--identity-recheck-secs", type=float, default=DEFAULT_IDENTITY_RECHECK_SECS
    )
    parser.add_argument(
        "--specs-recheck-secs", type=float, default=DEFAULT_SPECS_RECHECK_SECS
    )
    parser.add_argument(
        "--http-timeout-secs", type=float, default=DEFAULT_HTTP_TIMEOUT_SECS
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_build_parser().parse_args()))
