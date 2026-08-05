"""Export deduplicated legacy quote rows as untrusted offline training bytes.

The legacy rows predate authenticated market-source stamping.  This exporter
therefore grants them no evidence, release, runtime, or activation authority.
It uses a repeatable-read, read-only transaction, stops before the first
authenticated row, removes consecutive duplicate quotes, and emits only a
hash-bound JSON/NPZ pair for exploratory model fitting.
"""

from __future__ import annotations

# AGENT: ROLE: Pre-isolation sanitizer for legacy untrusted tick training rows.
# AGENT: ISOLATION: Database access ends here; output contains quote/time bytes only.
# AGENT: SIDE EFFECTS: One new atomic artifact directory; no overwrite or DB write.

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Iterable

import numpy as np
from sqlalchemy import create_engine, text

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
)


SCHEMA_VERSION = "fxstack.legacy_tick_training_snapshot.v1"
DEFINITION = "legacy_untrusted_deduplicated_tick_training.v1"
JSON_FILENAME = "legacy_tick_training_snapshot.json"
NPZ_FILENAME = "legacy_tick_training_snapshot.npz"
DEFAULT_LOOKBACK_DAYS = 30.0
DEFAULT_MAX_ROWS_PER_SYMBOL = 250_000
DEFAULT_MAX_TOTAL_ROWS = 2_000_000


