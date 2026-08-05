"""Chronological BUY/SELL/abstain screen for a sealed IG tick snapshot.

This evaluator is research-only.  It accepts only the sanitized hash-bound
JSON/NPZ pair emitted by ``capture_ig_scalp_cost_model.py db-history-full``.
It has no live API, database, credential, registry, activation, or broker
surface.
"""

from __future__ import annotations

# AGENT: ROLE: Offline advisory tick-microstructure BUY/SELL/abstain screen.
# AGENT: ISOLATION: Reads one sealed portable bundle and writes one advisory report.
# AGENT: HANDSHAKE: Full IG tick snapshot -> chronological delayed executable outcomes.
# AGENT: SIDE EFFECTS: Creates a new report file only; never overwrites.

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor


CAPTURE_FILENAME = "ig_mt4_bid_ask_capture.json"
FULL_HISTORY_CAPTURE_MODE = "authenticated_same_source_db_history_full"
FULL_HISTORY_CAPTURE_DEFINITION = (
    "authenticated_ig_demo_tick_microstructure_snapshot.v1"
)
REPORT_SCHEMA_VERSION = "fxstack.ig_tick_microstructure_screen.v1"
LEGACY_JSON_FILENAME = "legacy_tick_training_snapshot.json"
LEGACY_SCHEMA_VERSION = "fxstack.legacy_tick_training_snapshot.v1"
LEGACY_DEFINITION = "legacy_untrusted_deduplicated_tick_training.v1"
BASE_FEATURE_NAMES: tuple[str, ...] = (
    "symbol_index_scaled",
    "spread_bps",
    "transport_delay_secs",
    "event_return_1_bps",
    "event_return_3_bps",
    "event_return_8_bps",
    "event_return_20_bps",
    "time_return_1s_bps",
    "time_return_5s_bps",
    "time_return_15s_bps",
    "sign_balance_5",
    "sign_balance_20",
    "event_intensity_5",
    "event_intensity_20",
    "realized_vol_20_bps",
    "utc_sin",
    "utc_cos",
)
FEATURE_NAMES = BASE_FEATURE_NAMES
GRAPH_FEATURE_NAMES: tuple[str, ...] = (
    "graph_centered_residual_bps",
    "graph_residual_z",
    "graph_residual_delta_bps",
    "graph_fresh",
)
GRAPH_FORMULAS: dict[str, tuple[str, str, str]] = {
    "EURUSD": ("EURJPY", "USDJPY", "divide"),
    "USDJPY": ("EURJPY", "EURUSD", "divide"),
    "AUDUSD": ("AUDJPY", "USDJPY", "divide"),
    "GBPUSD": ("GBPJPY", "USDJPY", "divide"),
    "USDCAD": ("EURCAD", "EURUSD", "divide"),
    "USDCHF": ("EURCHF", "EURUSD", "divide"),
    "EURGBP": ("EURUSD", "GBPUSD", "divide"),
    "EURJPY": ("EURUSD", "USDJPY", "multiply"),
    "NZDUSD": ("NZDJPY", "USDJPY", "divide"),
    "AUDJPY": ("AUDUSD", "USDJPY", "multiply"),
    "CADJPY": ("USDJPY", "USDCAD", "divide"),
    "CHFJPY": ("USDJPY", "USDCHF", "divide"),
    "EURAUD": ("EURUSD", "AUDUSD", "divide"),
    "EURCAD": ("EURUSD", "USDCAD", "multiply"),
    "EURCHF": ("EURUSD", "USDCHF", "multiply"),
    "GBPCAD": ("GBPUSD", "USDCAD", "multiply"),
    "GBPCHF": ("GBPUSD", "USDCHF", "multiply"),
    "GBPJPY": ("GBPUSD", "USDJPY", "multiply"),
    "AUDCAD": ("AUDUSD", "USDCAD", "multiply"),
    "NZDJPY": ("NZDUSD", "USDJPY", "multiply"),
}


def _feature_names(symbols: list[str]) -> tuple[str, ...]:
    cross = tuple(
        name
        for symbol in symbols
        for name in (
            f"peer_{symbol}_return_1s_bps",
            f"peer_{symbol}_return_5s_bps",
            f"peer_{symbol}_fresh",
        )
    )
    return BASE_FEATURE_NAMES + cross + GRAPH_FEATURE_NAMES


class ScreenRefusal(RuntimeError):
    """Stable fail-closed input or causal-contract refusal."""


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
        raise ScreenRefusal("payload_not_canonical") from exc


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _strict_capture(root: Path) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    bundle = root.expanduser().resolve()
    payload_path = bundle / CAPTURE_FILENAME
    if not bundle.is_dir() or not payload_path.is_file():
        raise ScreenRefusal("capture_bundle_missing")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ScreenRefusal("capture_payload_invalid") from None
    if not isinstance(payload, dict):
        raise ScreenRefusal("capture_payload_invalid")
    expected_payload_hash = str(payload.get("capture_payload_sha256") or "").lower()
    body = dict(payload)
    body.pop("capture_payload_sha256", None)
    if len(expected_payload_hash) != 64 or _canonical_sha256(body) != expected_payload_hash:
        raise ScreenRefusal("capture_payload_hash_mismatch")
    audit = payload.get("point_in_time_audit")
    if (
        payload.get("capture_mode") != FULL_HISTORY_CAPTURE_MODE
        or payload.get("capture_definition") != FULL_HISTORY_CAPTURE_DEFINITION
        or not isinstance(audit, dict)
        or audit.get("passed") is not True
        or audit.get("database_read_only") is not True
        or audit.get("history_scope_complete") is not True
        or audit.get("history_selection")
        != "complete_repeatable_read_current_source"
        or payload.get("source_errors") != []
    ):
        raise ScreenRefusal("capture_full_history_contract_invalid")
    npz_name = str(payload.get("npz_path") or "")
    if not npz_name or Path(npz_name).name != npz_name:
        raise ScreenRefusal("capture_npz_path_invalid")
    npz_path = (bundle / npz_name).resolve()
    if npz_path.parent != bundle or not npz_path.is_file():
        raise ScreenRefusal("capture_npz_missing")
    expected_npz_hash = str(payload.get("npz_sha256") or "").lower()
    if len(expected_npz_hash) != 64 or _file_sha256(npz_path) != expected_npz_hash:
        raise ScreenRefusal("capture_npz_hash_mismatch")
    try:
        with np.load(npz_path, allow_pickle=False) as loaded:
            arrays = {name: np.asarray(loaded[name]) for name in loaded.files}
    except (OSError, ValueError, KeyError):
        raise ScreenRefusal("capture_npz_invalid") from None
    required = {
        "symbol_index",
        "sample_epoch",
        "broker_quote_epoch",
        "received_at_epoch",
        "market_event_received_at_epoch",
        "source_event_sequence",
        "source_event_token_sha256",
        "bid",
        "ask",
        "point",
        "price_tick_size",
        "digits",
        "trade_allowed",
    }
    if set(arrays) != required:
        raise ScreenRefusal("capture_npz_array_scope_invalid")
    lengths = {int(value.shape[0]) for value in arrays.values() if value.ndim == 1}
    if any(value.ndim != 1 for value in arrays.values()) or len(lengths) != 1:
        raise ScreenRefusal("capture_npz_shape_invalid")
    return payload, arrays


