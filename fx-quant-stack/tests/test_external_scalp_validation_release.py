from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import random
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import numpy as np
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "external_scalp_validation_release.py"
SPEC = importlib.util.spec_from_file_location(
    "external_scalp_validation_release", TOOL_PATH
)
assert SPEC is not None and SPEC.loader is not None
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)


NOW = 1_800_000_000.0


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _candidate_identity() -> tuple[str, str]:
    identity = release.production_scalp_engine_identity(
        package_root=REPO_ROOT / "fx-quant-stack" / "src" / "fxstack",
        repository_root=REPO_ROOT,
    )
    return identity.engine_sha256, release.DislocationPolicy().config_sha256()


def _build_cost_model(
    root: Path,
    *,
    ig_ask_value: float = 100.02,
    capture_mode: str = "live_endpoint",
) -> tuple[Path, Path, dict]:
    source_root = root / "source-matrix"
    source_root.mkdir(parents=True)
    source_files: list[dict] = []
    source_file_paths: dict[str, str] = {}
    source_file_sha: dict[str, str] = {}
    for symbol in release.IG_MT4_SCALP_SYMBOLS:
        source_path = source_root / f"{symbol}_M1.csv"
        source_path.write_text(f"sealed source bytes for {symbol}\n", encoding="utf-8")
        digest = _sha256(source_path)
        source_files.append(
            {"symbol": symbol, "sha256": digest, "size": source_path.stat().st_size}
        )
        source_file_paths[symbol] = source_path.name
        source_file_sha[symbol] = digest

    start_day_epoch = 1_700_006_400
    source_indices: list[int] = []
    source_epochs: list[int] = []
    for symbol_index, _ in enumerate(release.IG_MT4_SCALP_SYMBOLS):
        for day_index in range(60):
            day_start = start_day_epoch + day_index * 86_400
            offsets = {
                symbol_index * 120,
                symbol_index * 120 + 60,
                3_600,
                7_200,
            }
            for offset in sorted(offsets):
                source_indices.append(symbol_index)
                source_epochs.append(day_start + offset)
    order = sorted(
        range(len(source_indices)),
        key=lambda index: (source_indices[index], source_epochs[index]),
    )
    source_indices_array = np.asarray([source_indices[index] for index in order], dtype=np.int16)
    source_epochs_array = np.asarray([source_epochs[index] for index in order], dtype=np.int64)
    source_bid_open = np.full(source_indices_array.shape, 100.0, dtype=np.float64)
    source_ask_open = np.full(source_indices_array.shape, 100.01, dtype=np.float64)
    source_bid_close = np.full(source_indices_array.shape, 100.0, dtype=np.float64)
    source_ask_close = np.full(source_indices_array.shape, 100.01, dtype=np.float64)
    source_npz = root / "dukascopy-source-samples.npz"
    np.savez(
        source_npz,
        symbol_index=source_indices_array,
        minute_epoch=source_epochs_array,
        bid_open=source_bid_open,
        ask_open=source_ask_open,
        bid_close=source_bid_close,
        ask_close=source_ask_close,
    )

    ig_start = NOW - 1_000.0
    history_capture = capture_mode == "authenticated_same_source_db_history"
    ig_samples = 101 if history_capture else 301
    sample_gap = 10.0 if history_capture else 1.0
    sample_start = ig_start - 2_000.0 if history_capture else ig_start
    capture_end = ig_start + 2.0 if history_capture else ig_start + ig_samples - 1
    ig_indices = np.repeat(
        np.arange(len(release.IG_MT4_SCALP_SYMBOLS), dtype=np.int64), ig_samples
    )
    ig_epochs = (
        np.tile(
            np.arange(ig_samples, dtype=np.float64) * sample_gap,
            len(release.IG_MT4_SCALP_SYMBOLS),
        )
        + sample_start
    )
    ig_broker_quote_epochs = ig_epochs - 0.05
    ig_market_event_received_epochs = ig_epochs - 0.02
    ig_received_epochs = ig_epochs - 0.01
    ig_event_sequences = np.tile(
        np.arange(1, ig_samples + 1, dtype=np.int64),
        len(release.IG_MT4_SCALP_SYMBOLS),
    )
    ig_event_tokens = np.asarray(
        [
            hashlib.sha256(f"{symbol_index}:{sequence}".encode()).hexdigest()
            for symbol_index in range(len(release.IG_MT4_SCALP_SYMBOLS))
            for sequence in range(1, ig_samples + 1)
        ],
        dtype="S64",
    )
    ig_bid = np.full(ig_indices.shape, 100.0, dtype=np.float64)
    ig_ask = np.full(ig_indices.shape, ig_ask_value, dtype=np.float64)
    ig_point = np.full(ig_indices.shape, 0.00001, dtype=np.float64)
    ig_tick = np.full(ig_indices.shape, 0.00001, dtype=np.float64)
    ig_digits = np.full(ig_indices.shape, 5, dtype=np.int64)
    ig_allowed = np.ones(ig_indices.shape, dtype=np.bool_)
    ig_npz = root / "ig-demo-calibration.npz"
    np.savez(
        ig_npz,
        symbol_index=ig_indices,
        sample_epoch=ig_epochs,
        broker_quote_epoch=ig_broker_quote_epochs,
        received_at_epoch=ig_received_epochs,
        market_event_received_at_epoch=ig_market_event_received_epochs,
        source_event_sequence=ig_event_sequences,
        source_event_token_sha256=ig_event_tokens,
        bid=ig_bid,
        ask=ig_ask,
        point=ig_point,
        price_tick_size=ig_tick,
        digits=ig_digits,
        trade_allowed=ig_allowed,
    )
    audit_path = root / "source-point-in-time-audit.json"
    _write_json(audit_path, {"passed": True, "errors": []})
    source_snapshot_manifest = {
        "symbol_scope": list(release.IG_MT4_SCALP_SYMBOLS),
        "start_epoch": int(np.min(source_epochs_array)),
        "end_epoch": int(np.max(source_epochs_array)),
        "files": source_files,
    }
    source_snapshot_sha = release.canonical_sha256(source_snapshot_manifest)
    source_spread = (100.01 - 100.0) / ((100.01 + 100.0) / 2.0) * 1e4
    ig_spread = (ig_ask_value - 100.0) / ((ig_ask_value + 100.0) / 2.0) * 1e4
    market_source_id_sha = "1" * 64
    market_source_audit = {
        "schema_version": release.IG_MARKET_SOURCE_AUDIT_SCHEMA,
        "authenticated": True,
        "venue_id": release.IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "market_source_schema": "fxstack_authenticated_broker_market_source_v2",
        "market_source_id_sha256": market_source_id_sha,
        "account_scope_sha256": "b" * 64,
        "producer_identity_sha256": "2" * 64,
        "terminal_producer_instance_sha256": "c" * 64,
        "terminal_lease_scope_sha256": "3" * 64,
        "credential_generation_id_sha256": "4" * 64,
        "bridge_protocol_version": "v2",
        "identity_observation_count": int(ig_indices.size),
        "first_observed_epoch": ig_start,
        "last_observed_epoch": capture_end,
        "identity_observation_chain_sha256": "5" * 64,
    }
    broker_contract_audit = {
        "schema_version": release.IG_BROKER_CONTRACT_AUDIT_SCHEMA,
        "market_source_id_sha256": market_source_id_sha,
        "observation_count": int(ig_indices.size),
        "first_observed_epoch": ig_start,
        "last_observed_epoch": capture_end,
        "contract_observation_chain_sha256": "6" * 64,
        "symbols": {
            symbol: {
                "point": 0.00001,
                "price_tick_size": 0.00001,
                "digits": 5,
                "trade_allowed": True,
            }
            for symbol in release.IG_MT4_SCALP_SYMBOLS
        },
    }
    point_in_time_audit = {
        "schema_version": release.IG_POINT_IN_TIME_AUDIT_SCHEMA,
        "passed": True,
        "errors": [],
        "minimum_samples_per_symbol": 100 if history_capture else 300,
        "minimum_duration_secs": 300.0,
        "maximum_sample_gap_secs": 5.0,
        "maximum_sample_gap_enforced": not history_capture,
        "requires_fresh_authenticated_source_events": True,
        "latest_scope_market_event_fresh": True,
        "database_read_only": history_capture,
        "sample_source": capture_mode,
        "symbols": {
            symbol: {
                "observations": ig_samples,
                "duration_secs": float((ig_samples - 1) * sample_gap),
                "max_intersample_gap_secs": sample_gap,
                "first_source_event_sequence": 1,
                "last_source_event_sequence": ig_samples,
                "unique_source_event_count": ig_samples,
                "passed": True,
            }
            for symbol in release.IG_MT4_SCALP_SYMBOLS
        },
    }
    spread_points = (ig_ask_value - 100.0) / 0.00001
    ig_capture = {
        "schema_version": release.IG_CALIBRATION_CAPTURE_SCHEMA,
        "capture_definition": release.IG_CALIBRATION_DEFINITION,
        "capture_mode": capture_mode,
        "source_errors": [],
        "venue_id": release.IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "source_id": "authenticated_ig_demo_mt4_bridge",
        "source_version": "v2",
        "scope_version": release.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(release.IG_MT4_SCALP_SYMBOLS),
        "capture_start_epoch": ig_start,
        "capture_end_epoch": capture_end,
        "created_at_epoch": capture_end + 1.0,
        "account_scope_sha256": "b" * 64,
        "terminal_producer_instance_sha256": "c" * 64,
        "market_source_audit": market_source_audit,
        "market_source_audit_sha256": release.canonical_sha256(
            market_source_audit
        ),
        "broker_contract_audit": broker_contract_audit,
        "broker_contract_snapshot_sha256": release.canonical_sha256(
            broker_contract_audit
        ),
        "point_in_time_audit": point_in_time_audit,
        "point_in_time_audit_sha256": release.canonical_sha256(
            point_in_time_audit
        ),
        "execution_contract": {
            "schema_version": release.IG_EXECUTION_CONTRACT_SCHEMA,
            "max_slippage_points": 20,
            "semantics": (
                "configured_broker_execution_tolerance_not_observed_slippage"
            ),
            "used_as_observed_cost": False,
        },
        "npz_path": ig_npz.name,
        "npz_sha256": _sha256(ig_npz),
        "npz_size_bytes": ig_npz.stat().st_size,
        "npz_arrays": {
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
        },
        "symbols": {
            symbol: {
                "observations": ig_samples,
                "duration_secs": float((ig_samples - 1) * sample_gap),
                "max_intersample_gap_secs": sample_gap,
                "median_observed_spread_bps": ig_spread,
                "p90_observed_spread_bps": ig_spread,
                "max_observed_spread_bps": ig_spread,
                "median_observed_spread_points": spread_points,
                "p90_observed_spread_points": spread_points,
                "max_observed_spread_points": spread_points,
                "point": 0.00001,
                "price_tick_size": 0.00001,
                "digits": 5,
                "trade_allowed": True,
            }
            for symbol in release.IG_MT4_SCALP_SYMBOLS
        },
    }
    ig_capture["capture_payload_sha256"] = release.canonical_sha256(ig_capture)
    fee_source_path = root / "ig-fee-source.txt"
    fee_source_path.write_text("operator-verified IG demo fee schedule\n", encoding="utf-8")
    fee_schedule = {
        "schema_version": release.FEE_SCHEDULE_SCHEMA,
        "source_errors": [],
        "venue_id": release.IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "symbol_scope": list(release.IG_MT4_SCALP_SYMBOLS),
        "effective_at_epoch": ig_start - 100.0,
        "source_document_path": fee_source_path.name,
        "source_document_sha256": _sha256(fee_source_path),
        "symbols": {
            symbol: {
                "commission_bps_per_round_trip": 0.05,
                "configured_financing_bps_per_trade": 0.0,
            }
            for symbol in release.IG_MT4_SCALP_SYMBOLS
        },
    }
    fee_schedule["operator_attestation_sha256"] = release.canonical_sha256(
        fee_schedule
    )
    fee_schedule_path = _write_json(root / "ig-fee-schedule.json", fee_schedule)
    payload = {
        "schema_version": release.COST_MODEL_SCHEMA,
        "source_errors": [],
        "venue_id": release.IG_MT4_VENUE_ID,
        "symbol_scope": list(release.IG_MT4_SCALP_SYMBOLS),
        "source_quote_definition": release.SOURCE_QUOTE_DEFINITION,
        "source_sampling_contract": release.SOURCE_SAMPLING_CONTRACT,
        "ig_calibration_definition": release.IG_CALIBRATION_DEFINITION,
        "cost_definition": release.COST_DEFINITION,
        "base_cost_multiplier": 1.0,
        "two_x_cost_multiplier": 2.0,
        "execution_max_slippage_points": 20,
        "components": {
            "source_bid_ask": True,
            "ig_cost_pad": True,
            "slippage": True,
            "commission": True,
            "financing": True,
        },
        "source_quote_npz_path": source_npz.name,
        "source_quote_npz_sha256": _sha256(source_npz),
        "source_snapshot_sha256": source_snapshot_sha,
        "ig_calibration_npz_path": ig_npz.name,
        "ig_calibration_npz_sha256": _sha256(ig_npz),
        "fee_schedule_path": fee_schedule_path.name,
        "fee_schedule_sha256": _sha256(fee_schedule_path),
        "source_capture": {
            "schema_version": release.SOURCE_QUOTE_CAPTURE_SCHEMA,
            "source": "dukascopy",
            "created_at_epoch": NOW - 2_000.0,
            "source_snapshot_sha256": source_snapshot_sha,
            "source_snapshot_manifest": source_snapshot_manifest,
            "source_file_paths": source_file_paths,
            "point_in_time_audit_path": audit_path.name,
            "point_in_time_audit_sha256": _sha256(audit_path),
            "source_errors": [],
        },
        "ig_capture": ig_capture,
        "symbols": {
            symbol: {
                "source_observations": int(
                    np.count_nonzero(source_indices_array == symbol_index)
                ),
                "source_file_sha256": source_file_sha[symbol],
                "source_independent_days": 60,
                "source_first_epoch": int(
                    np.min(source_epochs_array[source_indices_array == symbol_index])
                ),
                "source_last_epoch": int(
                    np.max(source_epochs_array[source_indices_array == symbol_index])
                ),
                "source_median_spread_bps": source_spread,
                "source_p90_spread_bps": source_spread,
                "ig_observations": ig_samples,
                "ig_duration_secs": float((ig_samples - 1) * sample_gap),
                "ig_median_spread_bps": ig_spread,
                "ig_p90_spread_bps": ig_spread,
                "ig_max_spread_bps": ig_spread,
                "point": 0.00001,
                "price_tick_size": 0.00001,
                "digits": 5,
                "trade_allowed": True,
                "commission_bps_per_round_trip": 0.05,
                "configured_financing_bps_per_trade": 0.0,
            }
            for symbol_index, symbol in enumerate(release.IG_MT4_SCALP_SYMBOLS)
        },
    }
    cost_path = _write_json(root / "cost-model.json", payload)
    return cost_path, source_root, payload


