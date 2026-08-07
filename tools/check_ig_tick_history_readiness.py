"""Report exact-scope authenticated IG tick-history readiness without exporting rows.

This collection-side probe performs authenticated loopback GETs plus aggregate
queries in one read-only repeatable-read transaction.  It emits no quotes,
credentials, account scope, research result, selection, activation, or order
authority.
"""

from __future__ import annotations

# AGENT: ROLE: Read-only exact-22 authenticated tick-history readiness probe.
# AGENT: HANDSHAKE: Current authenticated source identity -> aggregate DB coverage.
# AGENT: ISOLATION: Collection-side metadata only; never enters an evaluator.
# AGENT: SIDE EFFECTS: Optional create-once JSON report; no database writes.

import argparse
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from sqlalchemy import create_engine, text


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tools import capture_ig_scalp_cost_model as capture  # noqa: E402


READINESS_SCHEMA_VERSION = "fxstack.ig_tick_history_readiness.v1"
REQUIRED_HISTORY_DAYS = 30
REQUIRED_DURATION_SECS = float(REQUIRED_HISTORY_DAYS * 24 * 60 * 60)
REQUIRED_OBSERVATIONS_PER_SYMBOL = capture.HISTORY_MINIMUM_SAMPLES_PER_SYMBOL


_AGGREGATE_QUERY = """
SELECT COUNT(*) AS observations,
       MIN(id) AS first_sequence,
       MAX(id) AS last_sequence,
       MIN(ts) AS first_quote_epoch,
       MAX(ts) AS last_quote_epoch
FROM market_ticks
WHERE symbol = :symbol
  AND market_source_schema = :market_source_schema
  AND market_source_id = :market_source_id
  AND market_source_authenticated = 1
  AND broker_account_scope = :broker_account_scope
  AND broker_venue_id = :broker_venue_id
  AND producer_identity = :producer_identity
  AND producer_instance_id = :producer_instance_id
  AND terminal_lease_scope = :terminal_lease_scope
  AND credential_generation_id = :credential_generation_id
  AND bridge_protocol_version = :bridge_protocol_version
"""