def _strict_legacy_training(
    root: Path,
    *,
    authenticated_payload: Mapping[str, Any],
    authenticated_arrays: Mapping[str, np.ndarray],
) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    bundle = root.expanduser().resolve()
    payload_path = bundle / LEGACY_JSON_FILENAME
    if not bundle.is_dir() or not payload_path.is_file():
        raise ScreenRefusal("legacy_training_bundle_missing")
    try:
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ScreenRefusal("legacy_training_payload_invalid") from None
    if not isinstance(payload, dict):
        raise ScreenRefusal("legacy_training_payload_invalid")
    expected_hash = str(payload.get("payload_sha256") or "").lower()
    body = dict(payload)
    body.pop("payload_sha256", None)
    if len(expected_hash) != 64 or _canonical_sha256(body) != expected_hash:
        raise ScreenRefusal("legacy_training_payload_hash_mismatch")
    if (
        payload.get("schema_version") != LEGACY_SCHEMA_VERSION
        or payload.get("definition") != LEGACY_DEFINITION
        or payload.get("legacy_untrusted") is not True
        or payload.get("research_training_only") is not True
        or payload.get("market_source_authenticated") is not False
        or payload.get("database_read_only") is not True
        or payload.get("repeatable_read") is not True
        or payload.get("activation_authorized") is not False
        or payload.get("success_claim_authorized") is not False
        or payload.get("deduplication") != "consecutive_bid_ask_changes"
        or list(payload.get("symbol_scope") or [])
        != list(authenticated_payload.get("symbol_scope") or [])
    ):
        raise ScreenRefusal("legacy_training_contract_invalid")
    authenticated_first = float(np.min(authenticated_arrays["sample_epoch"]))
    boundary = float(payload.get("authenticated_boundary_epoch") or 0.0)
    if not math.isfinite(boundary) or boundary > authenticated_first:
        raise ScreenRefusal("legacy_training_boundary_overlaps_authenticated")
    npz_name = str(payload.get("npz_path") or "")
    if not npz_name or Path(npz_name).name != npz_name:
        raise ScreenRefusal("legacy_training_npz_path_invalid")
    npz_path = (bundle / npz_name).resolve()
    if npz_path.parent != bundle or not npz_path.is_file():
        raise ScreenRefusal("legacy_training_npz_missing")
    if _file_sha256(npz_path) != str(payload.get("npz_sha256") or "").lower():
        raise ScreenRefusal("legacy_training_npz_hash_mismatch")
    try:
        with np.load(npz_path, allow_pickle=False) as loaded:
            arrays = {name: np.asarray(loaded[name]) for name in loaded.files}
    except (OSError, ValueError, KeyError):
        raise ScreenRefusal("legacy_training_npz_invalid") from None
    if set(arrays) != {"symbol_index", "sample_epoch", "bid", "ask"}:
        raise ScreenRefusal("legacy_training_npz_scope_invalid")
    lengths = {int(value.shape[0]) for value in arrays.values() if value.ndim == 1}
    if any(value.ndim != 1 for value in arrays.values()) or len(lengths) != 1:
        raise ScreenRefusal("legacy_training_npz_shape_invalid")
    return payload, arrays


def _window_sum(values: np.ndarray, width: int) -> np.ndarray:
    cumulative = np.concatenate(([0.0], np.cumsum(values, dtype=np.float64)))
    out = np.zeros(values.shape[0], dtype=np.float64)
    out[width - 1 :] = cumulative[width:] - cumulative[:-width]
    return out


def _event_return(mid: np.ndarray, width: int) -> np.ndarray:
    out = np.zeros(mid.shape[0], dtype=np.float64)
    out[width:] = (mid[width:] / mid[:-width] - 1.0) * 10_000.0
    return out


def _time_return(mid: np.ndarray, epochs: np.ndarray, seconds: float) -> np.ndarray:
    prior = np.searchsorted(epochs, epochs - float(seconds), side="right") - 1
    valid = prior >= 0
    out = np.zeros(mid.shape[0], dtype=np.float64)
    out[valid] = (mid[valid] / mid[prior[valid]] - 1.0) * 10_000.0
    return out


def _rolling_vol(values: np.ndarray, width: int) -> np.ndarray:
    sums = _window_sum(values, width)
    squares = _window_sum(values * values, width)
    variance = np.maximum(0.0, squares / width - (sums / width) ** 2)
    return np.sqrt(variance)