def _trade_ledger(
    *,
    engine_sha256: str,
    config_sha256: str,
    cost_model_sha256: str,
    cost_model: dict,
) -> dict:
    initial_equity = 100_000.0
    equity = initial_equity
    peak = initial_equity
    records: list[dict] = []
    start_day_epoch = 1_700_006_400
    for day_index in range(60):
        for symbol_index, symbol in enumerate(release.IG_MT4_SCALP_SYMBOLS):
            cost_row = cost_model["symbols"][symbol]
            side = "BUY" if day_index < 30 else "SELL"
            cell_index = day_index if side == "BUY" else day_index - 30
            target_hit = cell_index < 27
            decision_epoch = start_day_epoch + day_index * 86_400 + symbol_index * 120
            fill_epoch = decision_epoch + 60
            entry_epoch = float(fill_epoch)
            exit_epoch = entry_epoch + 30.0
            next_bid = 100.0
            next_ask = 100.01
            if side == "BUY":
                entry_price = 100.02
                sl_price = entry_price * (1.0 - 5.0 / 1e4)
                tp_price = entry_price * (1.0 + 15.0 / 1e4)
                exit_price = tp_price if target_hit else sl_price
                slippage = (entry_price - next_ask) / next_ask * 1e4
                gross = (exit_price - entry_price) / entry_price * 1e4
                stop_bps = (entry_price - sl_price) / entry_price * 1e4
                target_bps = (tp_price - entry_price) / entry_price * 1e4
            else:
                entry_price = 99.99
                sl_price = entry_price * (1.0 + 5.0 / 1e4)
                tp_price = entry_price * (1.0 - 15.0 / 1e4)
                exit_price = tp_price if target_hit else sl_price
                slippage = (next_bid - entry_price) / next_bid * 1e4
                gross = (entry_price - exit_price) / entry_price * 1e4
                stop_bps = (sl_price - entry_price) / entry_price * 1e4
                target_bps = (entry_price - tp_price) / entry_price * 1e4
            source_mid = (next_bid + next_ask) / 2.0
            spread_cost = (next_ask - next_bid) / source_mid * 1e4
            pad = max(0.0, cost_row["ig_p90_spread_bps"] - spread_cost)
            commission = cost_row["commission_bps_per_round_trip"]
            financing = cost_row["configured_financing_bps_per_trade"]
            base_cost = spread_cost + pad + slippage + commission + financing
            two_x_cost = 2.0 * base_cost
            net_base = gross - base_cost
            net_two_x = gross - two_x_cost
            initial_risk = stop_bps + base_cost
            net_r_base = net_base / initial_risk
            net_r_two_x = net_two_x / initial_risk
            p_star = initial_risk / (target_bps + stop_bps)
            notional = 10_000.0
            realized = net_base / 1e4 * notional
            equity_before = equity
            equity += realized
            peak = max(peak, equity)
            drawdown = (peak - equity) / peak * 100.0
            trade_id = f"trade-{day_index:02d}-{symbol_index:02d}"
            records.append(
                {
                    "trade_id": trade_id,
                    "symbol": symbol,
                    "side": side,
                    "strategy_id": release.SCALP_DISLOCATION_STRATEGY_ID,
                    "strategy_version": release.SCALP_DISLOCATION_STRATEGY_VERSION,
                    "engine_sha256": engine_sha256,
                    "config_sha256": config_sha256,
                    "source_snapshot_sha256": cost_row["source_file_sha256"],
                    "decision_bar_open_epoch": decision_epoch,
                    "fill_bar_open_epoch": fill_epoch,
                    "entry_epoch": entry_epoch,
                    "exit_epoch": exit_epoch,
                    "entry_utc_day": release._utc_day(entry_epoch),
                    "decision_bid": 100.0,
                    "decision_ask": 100.01,
                    "next_open_bid": next_bid,
                    "next_open_ask": next_ask,
                    "entry_price": entry_price,
                    "initial_sl_price": sl_price,
                    "initial_tp_price": tp_price,
                    "exit_price": exit_price,
                    "exit_reason": "TAKE_PROFIT" if target_hit else "STOP_LOSS",
                    "stop_bps": stop_bps,
                    "target_bps": target_bps,
                    "p_star": p_star,
                    "gross_mid_pnl_bps": gross,
                    "source_spread_cost_bps": spread_cost,
                    "ig_cost_pad_bps": pad,
                    "slippage_bps": slippage,
                    "commission_bps": commission,
                    "financing_bps": financing,
                    "base_total_cost_bps": base_cost,
                    "two_x_total_cost_bps": two_x_cost,
                    "net_pnl_bps_base": net_base,
                    "net_pnl_bps_2x": net_two_x,
                    "initial_risk_bps": initial_risk,
                    "net_r_base": net_r_base,
                    "net_r_2x": net_r_two_x,
                    "full_target_hit_first": target_hit,
                    "notional_account_ccy": notional,
                    "equity_before": equity_before,
                    "realized_pnl_account_ccy": realized,
                    "equity_after": equity,
                    "peak_equity_after": peak,
                    "drawdown_pct_after": drawdown,
                }
            )
    records.sort(key=lambda row: (row["exit_epoch"], row["trade_id"]))
    # Recalculate the portfolio sequence after the canonical exit ordering.
    equity = initial_equity
    peak = initial_equity
    for record in records:
        realized = record["realized_pnl_account_ccy"]
        record["equity_before"] = equity
        equity += realized
        peak = max(peak, equity)
        record["equity_after"] = equity
        record["peak_equity_after"] = peak
        record["drawdown_pct_after"] = (peak - equity) / peak * 100.0
    return {
        "schema_version": release.TRADE_LEDGER_SCHEMA,
        "source_errors": [],
        "strategy_id": release.SCALP_DISLOCATION_STRATEGY_ID,
        "strategy_version": release.SCALP_DISLOCATION_STRATEGY_VERSION,
        "engine_sha256": engine_sha256,
        "config_sha256": config_sha256,
        "venue_id": release.IG_MT4_VENUE_ID,
        "symbol_scope": list(release.IG_MT4_SCALP_SYMBOLS),
        "cost_model_sha256": cost_model_sha256,
        "portfolio_contract": {
            "initial_equity": initial_equity,
            "account_currency": "GBP",
            "risk_sizing": release.RISK_SIZING_METHOD,
            "max_concurrent_positions": 1,
        },
        "records": records,
    }