def _finite_epoch(value: Any, reason: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise capture.CaptureRefusal(reason) from None
    if not math.isfinite(result) or result <= 0.0:
        raise capture.CaptureRefusal(reason)
    return result


def _read_history_aggregates(
    *, database_url: str, source: capture.AuthenticatedMarketSource
) -> dict[str, dict[str, Any]]:
    url = str(database_url or "").strip()
    if not url:
        raise capture.CaptureRefusal("readiness_database_url_missing")
    engine = None
    try:
        engine = create_engine(url, future=True, pool_pre_ping=True)
        output: dict[str, dict[str, Any]] = {}
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
                        raise capture.CaptureRefusal(
                            "readiness_database_read_only_not_proven"
                        )
                else:
                    raise capture.CaptureRefusal(
                        "readiness_database_dialect_unsupported"
                    )
                identity = source.to_fields()
                for symbol in capture.IG_MT4_SCALP_SYMBOLS:
                    row = connection.execute(
                        text(_AGGREGATE_QUERY),
                        {"symbol": symbol, **identity},
                    ).mappings().one()
                    observations = int(row.get("observations") or 0)
                    if observations <= 0:
                        output[symbol] = {
                            "observations": 0,
                            "first_sequence": 0,
                            "last_sequence": 0,
                            "first_quote_epoch": None,
                            "last_quote_epoch": None,
                        }
                        continue
                    first_epoch = _finite_epoch(
                        row.get("first_quote_epoch"),
                        f"readiness_first_quote_epoch_invalid:{symbol}",
                    )
                    last_epoch = _finite_epoch(
                        row.get("last_quote_epoch"),
                        f"readiness_last_quote_epoch_invalid:{symbol}",
                    )
                    first_sequence = int(row.get("first_sequence") or 0)
                    last_sequence = int(row.get("last_sequence") or 0)
                    if (
                        last_epoch < first_epoch
                        or first_sequence <= 0
                        or last_sequence < first_sequence
                    ):
                        raise capture.CaptureRefusal(
                            f"readiness_history_order_invalid:{symbol}"
                        )
                    output[symbol] = {
                        "observations": observations,
                        "first_sequence": first_sequence,
                        "last_sequence": last_sequence,
                        "first_quote_epoch": first_epoch,
                        "last_quote_epoch": last_epoch,
                    }
            finally:
                transaction.rollback()
        return output
    except capture.CaptureRefusal:
        raise
    except Exception:
        raise capture.CaptureRefusal("readiness_database_read_failed") from None
    finally:
        if engine is not None:
            engine.dispose()


def _build_readiness_payload(
    *,
    aggregates: Mapping[str, Mapping[str, Any]],
    source: capture.AuthenticatedMarketSource,
    observed_at_epoch: float,
) -> dict[str, Any]:
    observed_at = _finite_epoch(observed_at_epoch, "readiness_observed_at_invalid")
    symbols: dict[str, dict[str, Any]] = {}
    for symbol in capture.IG_MT4_SCALP_SYMBOLS:
        row = dict(aggregates.get(symbol) or {})
        observations = int(row.get("observations") or 0)
        first_epoch = row.get("first_quote_epoch")
        last_epoch = row.get("last_quote_epoch")
        duration = (
            max(0.0, float(last_epoch) - float(first_epoch))
            if observations > 0 and first_epoch is not None and last_epoch is not None
            else 0.0
        )
        remaining = max(0.0, REQUIRED_DURATION_SECS - duration)
        ready = bool(
            observations >= REQUIRED_OBSERVATIONS_PER_SYMBOL
            and duration >= REQUIRED_DURATION_SECS
        )
        symbols[symbol] = {
            "observations": observations,
            "first_sequence": int(row.get("first_sequence") or 0),
            "last_sequence": int(row.get("last_sequence") or 0),
            "first_quote_epoch": first_epoch,
            "last_quote_epoch": last_epoch,
            "duration_secs": duration,
            "duration_hours": duration / 3600.0,
            "remaining_duration_secs": remaining,
            "ready": ready,
        }
    minimum_duration = min(row["duration_secs"] for row in symbols.values())
    maximum_duration = max(row["duration_secs"] for row in symbols.values())
    maximum_remaining = max(row["remaining_duration_secs"] for row in symbols.values())
    ready_symbols = [symbol for symbol, row in symbols.items() if row["ready"]]
    payload: dict[str, Any] = {
        "schema_version": READINESS_SCHEMA_VERSION,
        "collection_metadata_only": True,
        "database_read_only": True,
        "database_isolation": "repeatable_read_read_only",
        "source_identity_authenticated": True,
        "current_market_source_id": source.source_id,
        "venue_id": capture.IG_MT4_VENUE_ID,
        "scope_version": capture.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(capture.IG_MT4_SCALP_SYMBOLS),
        "required_history_days": REQUIRED_HISTORY_DAYS,
        "required_duration_secs_per_symbol": REQUIRED_DURATION_SECS,
        "required_observations_per_symbol": REQUIRED_OBSERVATIONS_PER_SYMBOL,
        "observed_at_epoch": observed_at,
        "minimum_symbol_duration_secs": minimum_duration,
        "minimum_symbol_duration_hours": minimum_duration / 3600.0,
        "maximum_symbol_duration_secs": maximum_duration,
        "maximum_symbol_duration_hours": maximum_duration / 3600.0,
        "ready_symbol_count": len(ready_symbols),
        "ready_symbols": ready_symbols,
        "exact_scope_ready": len(ready_symbols) == len(capture.IG_MT4_SCALP_SYMBOLS),
        "projected_earliest_exact_scope_ready_epoch": observed_at + maximum_remaining,
        "projection_assumption": "same_authenticated_source_continues_without_reset",
        "symbols": symbols,
        "research_authorized": False,
        "selection_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
        "withheld_reasons": [],
    }
    if not payload["exact_scope_ready"]:
        payload["withheld_reasons"].append("minimum_30_day_exact_scope_history_missing")
    payload["payload_sha256"] = capture.canonical_sha256(payload)
    return payload


def run(args: argparse.Namespace) -> int:
    try:
        client = capture.BridgeReadClient(
            base_url=args.base_url,
            api_key=capture._load_api_key(args.api_key_file),
            timeout_secs=float(args.http_timeout_secs),
        )
        client.prove_authentication_required()
        started_at = time.time()
        source = capture._validated_state_source(
            client.get("/v2/state"), now_epoch=started_at
        )
        capture._validated_specs(client.get("/v2/market/specs"), source=source)
        capture._validate_latest_scope_market_events(
            client.get("/v2/market/ticks"), source=source
        )
        aggregates = _read_history_aggregates(
            database_url=capture._load_database_url(args.database_url_file),
            source=source,
        )
        checked_at = time.time()
        capture._validated_state_source(
            client.get("/v2/state"), expected=source, now_epoch=checked_at
        )
        capture._validate_latest_scope_market_events(
            client.get("/v2/market/ticks"), source=source
        )
        payload = _build_readiness_payload(
            aggregates=aggregates,
            source=source,
            observed_at_epoch=time.time(),
        )
        output_text = str(args.output or "").strip()
        if output_text:
            output = Path(output_text).expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    payload,
                    handle,
                    ensure_ascii=True,
                    allow_nan=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
            print(str(output))
        else:
            print(
                json.dumps(
                    payload,
                    ensure_ascii=True,
                    allow_nan=False,
                    sort_keys=True,
                )
            )
        return 0
    except (capture.CaptureRefusal, FileExistsError) as exc:
        reason = "readiness_output_already_exists" if isinstance(exc, FileExistsError) else str(exc)
        print(f"readiness refused: {reason}", file=sys.stderr)
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:58710",
    )
    parser.add_argument("--api-key-file", default="")
    parser.add_argument("--database-url-file", default="")
    parser.add_argument("--http-timeout-secs", type=float, default=3.0)
    parser.add_argument("--output", default="")
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