def _build_symbol_rows(
    *,
    symbol_index: int,
    symbol_count: int,
    epochs: np.ndarray,
    broker_epochs: np.ndarray,
    bid: np.ndarray,
    ask: np.ndarray,
    horizon_secs: float,
    maximum_event_gap_secs: float,
) -> dict[str, np.ndarray]:
    n = epochs.shape[0]
    if n < 100 or not np.all(np.diff(epochs) > 0.0):
        raise ScreenRefusal(f"symbol_epoch_order_invalid:{symbol_index}")
    if (
        not np.all(np.isfinite(bid))
        or not np.all(np.isfinite(ask))
        or not np.all(np.isfinite(epochs))
        or not np.all(np.isfinite(broker_epochs))
        or np.any(bid <= 0.0)
        or np.any(ask < bid)
    ):
        raise ScreenRefusal(f"symbol_quote_invalid:{symbol_index}")
    mid = (bid + ask) / 2.0
    spread_bps = (ask - bid) / mid * 10_000.0
    one = _event_return(mid, 1)
    signs = np.sign(one)
    dt5 = np.zeros(n, dtype=np.float64)
    dt20 = np.zeros(n, dtype=np.float64)
    dt5[5:] = epochs[5:] - epochs[:-5]
    dt20[20:] = epochs[20:] - epochs[:-20]
    phase = np.remainder(epochs, 86_400.0) / 86_400.0 * (2.0 * math.pi)
    features = np.column_stack(
        (
            np.full(n, symbol_index / max(1, symbol_count - 1)),
            spread_bps,
            np.maximum(0.0, epochs - broker_epochs),
            one,
            _event_return(mid, 3),
            _event_return(mid, 8),
            _event_return(mid, 20),
            _time_return(mid, epochs, 1.0),
            _time_return(mid, epochs, 5.0),
            _time_return(mid, epochs, 15.0),
            _window_sum(signs, 5) / 5.0,
            _window_sum(signs, 20) / 20.0,
            np.divide(5.0, dt5, out=np.zeros(n), where=dt5 > 0.0),
            np.divide(20.0, dt20, out=np.zeros(n), where=dt20 > 0.0),
            _rolling_vol(one, 20),
            np.sin(phase),
            np.cos(phase),
        )
    )
    signal_index = np.arange(n, dtype=np.int64)
    entry_index = signal_index + 1
    valid_entry = entry_index < n
    target_epoch = np.full(n, np.inf, dtype=np.float64)
    target_epoch[valid_entry] = epochs[entry_index[valid_entry]] + horizon_secs
    exit_index = np.searchsorted(epochs, target_epoch, side="left")
    valid_exit = exit_index < n
    clipped_entry = np.minimum(entry_index, n - 1)
    clipped_exit = np.minimum(exit_index, n - 1)
    entry_delay = epochs[clipped_entry] - epochs
    exit_delay = epochs[clipped_exit] - target_epoch
    warm = 20
    valid = (
        (signal_index >= warm)
        & valid_entry
        & valid_exit
        & (entry_delay > 0.0)
        & (entry_delay <= maximum_event_gap_secs)
        & (exit_delay >= 0.0)
        & (exit_delay <= maximum_event_gap_secs)
        & np.all(np.isfinite(features), axis=1)
    )
    idx = signal_index[valid]
    ent = entry_index[valid]
    ext = exit_index[valid]
    entry_mid = mid[ent]
    mid_bps = (mid[ext] - entry_mid) / entry_mid * 10_000.0
    long_bps = (bid[ext] - ask[ent]) / entry_mid * 10_000.0
    short_bps = (bid[ent] - ask[ext]) / entry_mid * 10_000.0
    return {
        "X": features[idx].astype(np.float32, copy=False),
        "mid_bps": mid_bps.astype(np.float64, copy=False),
        "long_bps": long_bps.astype(np.float64, copy=False),
        "short_bps": short_bps.astype(np.float64, copy=False),
        "signal_epoch": epochs[idx].astype(np.float64, copy=False),
        "exit_epoch": epochs[ext].astype(np.float64, copy=False),
        "symbol_index": np.full(idx.shape[0], symbol_index, dtype=np.int16),
    }


def _cross_pair_columns(
    *,
    signal_epochs: np.ndarray,
    target_symbol_index: int,
    raw_symbols: list[tuple[np.ndarray, np.ndarray]],
    maximum_event_gap_secs: float,
) -> np.ndarray:
    columns: list[np.ndarray] = []
    for peer_index, (peer_epochs, peer_mid) in enumerate(raw_symbols):
        zeros = np.zeros(signal_epochs.shape[0], dtype=np.float64)
        if peer_index == target_symbol_index or peer_epochs.size == 0:
            columns.extend((zeros.copy(), zeros.copy(), zeros.copy()))
            continue
        latest = np.searchsorted(peer_epochs, signal_epochs, side="right") - 1
        latest_clipped = np.maximum(latest, 0)
        fresh = (
            (latest >= 0)
            & (signal_epochs - peer_epochs[latest_clipped] >= 0.0)
            & (
                signal_epochs - peer_epochs[latest_clipped]
                <= maximum_event_gap_secs
            )
        )
        peer_returns: list[np.ndarray] = []
        for seconds in (1.0, 5.0):
            prior = (
                np.searchsorted(peer_epochs, signal_epochs - seconds, side="right")
                - 1
            )
            valid = fresh & (prior >= 0)
            prior_clipped = np.maximum(prior, 0)
            values = np.zeros(signal_epochs.shape[0], dtype=np.float64)
            values[valid] = (
                peer_mid[latest_clipped[valid]] / peer_mid[prior_clipped[valid]]
                - 1.0
            ) * 10_000.0
            peer_returns.append(values)
        columns.extend((peer_returns[0], peer_returns[1], fresh.astype(np.float64)))
    return np.column_stack(columns).astype(np.float32, copy=False)


def _backward_asof_mid(
    *,
    signal_epochs: np.ndarray,
    epochs: np.ndarray,
    mid: np.ndarray,
    maximum_event_gap_secs: float,
) -> tuple[np.ndarray, np.ndarray]:
    if epochs.size == 0:
        return (
            np.zeros(signal_epochs.shape[0], dtype=np.float64),
            np.zeros(signal_epochs.shape[0], dtype=bool),
        )
    latest = np.searchsorted(epochs, signal_epochs, side="right") - 1
    clipped = np.maximum(latest, 0)
    age = signal_epochs - epochs[clipped]
    valid = (
        (latest >= 0)
        & (age >= 0.0)
        & (age <= maximum_event_gap_secs)
        & np.isfinite(mid[clipped])
        & (mid[clipped] > 0.0)
    )
    values = np.zeros(signal_epochs.shape[0], dtype=np.float64)
    values[valid] = mid[clipped[valid]]
    return values, valid