def _cell_evidence(*, ledger_sha256: str, cost_model_sha256: str, ledger: dict) -> dict:
    cells = {
        symbol: {
            side: {
                "trade_ids": [
                    record["trade_id"]
                    for record in ledger["records"]
                    if record["symbol"] == symbol and record["side"] == side
                ]
            }
            for side in ("BUY", "SELL")
        }
        for symbol in release.IG_MT4_SCALP_SYMBOLS
    }
    return {
        "schema_version": release.CELL_EVIDENCE_SCHEMA,
        "source_errors": [],
        "trade_ledger_sha256": ledger_sha256,
        "cost_model_sha256": cost_model_sha256,
        "win_definition": release.WIN_DEFINITION,
        "max_entries_per_symbol_utc_day": 1,
        "cells": cells,
    }


def _period_sharpes(matrix: np.ndarray) -> np.ndarray:
    return np.asarray(
        [release._period_sharpe(matrix[:, index]) for index in range(matrix.shape[1])]
    )


def _statistical_report(
    root: Path,
    *,
    ledger_sha256: str,
    cost_model_sha256: str,
    cell_evidence_sha256: str,
    config_sha256: str,
    source_snapshot_sha256: str,
) -> dict:
    from fxstack.validation.metrics import kurtosis, skewness
    from fxstack.validation.overfitting import (
        deflated_sharpe_ratio,
        probability_of_backtest_overfitting,
        sharpe_variance_across_trials,
    )

    observations = 40
    symbols = len(release.IG_MT4_SCALP_SYMBOLS)
    rng = np.random.default_rng(7)
    bar_returns = rng.normal(0.0, 0.002, size=(observations, symbols))
    exposure = np.sign(bar_returns)
    cost_per_turn = np.zeros_like(bar_returns)
    timestamps = np.arange(observations, dtype=np.int64) * 60 + 1_700_000_000
    mcpt_npz = root / "mcpt-input.npz"
    np.savez(
        mcpt_npz,
        timestamps=timestamps,
        lagged_signed_exposure=exposure,
        bar_returns=bar_returns,
        cost_per_turn=cost_per_turn,
    )

    def _mcpt_stat(selected: np.ndarray) -> float:
        turnover = np.abs(selected - np.roll(selected, 1, axis=0))
        net = np.sum(selected * bar_returns - turnover * cost_per_turn, axis=1)
        return release._period_sharpe(net)

    observed = _mcpt_stat(exposure)
    permutation_count = release.MIN_MCPT_PERMUTATIONS
    permutation_rng = random.Random(12345)
    null = [
        _mcpt_stat(np.roll(exposure, permutation_rng.randrange(1, observations), axis=0))
        for _ in range(permutation_count)
    ]
    p_value = (1 + sum(value >= observed for value in null)) / (
        permutation_count + 1
    )

    selected = np.full(observations, 0.01) + rng.normal(
        0.0, 0.0005, size=observations
    )
    attempt_two = rng.normal(0.0, 0.01, size=observations)
    attempt_three = rng.normal(-0.001, 0.01, size=observations)
    returns = np.column_stack((selected, attempt_two, attempt_three))
    trial_sharpes = _period_sharpes(returns)
    pbo_result = probability_of_backtest_overfitting(
        returns, n_splits=10, max_combinations=512
    )
    variance = sharpe_variance_across_trials(trial_sharpes)
    dsr_result = deflated_sharpe_ratio(
        sharpe_per_period=float(trial_sharpes[0]),
        n_obs=observations,
        n_trials=3,
        sharpe_variance_across_trials=variance,
        skew=skewness(selected),
        kurtosis=kurtosis(selected),
    )
    pbo_npz = root / "pbo-dsr-input.npz"
    np.savez(pbo_npz, aligned_returns=returns, trial_sharpes=trial_sharpes)
    selected_policy = release.DislocationPolicy().to_canonical_dict()
    attempt_two_policy = {
        **selected_policy,
        "z_entry": float(selected_policy["z_entry"]) + 0.25,
    }
    attempt_three_policy = {
        **selected_policy,
        "z_entry": float(selected_policy["z_entry"]) + 0.50,
    }
    attempts = [
        {
            "attempt_id": "selected",
            "policy": selected_policy,
            "config_sha256": config_sha256,
        },
        {
            "attempt_id": "attempt-2",
            "policy": attempt_two_policy,
            "config_sha256": release.canonical_sha256(attempt_two_policy),
        },
        {
            "attempt_id": "attempt-3",
            "policy": attempt_three_policy,
            "config_sha256": release.canonical_sha256(attempt_three_policy),
        },
    ]
    attempt_manifest_path = _write_json(
        root / "attempt-manifest.json",
        {
            "schema_version": release.ATTEMPT_MANIFEST_SCHEMA,
            "sealed_before_replay": True,
            "created_at_epoch": NOW - 5_000.0,
            "replay_started_at_epoch": NOW - 4_999.0,
            "source_snapshot_sha256": source_snapshot_sha256,
            "selected_attempt_id": "selected",
            "attempts": attempts,
        },
    )
    return {
        "schema_version": release.STATISTICAL_REPORT_SCHEMA,
        "source_errors": [],
        "trade_ledger_sha256": ledger_sha256,
        "cost_model_sha256": cost_model_sha256,
        "cell_evidence_sha256": cell_evidence_sha256,
        "mcpt": {
            "method": release.MCPT_METHOD,
            "seed": 12345,
            "n_permutations": permutation_count,
            "input_npz_path": mcpt_npz.name,
            "input_npz_sha256": _sha256(mcpt_npz),
            "observed_statistic": observed,
            "null_statistics": null,
            "p_value": p_value,
        },
        "pbo_dsr": {
            "method": release.PBO_DSR_METHOD,
            "input_npz_path": pbo_npz.name,
            "input_npz_sha256": _sha256(pbo_npz),
            "attempt_manifest_path": attempt_manifest_path.name,
            "attempt_manifest_sha256": _sha256(attempt_manifest_path),
            "attempt_ids": ["selected", "attempt-2", "attempt-3"],
            "selected_attempt_id": "selected",
            "n_splits": 10,
            "max_combinations": 512,
            "pbo": float(pbo_result["pbo"]),
            "dsr": float(dsr_result["dsr"]),
            "selected_sharpe": float(trial_sharpes[0]),
            "sharpe_variance_across_trials": variance,
        },
        "statistics": {
            "mcpt_p_value": p_value,
            "pbo": float(pbo_result["pbo"]),
            "dsr": float(dsr_result["dsr"]),
            "mcpt_observations": observations,
            "return_observations": observations,
            "attempts": 3,
            "selected_attempt_id": "selected",
        },
    }