class ExportRefusal(RuntimeError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ExportRefusal("legacy_payload_not_canonical") from exc


def _sha256_value(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_quote(value: Any, reason: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ExportRefusal(reason) from None
    if not math.isfinite(number) or number <= 0.0:
        raise ExportRefusal(reason)
    return number


def _read_rows(
    *,
    database_url: str,
    symbols: tuple[str, ...],
    lookback_days: float,
    max_rows_per_symbol: int,
    max_total_rows: int,
) -> tuple[dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]], float, float]:
    if (
        not database_url
        or not symbols
        or not math.isfinite(lookback_days)
        or lookback_days <= 0.0
        or max_rows_per_symbol < 100
        or max_total_rows < max_rows_per_symbol
    ):
        raise ExportRefusal("legacy_export_policy_invalid")
    engine = create_engine(database_url, future=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            dialect = str(connection.dialect.name or "").lower()
            if dialect == "postgresql":
                connection.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                )
            elif dialect == "sqlite":
                connection.exec_driver_sql("PRAGMA query_only = ON")
                if int(connection.exec_driver_sql("PRAGMA query_only").scalar() or 0) != 1:
                    raise ExportRefusal("legacy_database_read_only_not_proven")
            else:
                raise ExportRefusal("legacy_database_dialect_unsupported")
            auth_start = connection.execute(
                text(
                    "SELECT min(ts) FROM market_ticks "
                    "WHERE market_source_authenticated = 1"
                )
            ).scalar()
            try:
                end_epoch = float(auth_start)
            except (TypeError, ValueError, OverflowError):
                raise ExportRefusal("legacy_authenticated_boundary_missing") from None
            if not math.isfinite(end_epoch) or end_epoch <= 0.0:
                raise ExportRefusal("legacy_authenticated_boundary_missing")
            start_epoch = end_epoch - float(lookback_days) * 86_400.0
            query = text(
                "SELECT id, bid, ask, ts FROM market_ticks "
                "WHERE market_source_authenticated = 0 AND symbol = :symbol "
                "AND ts >= :start_epoch AND ts < :end_epoch "
                "ORDER BY ts ASC, id ASC"
            )
            output: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
            total = 0
            for symbol in symbols:
                epochs: list[float] = []
                bids: list[float] = []
                asks: list[float] = []
                previous_quote: tuple[float, float] | None = None
                result = connection.execution_options(stream_results=True).execute(
                    query,
                    {
                        "symbol": symbol,
                        "start_epoch": start_epoch,
                        "end_epoch": end_epoch,
                    },
                ).mappings()
                for raw in result:
                    bid = _finite_quote(raw.get("bid"), f"legacy_bid_invalid:{symbol}")
                    ask = _finite_quote(raw.get("ask"), f"legacy_ask_invalid:{symbol}")
                    epoch = _finite_quote(raw.get("ts"), f"legacy_epoch_invalid:{symbol}")
                    if ask < bid:
                        raise ExportRefusal(f"legacy_crossed_quote:{symbol}")
                    if epochs and epoch < epochs[-1]:
                        raise ExportRefusal(f"legacy_epoch_regressed:{symbol}")
                    quote = (bid, ask)
                    if quote == previous_quote:
                        continue
                    if epochs and epoch == epochs[-1]:
                        bids[-1] = bid
                        asks[-1] = ask
                    else:
                        epochs.append(epoch)
                        bids.append(bid)
                        asks.append(ask)
                    previous_quote = quote
                    if len(epochs) > max_rows_per_symbol:
                        raise ExportRefusal(
                            f"legacy_symbol_row_limit_exceeded:{symbol}:{max_rows_per_symbol}"
                        )
                if epochs and len(epochs) < 100:
                    raise ExportRefusal(f"legacy_symbol_rows_insufficient:{symbol}")
                total += len(epochs)
                if total > max_total_rows:
                    raise ExportRefusal(
                        f"legacy_total_row_limit_exceeded:{max_total_rows}"
                    )
                output[symbol] = (
                    np.asarray(epochs, dtype=np.float64),
                    np.asarray(bids, dtype=np.float64),
                    np.asarray(asks, dtype=np.float64),
                )
            transaction.rollback()
            return output, start_epoch, end_epoch
    except ExportRefusal:
        raise
    except Exception:
        raise ExportRefusal("legacy_database_read_failed") from None
    finally:
        engine.dispose()


def _emit(
    *,
    output_dir: Path,
    symbols: tuple[str, ...],
    rows: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    requested_start_epoch: float,
    authenticated_boundary_epoch: float,
    created_at_epoch: float,
) -> Path:
    target = output_dir.expanduser().resolve()
    if target.exists():
        raise ExportRefusal("legacy_output_already_exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.tmp-", dir=target.parent))
    try:
        symbol_index: list[np.ndarray] = []
        epochs: list[np.ndarray] = []
        bids: list[np.ndarray] = []
        asks: list[np.ndarray] = []
        summaries: dict[str, dict[str, Any]] = {}
        for index, symbol in enumerate(symbols):
            symbol_epochs, symbol_bids, symbol_asks = rows[symbol]
            count = int(symbol_epochs.shape[0])
            symbol_index.append(np.full(count, index, dtype=np.int16))
            epochs.append(symbol_epochs)
            bids.append(symbol_bids)
            asks.append(symbol_asks)
            summaries[symbol] = {
                "observations": count,
                "first_epoch": float(symbol_epochs[0]) if count else None,
                "last_epoch": float(symbol_epochs[-1]) if count else None,
                "duration_secs": (
                    float(symbol_epochs[-1] - symbol_epochs[0]) if count >= 2 else 0.0
                ),
            }
        arrays = {
            "symbol_index": np.concatenate(symbol_index),
            "sample_epoch": np.concatenate(epochs),
            "bid": np.concatenate(bids),
            "ask": np.concatenate(asks),
        }
        npz_path = temporary / NPZ_FILENAME
        np.savez_compressed(npz_path, **arrays)
        with npz_path.open("rb+") as handle:
            os.fsync(handle.fileno())
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "definition": DEFINITION,
            "research_training_only": True,
            "legacy_untrusted": True,
            "market_source_authenticated": False,
            "database_read_only": True,
            "repeatable_read": True,
            "activation_authorized": False,
            "success_claim_authorized": False,
            "deduplication": "consecutive_bid_ask_changes",
            "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
            "symbol_scope": list(symbols),
            "requested_start_epoch": requested_start_epoch,
            "authenticated_boundary_epoch": authenticated_boundary_epoch,
            "created_at_epoch": created_at_epoch,
            "npz_path": NPZ_FILENAME,
            "npz_sha256": _sha256_file(npz_path),
            "npz_size_bytes": npz_path.stat().st_size,
            "npz_arrays": {
                "symbol_index": "int16",
                "sample_epoch": "float64",
                "bid": "float64",
                "ask": "float64",
            },
            "symbols": summaries,
            "total_observations": int(arrays["sample_epoch"].shape[0]),
        }
        payload["payload_sha256"] = _sha256_value(payload)
        json_path = temporary / JSON_FILENAME
        with json_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def run(args: argparse.Namespace) -> int:
    database_url = str(os.environ.get("FXSTACK_DATABASE_URL") or "").strip()
    if not database_url:
        raise ExportRefusal("legacy_database_url_missing")
    rows, start_epoch, end_epoch = _read_rows(
        database_url=database_url,
        symbols=IG_MT4_SCALP_SYMBOLS,
        lookback_days=float(args.lookback_days),
        max_rows_per_symbol=int(args.max_rows_per_symbol),
        max_total_rows=int(args.max_total_rows),
    )
    output = _emit(
        output_dir=Path(args.output_dir),
        symbols=IG_MT4_SCALP_SYMBOLS,
        rows=rows,
        requested_start_epoch=start_epoch,
        authenticated_boundary_epoch=end_epoch,
        created_at_epoch=time.time(),
    )
    print(str(output))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--lookback-days", type=float, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument(
        "--max-rows-per-symbol", type=int, default=DEFAULT_MAX_ROWS_PER_SYMBOL
    )
    parser.add_argument("--max-total-rows", type=int, default=DEFAULT_MAX_TOTAL_ROWS)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