def _graph_dislocation_columns(
    *,
    signal_epochs: np.ndarray,
    target_symbol_index: int,
    symbols: list[str],
    raw_symbols: list[tuple[np.ndarray, np.ndarray]],
    maximum_event_gap_secs: float,
) -> np.ndarray:
    """Build causal triangular-basis features from independent peer quotes."""

    output = np.zeros((signal_epochs.shape[0], len(GRAPH_FEATURE_NAMES)))
    target_symbol = symbols[target_symbol_index]
    formula = GRAPH_FORMULAS.get(target_symbol)
    if formula is None:
        return output.astype(np.float32)
    leg_a, leg_b, operation = formula
    symbol_index = {symbol: index for index, symbol in enumerate(symbols)}
    if leg_a not in symbol_index or leg_b not in symbol_index:
        return output.astype(np.float32)

    target_epochs, target_mid = raw_symbols[target_symbol_index]
    target_values, target_valid = _backward_asof_mid(
        signal_epochs=signal_epochs,
        epochs=target_epochs,
        mid=target_mid,
        maximum_event_gap_secs=maximum_event_gap_secs,
    )
    a_epochs, a_mid = raw_symbols[symbol_index[leg_a]]
    b_epochs, b_mid = raw_symbols[symbol_index[leg_b]]
    a_values, a_valid = _backward_asof_mid(
        signal_epochs=signal_epochs,
        epochs=a_epochs,
        mid=a_mid,
        maximum_event_gap_secs=maximum_event_gap_secs,
    )
    b_values, b_valid = _backward_asof_mid(
        signal_epochs=signal_epochs,
        epochs=b_epochs,
        mid=b_mid,
        maximum_event_gap_secs=maximum_event_gap_secs,
    )
    valid = target_valid & a_valid & b_valid
    implied = np.zeros(signal_epochs.shape[0], dtype=np.float64)
    if operation == "multiply":
        implied[valid] = a_values[valid] * b_values[valid]
    else:
        implied[valid] = a_values[valid] / b_values[valid]
    valid &= np.isfinite(implied) & (implied > 0.0)
    residual = np.zeros(signal_epochs.shape[0], dtype=np.float64)
    residual[valid] = np.log(target_values[valid] / implied[valid]) * 10_000.0

    mean = 0.0
    variance = 0.0
    observations = 0
    previous = 0.0
    alpha = 2.0 / 101.0
    for index in np.flatnonzero(valid):
        current = float(residual[index])
        if observations >= 20:
            centered = current - mean
            output[index, 0] = centered
            output[index, 1] = centered / max(math.sqrt(variance), 0.05)
            output[index, 2] = current - previous
            output[index, 3] = 1.0
        if observations == 0:
            mean = current
            variance = 0.0
        else:
            prior_mean = mean
            mean = prior_mean + alpha * (current - prior_mean)
            variance = (1.0 - alpha) * (
                variance + alpha * (current - prior_mean) ** 2
            )
        previous = current
        observations += 1
    return output.astype(np.float32, copy=False)


def _build_dataset(
    payload: Mapping[str, Any],
    arrays: Mapping[str, np.ndarray],
    *,
    horizon_secs: float,
    maximum_event_gap_secs: float,
    allow_missing_symbols: bool = False,
) -> dict[str, np.ndarray]:
    symbols = list(payload.get("symbol_scope") or [])
    summaries = payload.get("symbols")
    if not symbols or not isinstance(summaries, dict):
        raise ScreenRefusal("capture_symbol_scope_invalid")
    counts = [int((summaries.get(symbol) or {}).get("observations") or 0) for symbol in symbols]
    invalid_count = any(
        count < 100 and not (allow_missing_symbols and count == 0)
        for count in counts
    )
    if invalid_count or sum(counts) != arrays["bid"].shape[0]:
        raise ScreenRefusal("capture_symbol_counts_invalid")
    raw_symbols: list[tuple[np.ndarray, np.ndarray]] = []
    offset = 0
    for count in counts:
        end = offset + count
        epochs = arrays["sample_epoch"][offset:end].astype(np.float64)
        bid = arrays["bid"][offset:end].astype(np.float64)
        ask = arrays["ask"][offset:end].astype(np.float64)
        raw_symbols.append((epochs, (bid + ask) / 2.0))
        offset = end
    rows: list[dict[str, np.ndarray]] = []
    offset = 0
    for symbol_index, count in enumerate(counts):
        if count == 0:
            continue
        end = offset + count
        selected = arrays["symbol_index"][offset:end]
        if selected.shape[0] != count or np.any(selected != symbol_index):
            raise ScreenRefusal(f"capture_symbol_partition_invalid:{symbol_index}")
        row = _build_symbol_rows(
            symbol_index=symbol_index,
            symbol_count=len(symbols),
            epochs=arrays["sample_epoch"][offset:end].astype(np.float64),
            broker_epochs=(
                arrays["broker_quote_epoch"][offset:end].astype(np.float64)
                if "broker_quote_epoch" in arrays
                else arrays["sample_epoch"][offset:end].astype(np.float64)
            ),
            bid=arrays["bid"][offset:end].astype(np.float64),
            ask=arrays["ask"][offset:end].astype(np.float64),
            horizon_secs=horizon_secs,
            maximum_event_gap_secs=maximum_event_gap_secs,
        )
        graph_columns = _graph_dislocation_columns(
            signal_epochs=row["signal_epoch"],
            target_symbol_index=symbol_index,
            symbols=symbols,
            raw_symbols=raw_symbols,
            maximum_event_gap_secs=maximum_event_gap_secs,
        )
        row["X"] = np.column_stack(
            (
                row["X"],
                _cross_pair_columns(
                    signal_epochs=row["signal_epoch"],
                    target_symbol_index=symbol_index,
                    raw_symbols=raw_symbols,
                    maximum_event_gap_secs=maximum_event_gap_secs,
                ),
                graph_columns,
            )
        ).astype(np.float32, copy=False)
        row["graph_centered_residual_bps"] = graph_columns[:, 0].astype(
            np.float64
        )
        row["graph_fresh"] = graph_columns[:, 3].astype(np.float64)
        rows.append(row)
        offset = end
    return {key: np.concatenate([row[key] for row in rows]) for key in rows[0]}