def _write_artifacts(
    root: Path,
    *,
    ig_ask_value: float = 100.02,
    capture_mode: str = "live_endpoint",
) -> tuple[dict[str, Path], str, str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    engine_sha256, config_sha256 = _candidate_identity()
    cost_path, source_root, cost_model = _build_cost_model(
        root,
        ig_ask_value=ig_ask_value,
        capture_mode=capture_mode,
    )
    cost_sha = _sha256(cost_path)
    ledger = _trade_ledger(
        engine_sha256=engine_sha256,
        config_sha256=config_sha256,
        cost_model_sha256=cost_sha,
        cost_model=cost_model,
    )
    ledger_path = _write_json(root / "trade-ledger.json", ledger)
    ledger_sha = _sha256(ledger_path)
    cells = _cell_evidence(
        ledger_sha256=ledger_sha,
        cost_model_sha256=cost_sha,
        ledger=ledger,
    )
    cell_path = _write_json(root / "cell-evidence.json", cells)
    stats = _statistical_report(
        root,
        ledger_sha256=ledger_sha,
        cost_model_sha256=cost_sha,
        cell_evidence_sha256=_sha256(cell_path),
        config_sha256=config_sha256,
        source_snapshot_sha256=cost_model["source_snapshot_sha256"],
    )
    stats_path = _write_json(root / "statistical-report.json", stats)
    return (
        {
            "trade_ledger": ledger_path,
            "cost_model": cost_path,
            "statistical_report": stats_path,
            "cell_evidence": cell_path,
        },
        engine_sha256,
        config_sha256,
        source_root,
    )


def _write_keys(root: Path) -> tuple[Path, Path, Ed25519PrivateKey]:
    signing_key = Ed25519PrivateKey.generate()
    signing_path = root / "issuer.pem"
    signing_path.write_bytes(
        signing_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    verify_path = root / "issuer.pub"
    verify_path.write_bytes(
        signing_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return signing_path, verify_path, signing_key


def _prepare(
    root: Path,
    artifacts: dict[str, Path],
    source_root: Path,
    *,
    suffix: str = "",
    now=NOW,
):
    return release.prepare_issuance_request(
        artifact_paths=artifacts,
        source_root=source_root,
        generation_id="scalp-validation-generation-7",
        output_path=root / f"issuance-request{suffix}.json",
        package_root=REPO_ROOT / "fx-quant-stack" / "src" / "fxstack",
        repository_root=REPO_ROOT,
        validity_secs=86_400.0,
        now_epoch=now,
    )


def test_strict_artifacts_prepare_and_issue_production_verifier_bundle(
    tmp_path: Path,
) -> None:
    artifacts, engine_sha256, config_sha256, source_root = _write_artifacts(tmp_path)
    request_path, request, _ = _prepare(tmp_path, artifacts, source_root)
    signing_path, verify_path, signing_key = _write_keys(tmp_path)

    output, bundle = release.issue_validation_bundle(
        request_path=request_path,
        artifact_paths=artifacts,
        source_root=source_root,
        signing_key_path=signing_path,
        verify_key_path=verify_path,
        output_path=tmp_path / "bundle.json",
        bootstrap_registry=True,
        now_epoch=NOW,
    )

    assert output.is_file()
    evidence = request["certificate_claims"]["evidence"]
    assert evidence["overall"]["trades"] == 1_320
    assert evidence["overall"]["independent_days"] == 60
    assert evidence["overall"]["mcpt_p_value"] <= 0.05
    assert evidence["overall"]["pbo"] <= 0.40
    assert evidence["overall"]["dsr"] >= 0.95
    assert evidence["two_x_cost_stress"]["ci_lower"] > 0.0
    assert request["certificate_claims"]["engine_sha256"] == engine_sha256
    assert request["certificate_claims"]["config_sha256"] == config_sha256
    certificate = bundle["certificate"]
    verification = release.verify_scalp_validation_evidence(
        certificate=certificate,
        revocation_registry=bundle["revocation_registry"],
        public_key=signing_key.public_key(),
        expectation=release.ScalpValidationExpectation(
            generation_id=certificate["generation_id"],
            strategy_id=certificate["strategy_id"],
            strategy_version=certificate["strategy_version"],
            engine_sha256=certificate["engine_sha256"],
            config_sha256=certificate["config_sha256"],
        ),
        now_epoch=NOW,
    )
    assert verification.valid is True
    assert len(verification.win_probability_lower_bounds) == 22


def test_prepare_accepts_read_only_same_source_db_history_with_gap_telemetry(
    tmp_path: Path,
) -> None:
    artifacts, _, _, source_root = _write_artifacts(
        tmp_path,
        capture_mode="authenticated_same_source_db_history",
    )

    _, request, _ = _prepare(tmp_path, artifacts, source_root)

    cost_model = json.loads(
        artifacts["cost_model"].read_text(encoding="utf-8")
    )
    audit = cost_model["ig_capture"]["point_in_time_audit"]
    assert audit["database_read_only"] is True
    assert audit["maximum_sample_gap_enforced"] is False
    assert audit["latest_scope_market_event_fresh"] is True
    assert audit["symbols"]["EURUSD"]["observations"] == 101
    assert audit["symbols"]["EURUSD"]["max_intersample_gap_secs"] == 10.0
    assert request["certificate_claims"]["evidence"]["overall"]["trades"] > 0


@pytest.mark.parametrize(
    ("capture_mode", "field", "value"),
    (
        ("live_endpoint", "maximum_sample_gap_enforced", False),
        (
            "authenticated_same_source_db_history",
            "latest_scope_market_event_fresh",
            False,
        ),
        (
            "authenticated_same_source_db_history",
            "database_read_only",
            False,
        ),
        (
            "authenticated_same_source_db_history",
            "minimum_samples_per_symbol",
            99,
        ),
    ),
)
def test_prepare_refuses_capture_mode_audit_contract_drift(
    tmp_path: Path,
    capture_mode: str,
    field: str,
    value: bool | int,
) -> None:
    artifacts, _, _, source_root = _write_artifacts(
        tmp_path,
        capture_mode=capture_mode,
    )
    cost_model = json.loads(
        artifacts["cost_model"].read_text(encoding="utf-8")
    )
    capture = cost_model["ig_capture"]
    capture["point_in_time_audit"][field] = value
    capture["point_in_time_audit_sha256"] = release.canonical_sha256(
        capture["point_in_time_audit"]
    )
    capture["capture_payload_sha256"] = release.canonical_sha256(
        {
            key: item
            for key, item in capture.items()
            if key != "capture_payload_sha256"
        }
    )
    _write_json(artifacts["cost_model"], cost_model)

    with pytest.raises(
        release.ReleaseRefusal,
        match="validation_cost_model_ig_capture_invalid",
    ):
        _prepare(tmp_path, artifacts, source_root)


def test_prepare_refuses_statistical_sidecar_tamper(tmp_path: Path) -> None:
    artifacts, _, _, source_root = _write_artifacts(tmp_path)
    (tmp_path / "mcpt-input.npz").write_bytes(b"tampered\n")

    with pytest.raises(
        release.ReleaseRefusal,
        match="validation_statistical_mcpt_input_sha256_mismatch",
    ):
        _prepare(tmp_path, artifacts, source_root)

    assert not (tmp_path / "issuance-request.json").exists()


def test_prepare_refuses_duplicate_authenticated_ig_event_identity(
    tmp_path: Path,
) -> None:
    artifacts, _, _, source_root = _write_artifacts(tmp_path)
    ig_npz = tmp_path / "ig-demo-calibration.npz"
    with np.load(ig_npz, allow_pickle=False) as raw:
        arrays = {name: np.asarray(raw[name]).copy() for name in raw.files}
    arrays["source_event_sequence"][1] = arrays["source_event_sequence"][0]
    np.savez(ig_npz, **arrays)
    cost_model = json.loads(
        artifacts["cost_model"].read_text(encoding="utf-8")
    )
    capture = cost_model["ig_capture"]
    capture["npz_sha256"] = _sha256(ig_npz)
    capture["npz_size_bytes"] = ig_npz.stat().st_size
    capture["capture_payload_sha256"] = release.canonical_sha256(
        {
            key: value
            for key, value in capture.items()
            if key != "capture_payload_sha256"
        }
    )
    cost_model["ig_calibration_npz_sha256"] = _sha256(ig_npz)
    _write_json(artifacts["cost_model"], cost_model)

    with pytest.raises(
        release.ReleaseRefusal,
        match="validation_cost_model_ig_event_identity_invalid:EURUSD",
    ):
        _prepare(tmp_path, artifacts, source_root)

    assert not (tmp_path / "issuance-request.json").exists()


def test_prepare_refuses_fee_schedule_cost_row_divergence(tmp_path: Path) -> None:
    artifacts, _, _, source_root = _write_artifacts(tmp_path)
    fee_path = tmp_path / "ig-fee-schedule.json"
    fee_schedule = json.loads(fee_path.read_text(encoding="utf-8"))
    fee_schedule["symbols"]["EURUSD"]["commission_bps_per_round_trip"] = 0.0
    fee_schedule["operator_attestation_sha256"] = release.canonical_sha256(
        {
            key: value
            for key, value in fee_schedule.items()
            if key != "operator_attestation_sha256"
        }
    )
    _write_json(fee_path, fee_schedule)
    cost_model = json.loads(
        artifacts["cost_model"].read_text(encoding="utf-8")
    )
    cost_model["fee_schedule_sha256"] = _sha256(fee_path)
    _write_json(artifacts["cost_model"], cost_model)

    with pytest.raises(
        release.ReleaseRefusal,
        match="validation_cost_model_fee_binding_mismatch:EURUSD",
    ):
        _prepare(tmp_path, artifacts, source_root)

    assert not (tmp_path / "issuance-request.json").exists()


def test_prepare_refuses_aggregate_only_or_changed_ledger(tmp_path: Path) -> None:
    artifacts, _, _, source_root = _write_artifacts(tmp_path)
    stats = json.loads(artifacts["statistical_report"].read_text(encoding="utf-8"))
    stats["mcpt"].pop("input_npz_path")
    _write_json(artifacts["statistical_report"], stats)
    with pytest.raises(
        release.ReleaseRefusal,
        match="validation_statistical_mcpt_scope_invalid",
    ):
        _prepare(tmp_path, artifacts, source_root, suffix="-aggregate")

    ledger = json.loads(artifacts["trade_ledger"].read_text(encoding="utf-8"))
    ledger["records"][0]["net_r_base"] += 0.5
    _write_json(artifacts["trade_ledger"], ledger)
    with pytest.raises(
        release.ReleaseRefusal,
        match="validation_trade_cost_arithmetic_invalid",
    ):
        _prepare(tmp_path, artifacts, source_root, suffix="-ledger")


def test_prepare_reports_exact_failed_economic_gate_values(tmp_path: Path) -> None:
    artifacts, _, _, source_root = _write_artifacts(
        tmp_path, ig_ask_value=100.08
    )

    with pytest.raises(
        release.ReleaseRefusal,
        match=(
            r"validation_evidence_two_x_cost_stress_failed:"
            r"observed_expectancy=-[0-9.]+:observed_ci_lower=-[0-9.]+:"
            r"required_expectancy_gt=0:required_ci_lower_gt=0"
        ),
    ):
        _prepare(tmp_path, artifacts, source_root)

    assert not (tmp_path / "issuance-request.json").exists()


def test_registry_rotation_is_append_only(tmp_path: Path) -> None:
    artifacts, _, _, source_root = _write_artifacts(tmp_path)
    signing_path, verify_path, _ = _write_keys(tmp_path)
    first_request, _, _ = _prepare(
        tmp_path, artifacts, source_root, suffix="-one"
    )
    first_bundle_path, first_bundle = release.issue_validation_bundle(
        request_path=first_request,
        artifact_paths=artifacts,
        source_root=source_root,
        signing_key_path=signing_path,
        verify_key_path=verify_path,
        output_path=tmp_path / "bundle-one.json",
        bootstrap_registry=True,
        now_epoch=NOW,
    )
    second_request, _, _ = _prepare(
        tmp_path, artifacts, source_root, suffix="-two", now=NOW + 60.0
    )
    _, second_bundle = release.issue_validation_bundle(
        request_path=second_request,
        artifact_paths=artifacts,
        source_root=source_root,
        signing_key_path=signing_path,
        verify_key_path=verify_path,
        previous_registry_path=first_bundle_path,
        output_path=tmp_path / "bundle-two.json",
        now_epoch=NOW + 60.0,
    )

    first_sha = first_bundle["certificate"][release.CERTIFICATE_SHA256_FIELD]
    registry = second_bundle["revocation_registry"]
    assert registry["registry_revision"] == 2
    assert registry["revoked_certificate_sha256s"] == [first_sha]
    assert registry["active_certificate_sha256"] != first_sha


def test_tool_has_no_key_generation_or_runtime_mutation_capability() -> None:
    source = TOOL_PATH.read_text(encoding="utf-8")
    assert "Ed25519PrivateKey.generate" not in source
    assert "RuntimeService" not in source
    assert "/v2/" not in source
    assert "production_scalp_authority" not in source