def _fit_model(X: np.ndarray, y: np.ndarray) -> HistGradientBoostingRegressor:
    model = HistGradientBoostingRegressor(
        loss="squared_error",
        learning_rate=0.05,
        max_iter=160,
        max_leaf_nodes=31,
        min_samples_leaf=200,
        l2_regularization=2.0,
        early_stopping=True,
        validation_fraction=0.10,
        n_iter_no_change=15,
        random_state=719,
    )
    return model.fit(X, y)


def _nonoverlapping_metrics(
    dataset: Mapping[str, np.ndarray],
    indices: np.ndarray,
    pred_long: np.ndarray,
    pred_short: np.ndarray,
    *,
    threshold_bps: float,
    extra_round_trip_cost_bps: float,
) -> dict[str, Any]:
    selected: list[int] = []
    sides: list[int] = []
    for symbol in np.unique(dataset["symbol_index"][indices]):
        local = indices[dataset["symbol_index"][indices] == symbol]
        local = local[np.argsort(dataset["signal_epoch"][local], kind="stable")]
        last_exit = -math.inf
        for absolute in local:
            pl = float(pred_long[absolute])
            ps = float(pred_short[absolute])
            best = max(pl, ps)
            if best < threshold_bps or dataset["signal_epoch"][absolute] < last_exit:
                continue
            selected.append(int(absolute))
            sides.append(1 if pl >= ps else -1)
            last_exit = float(dataset["exit_epoch"][absolute])
    if not selected:
        return {
            "trades": 0,
            "buy_trades": 0,
            "sell_trades": 0,
            "mean_bps": None,
            "total_bps": None,
            "win_rate": None,
            "profit_factor": None,
        }
    chosen = np.asarray(selected, dtype=np.int64)
    side_array = np.asarray(sides, dtype=np.int8)
    realized = np.where(
        side_array > 0,
        dataset["long_bps"][chosen],
        dataset["short_bps"][chosen],
    ) - float(extra_round_trip_cost_bps)
    gains = float(realized[realized > 0.0].sum())
    losses = float(-realized[realized < 0.0].sum())
    return {
        "trades": int(realized.shape[0]),
        "buy_trades": int(np.sum(side_array > 0)),
        "sell_trades": int(np.sum(side_array < 0)),
        "mean_bps": float(np.mean(realized)),
        "median_bps": float(np.median(realized)),
        "total_bps": float(np.sum(realized)),
        "win_rate": float(np.mean(realized > 0.0)),
        "profit_factor": gains / losses if losses > 0.0 else None,
    }


def _validation_coverage_sufficient(row: Mapping[str, Any]) -> bool:
    return bool(
        int(row.get("trades") or 0) >= 100
        and int(row.get("buy_trades") or 0) >= 20
        and int(row.get("sell_trades") or 0) >= 20
    )


def _validation_candidate_eligible(row: Mapping[str, Any]) -> bool:
    return bool(
        _validation_coverage_sufficient(row)
        and row.get("mean_bps") is not None
        and float(row["mean_bps"]) > 0.0
        and row.get("total_bps") is not None
        and float(row["total_bps"]) > 0.0
        and row.get("profit_factor") is not None
        and float(row["profit_factor"]) > 1.0
    )


def _validation_economics_positive(row: Mapping[str, Any]) -> bool:
    return bool(
        row.get("mean_bps") is not None
        and float(row["mean_bps"]) > 0.0
        and row.get("total_bps") is not None
        and float(row["total_bps"]) > 0.0
        and row.get("profit_factor") is not None
        and float(row["profit_factor"]) > 1.0
    )


def _symbol_validation_summary(
    results: list[Mapping[str, Any]],
) -> dict[str, Any]:
    trials: list[dict[str, Any]] = []
    for result in results:
        horizon = float(result["horizon_secs"])
        for raw in list(result.get("symbol_validation_trials") or []):
            trials.append({"horizon_secs": horizon, **dict(raw)})
    covered = [row for row in trials if _validation_coverage_sufficient(row)]
    positive = [row for row in trials if _validation_economics_positive(row)]
    eligible = [row for row in trials if _validation_candidate_eligible(row)]

    def best(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not rows:
            return None
        return dict(
            max(
                rows,
                key=lambda row: (
                    (
                        float(row["mean_bps"])
                        if row.get("mean_bps") is not None
                        else -math.inf
                    ),
                    (
                        float(row["total_bps"])
                        if row.get("total_bps") is not None
                        else -math.inf
                    ),
                    int(row.get("trades") or 0),
                ),
            )
        )

    return {
        "validation_only": True,
        "held_out_test_opened": False,
        "selection_authority": False,
        "multiple_testing_policy": (
            "all_pair_cells_retained_no_pair_test_opened_without_separate_fixed_selection"
        ),
        "trial_count": len(trials),
        "coverage_sufficient_trial_count": len(covered),
        "raw_positive_trial_count": len(positive),
        "eligible_trial_count": len(eligible),
        "best_raw_trial": best(trials),
        "best_eligible_trial": best(eligible),
    }


def _symbol_validation_trials(
    *,
    dataset: Mapping[str, np.ndarray],
    validation_indices: np.ndarray,
    prediction_families: Mapping[str, tuple[np.ndarray, np.ndarray]],
    threshold_grid_bps: tuple[float, ...],
    symbols: list[str],
    extra_round_trip_cost_bps: float,
) -> list[dict[str, Any]]:
    """Keep validation-only pair evidence for deterministic graph families."""

    rows: list[dict[str, Any]] = []
    for family in (
        "triangular_basis_reversion",
        "triangular_basis_reversion_confirmed",
        "triangular_basis_continuation",
    ):
        prediction_long, prediction_short = prediction_families[family]
        for symbol_index, symbol in enumerate(symbols):
            local = validation_indices[
                dataset["symbol_index"][validation_indices] == symbol_index
            ]
            for threshold in threshold_grid_bps:
                rows.append(
                    {
                        "model_family": family,
                        "symbol": symbol,
                        "threshold_bps": threshold,
                        **_nonoverlapping_metrics(
                            dataset,
                            local,
                            prediction_long,
                            prediction_short,
                            threshold_bps=threshold,
                            extra_round_trip_cost_bps=(
                                extra_round_trip_cost_bps
                            ),
                        ),
                    }
                )
    return rows


def _confirmed_graph_reversion_predictions(
    dataset: Mapping[str, np.ndarray],
    score_indices: np.ndarray,
    *,
    confirmation_secs: float,
    maximum_event_gap_secs: float,
    extra_round_trip_cost_bps: float,
    maximum_residual_ratio: float = 0.80,
) -> tuple[np.ndarray, np.ndarray]:
    """Score graph reversion only after a causal same-sign contraction.

    The prior residual is selected backward-as-of at ``signal-confirmation``.
    A signal exists only when the residual is still displaced but has already
    recovered at least 20% toward zero.  The score is residual magnitude net
    of the currently known target spread and configured extra round-trip cost.
    """

    size = int(dataset["long_bps"].shape[0])
    prediction_long = np.full(size, -math.inf, dtype=np.float64)
    prediction_short = np.full(size, -math.inf, dtype=np.float64)
    confirmation = max(0.001, float(confirmation_secs))
    maximum_gap = max(0.0, float(maximum_event_gap_secs))
    ratio = min(0.999999, max(0.0, float(maximum_residual_ratio)))
    score_mask = np.zeros(size, dtype=np.bool_)
    score_mask[np.asarray(score_indices, dtype=np.int64)] = True
    spread_feature_index = BASE_FEATURE_NAMES.index("spread_bps")

    for symbol_index in np.unique(dataset["symbol_index"]):
        local = np.flatnonzero(dataset["symbol_index"] == symbol_index)
        local = local[
            np.argsort(dataset["signal_epoch"][local], kind="stable")
        ]
        epochs = dataset["signal_epoch"][local].astype(np.float64)
        residuals = dataset["graph_centered_residual_bps"][local].astype(
            np.float64
        )
        fresh = dataset["graph_fresh"][local].astype(np.float64) > 0.5
        for local_position in np.flatnonzero(score_mask[local]):
            current_absolute = int(local[local_position])
            target_epoch = float(epochs[local_position]) - confirmation
            prior_position = int(
                np.searchsorted(epochs, target_epoch, side="right") - 1
            )
            if prior_position < 0:
                continue
            prior_age = target_epoch - float(epochs[prior_position])
            if (
                prior_age < 0.0
                or prior_age > maximum_gap
                or not fresh[local_position]
                or not fresh[prior_position]
            ):
                continue
            current_residual = float(residuals[local_position])
            prior_residual = float(residuals[prior_position])
            if (
                not math.isfinite(current_residual)
                or not math.isfinite(prior_residual)
                or current_residual == 0.0
                or prior_residual == 0.0
                or math.copysign(1.0, current_residual)
                != math.copysign(1.0, prior_residual)
                or abs(current_residual) > ratio * abs(prior_residual)
            ):
                continue
            current_spread = float(
                dataset["X"][current_absolute, spread_feature_index]
            )
            net_score = (
                abs(current_residual)
                - current_spread
                - float(extra_round_trip_cost_bps)
            )
            if current_residual < 0.0:
                prediction_long[current_absolute] = net_score
            else:
                prediction_short[current_absolute] = net_score
    return prediction_long, prediction_short


def _screen_horizon(
    payload: Mapping[str, Any],
    arrays: Mapping[str, np.ndarray],
    *,
    horizon_secs: float,
    maximum_event_gap_secs: float,
    extra_round_trip_cost_bps: float,
    threshold_grid_bps: tuple[float, ...],
    max_train_rows: int,
    legacy_training: tuple[Mapping[str, Any], Mapping[str, np.ndarray]] | None = None,
) -> dict[str, Any]:
    data = _build_dataset(
        payload,
        arrays,
        horizon_secs=horizon_secs,
        maximum_event_gap_secs=maximum_event_gap_secs,
    )
    start = float(np.min(data["signal_epoch"]))
    end = float(np.max(data["signal_epoch"]))
    if legacy_training is None:
        training_data = data
        train_end = start + 0.60 * (end - start)
        test_start = start + 0.80 * (end - start)
        train = np.flatnonzero(data["exit_epoch"] < train_end)
        validation = np.flatnonzero(
            (data["signal_epoch"] >= train_end) & (data["exit_epoch"] < test_start)
        )
        test = np.flatnonzero(data["signal_epoch"] >= test_start)
        training_source = "authenticated_chronological_head"
    else:
        training_data = _build_dataset(
            legacy_training[0],
            legacy_training[1],
            horizon_secs=horizon_secs,
            maximum_event_gap_secs=maximum_event_gap_secs,
            allow_missing_symbols=True,
        )
        train_end = start
        test_start = start + 0.50 * (end - start)
        train = np.arange(training_data["X"].shape[0], dtype=np.int64)
        validation = np.flatnonzero(data["exit_epoch"] < test_start)
        test = np.flatnonzero(data["signal_epoch"] >= test_start)
        training_source = "legacy_untrusted_pre_authentication"
    if min(train.size, validation.size, test.size) < 5_000:
        raise ScreenRefusal(f"chronological_split_insufficient:{horizon_secs}")
    fitted_train = train
    if train.size > max_train_rows:
        rng = np.random.default_rng(719)
        fitted_train = np.sort(rng.choice(train, size=max_train_rows, replace=False))
    long_model = _fit_model(
        training_data["X"][fitted_train], training_data["long_bps"][fitted_train]
    )
    short_model = _fit_model(
        training_data["X"][fitted_train], training_data["short_bps"][fitted_train]
    )
    mid_model = _fit_model(
        training_data["X"][fitted_train], training_data["mid_bps"][fitted_train]
    )
    score_indices = np.concatenate((validation, test))
    direct_long = np.full(data["long_bps"].shape, -math.inf, dtype=np.float64)
    direct_short = np.full(data["short_bps"].shape, -math.inf, dtype=np.float64)
    direct_long[score_indices] = (
        long_model.predict(data["X"][score_indices])
        - extra_round_trip_cost_bps
    )
    direct_short[score_indices] = (
        short_model.predict(data["X"][score_indices])
        - extra_round_trip_cost_bps
    )
    predicted_mid = mid_model.predict(data["X"][score_indices])
    predicted_cost = (
        data["X"][score_indices, BASE_FEATURE_NAMES.index("spread_bps")].astype(np.float64)
        + extra_round_trip_cost_bps
    )
    mid_long = np.full(data["long_bps"].shape, -math.inf, dtype=np.float64)
    mid_short = np.full(data["short_bps"].shape, -math.inf, dtype=np.float64)
    mid_long[score_indices] = predicted_mid - predicted_cost
    mid_short[score_indices] = -predicted_mid - predicted_cost
    graph_long = np.full(data["long_bps"].shape, -math.inf, dtype=np.float64)
    graph_short = np.full(data["short_bps"].shape, -math.inf, dtype=np.float64)
    graph_ready = score_indices[data["graph_fresh"][score_indices] > 0.5]
    graph_residual = data["graph_centered_residual_bps"][graph_ready]
    graph_long[graph_ready] = -graph_residual
    graph_short[graph_ready] = graph_residual
    graph_momentum_long = np.full(
        data["long_bps"].shape, -math.inf, dtype=np.float64
    )
    graph_momentum_short = np.full(
        data["short_bps"].shape, -math.inf, dtype=np.float64
    )
    graph_momentum_long[graph_ready] = graph_residual
    graph_momentum_short[graph_ready] = -graph_residual
    confirmed_graph_long, confirmed_graph_short = (
        _confirmed_graph_reversion_predictions(
            data,
            score_indices,
            confirmation_secs=1.0,
            maximum_event_gap_secs=maximum_event_gap_secs,
            extra_round_trip_cost_bps=extra_round_trip_cost_bps,
        )
    )
    prediction_families = {
        "direct_executable_net": (direct_long, direct_short),
        "mid_move_minus_current_cost": (mid_long, mid_short),
        "triangular_basis_reversion": (graph_long, graph_short),
        "triangular_basis_reversion_confirmed": (
            confirmed_graph_long,
            confirmed_graph_short,
        ),
        "triangular_basis_continuation": (
            graph_momentum_long,
            graph_momentum_short,
        ),
    }
    symbol_validation_trials = _symbol_validation_trials(
        dataset=data,
        validation_indices=validation,
        prediction_families=prediction_families,
        threshold_grid_bps=threshold_grid_bps,
        symbols=list(payload.get("symbol_scope") or []),
        extra_round_trip_cost_bps=extra_round_trip_cost_bps,
    )
    trials: list[dict[str, Any]] = []
    for model_family, (prediction_long, prediction_short) in prediction_families.items():
        for threshold in threshold_grid_bps:
            metrics = _nonoverlapping_metrics(
                data,
                validation,
                prediction_long,
                prediction_short,
                threshold_bps=threshold,
                extra_round_trip_cost_bps=extra_round_trip_cost_bps,
            )
            trials.append(
                {
                    "model_family": model_family,
                    "threshold_bps": threshold,
                    **metrics,
                }
            )
    eligible = [row for row in trials if _validation_candidate_eligible(row)]
    if not eligible:
        observed = [row for row in trials if row["trades"] > 0]
        status = (
            "validation_economics_rejected"
            if any(_validation_coverage_sufficient(row) for row in observed)
            else "validation_trade_coverage_insufficient"
        )
        diagnostic = (
            max(
                observed,
                key=lambda row: (
                    int(row["trades"]),
                    float(row["total_bps"] or -math.inf),
                ),
            )
            if observed
            else None
        )
        return {
            "status": status,
            "horizon_secs": horizon_secs,
            "training_source": training_source,
            "dataset_rows": int(data["X"].shape[0]),
            "train_rows": int(train.size),
            "fitted_train_rows": int(fitted_train.size),
            "validation_rows": int(validation.size),
            "test_rows": int(test.size),
            "train_end_epoch": train_end,
            "test_start_epoch": test_start,
            "threshold_trials": trials,
            "symbol_validation_trials": symbol_validation_trials,
            "selected_threshold_bps": None,
            "validation": diagnostic,
            "test": None,
            "model_iterations": {
                "long": int(long_model.n_iter_),
                "short": int(short_model.n_iter_),
                "mid": int(mid_model.n_iter_),
            },
        }
    chosen = max(eligible, key=lambda row: (float(row["total_bps"]), float(row["mean_bps"])))
    chosen_predictions = prediction_families[str(chosen["model_family"])]
    test_metrics = _nonoverlapping_metrics(
        data,
        test,
        chosen_predictions[0],
        chosen_predictions[1],
        threshold_bps=float(chosen["threshold_bps"]),
        extra_round_trip_cost_bps=extra_round_trip_cost_bps,
    )
    return {
        "status": "screened",
        "horizon_secs": horizon_secs,
        "training_source": training_source,
        "dataset_rows": int(data["X"].shape[0]),
        "train_rows": int(train.size),
        "fitted_train_rows": int(fitted_train.size),
        "validation_rows": int(validation.size),
        "test_rows": int(test.size),
        "train_end_epoch": train_end,
        "test_start_epoch": test_start,
        "threshold_trials": trials,
        "symbol_validation_trials": symbol_validation_trials,
        "selected_model_family": str(chosen["model_family"]),
        "selected_threshold_bps": float(chosen["threshold_bps"]),
        "validation": {
            key: value
            for key, value in chosen.items()
            if key not in {"model_family", "threshold_bps"}
        },
        "test": test_metrics,
        "model_iterations": {
            "long": int(long_model.n_iter_),
            "short": int(short_model.n_iter_),
            "mid": int(mid_model.n_iter_),
        },
    }


def run(args: argparse.Namespace) -> int:
    input_root = Path(args.input_bundle).expanduser().resolve()
    output = Path(args.output).expanduser().resolve()
    if output.exists():
        raise ScreenRefusal("output_already_exists")
    if output == input_root or input_root in output.parents:
        raise ScreenRefusal("output_must_be_outside_input_bundle")
    horizons = tuple(float(item) for item in str(args.horizons_secs).split(","))
    thresholds = tuple(
        float(item) for item in str(args.threshold_grid_bps).split(",")
    )
    if (
        not horizons
        or any(not math.isfinite(item) or item <= 0.0 for item in horizons)
        or len(set(horizons)) != len(horizons)
        or not thresholds
        or any(not math.isfinite(item) or item < 0.0 for item in thresholds)
        or thresholds != tuple(sorted(set(thresholds)))
        or not math.isfinite(args.maximum_event_gap_secs)
        or args.maximum_event_gap_secs <= 0.0
        or not math.isfinite(args.extra_round_trip_cost_bps)
        or args.extra_round_trip_cost_bps < 0.0
        or args.max_train_rows < 10_000
    ):
        raise ScreenRefusal("screen_policy_invalid")
    payload, arrays = _strict_capture(input_root)
    legacy_training = None
    legacy_root_text = str(args.legacy_training_bundle or "").strip()
    if legacy_root_text:
        legacy_training = _strict_legacy_training(
            Path(legacy_root_text),
            authenticated_payload=payload,
            authenticated_arrays=arrays,
        )
    results = [
        _screen_horizon(
            payload,
            arrays,
            horizon_secs=horizon,
            maximum_event_gap_secs=float(args.maximum_event_gap_secs),
            extra_round_trip_cost_bps=float(args.extra_round_trip_cost_bps),
            threshold_grid_bps=thresholds,
            max_train_rows=int(args.max_train_rows),
            legacy_training=legacy_training,
        )
        for horizon in horizons
    ]
    eligible_results = [row for row in results if row["status"] == "screened"]
    selected = (
        max(
            eligible_results,
            key=lambda row: (
                float(row["validation"]["total_bps"]),
                float(row["validation"]["mean_bps"]),
            ),
        )
        if eligible_results
        else None
    )
    symbol_source_hours = {
        str(symbol): float(row["duration_secs"]) / 3600.0
        for symbol, row in payload["symbols"].items()
    }
    minimum_source_hours = min(symbol_source_hours.values())
    maximum_source_hours = max(symbol_source_hours.values())
    symbol_validation_summary = _symbol_validation_summary(results)
    test = selected["test"] if selected is not None else None
    economic_test_pass = bool(
        test is not None
        and test["trades"] >= 100
        and test["buy_trades"] >= 20
        and test["sell_trades"] >= 20
        and test["mean_bps"] is not None
        and test["mean_bps"] > 0.0
        and test["total_bps"] > 0.0
        and test["profit_factor"] is not None
        and test["profit_factor"] > 1.0
    )
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "research_only": True,
        "activation_authorized": False,
        "success_claim_authorized": False,
        "future_data_access": "forbidden",
        "fill_delay_events": 1,
        "execution_prices": "buy_ask_sell_bid_exit_bid_ask",
        "parameter_selection": (
            "fixed_models_validation_only_family_horizon_and_threshold"
        ),
        "capture_payload_sha256": payload["capture_payload_sha256"],
        "capture_npz_sha256": payload["npz_sha256"],
        "legacy_training_used": legacy_training is not None,
        "legacy_training_payload_sha256": (
            legacy_training[0]["payload_sha256"] if legacy_training is not None else None
        ),
        "legacy_training_npz_sha256": (
            legacy_training[0]["npz_sha256"] if legacy_training is not None else None
        ),
        "symbol_scope": list(payload["symbol_scope"]),
        "source_span_hours": minimum_source_hours,
        "minimum_symbol_source_span_hours": minimum_source_hours,
        "maximum_symbol_source_span_hours": maximum_source_hours,
        "symbol_source_span_hours": symbol_source_hours,
        "feature_names": list(_feature_names(list(payload["symbol_scope"]))),
        "cross_pair_feature_contract": (
            "backward_asof_peer_1s_5s_returns_plus_freshness_target_excluded"
        ),
        "triangular_graph_feature_contract": (
            "independent_two_leg_backward_asof_basis_causal_ewma_centering_plus_1s_same_sign_20pct_contraction_net_current_spread"
        ),
        "maximum_event_gap_secs": float(args.maximum_event_gap_secs),
        "extra_round_trip_cost_bps": float(args.extra_round_trip_cost_bps),
        "threshold_grid_bps": list(thresholds),
        "horizon_trials": len(horizons),
        "threshold_trials_per_horizon": len(thresholds),
        "model_family_trials_per_horizon": 5,
        "symbol_validation_summary": symbol_validation_summary,
        "results": results,
        "selected_horizon_secs": (
            selected["horizon_secs"] if selected is not None else None
        ),
        "selected_test": test,
        "economic_test_pass": economic_test_pass,
        "evidence_sufficiency_pass": minimum_source_hours >= 24.0 * 30.0,
        "withheld_reasons": [],
    }
    if not economic_test_pass:
        report["withheld_reasons"].append("positive_held_out_economics_missing")
    if minimum_source_hours < 24.0 * 30.0:
        report["withheld_reasons"].append("minimum_30_day_source_span_missing")
    if legacy_training is not None:
        report["withheld_reasons"].append("legacy_untrusted_training_material")
    report["report_sha256"] = _canonical_sha256(report)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(str(output))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-bundle", required=True)
    parser.add_argument("--legacy-training-bundle", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--horizons-secs", default="5,15,30")
    parser.add_argument(
        "--threshold-grid-bps", default="0,0.05,0.1,0.2,0.3,0.5,0.75,1,1.5,2"
    )
    parser.add_argument("--maximum-event-gap-secs", type=float, default=5.0)
    parser.add_argument("--extra-round-trip-cost-bps", type=float, default=0.20)
    parser.add_argument("--max-train-rows", type=int, default=300_000)
    return parser


if __name__ == "__main__":
    raise SystemExit(run(_parser().parse_args()))
