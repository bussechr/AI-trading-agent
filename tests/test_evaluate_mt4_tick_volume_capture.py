from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tools import evaluate_mt4_tick_volume_capture as evaluator
from tools import verify_mt4_tick_volume_capture_handoff as handoff

screen = evaluator.screen
T0 = datetime(2020, 1, 1, tzinfo=UTC)
END = T0 + timedelta(days=handoff.PROSPECTIVE_WINDOW_DAYS)


def _identity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _fake_identity(filename: str, marker: str) -> dict[str, Any]:
    raw = marker.encode("ascii")
    return {
        "filename": filename,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _cost_rows(account_currency: str = "USD") -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for symbol in screen.MTVCLC_SYMBOLS:
        p90 = 3.0
        commission = 0.0
        financing = 0.0
        pnl_currency = symbol[3:]
        conversion_applies = pnl_currency != account_currency
        conversion = (
            screen.IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
            if conversion_applies
            else 0.0
        )
        calibration = screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=p90,
            commission_bps_per_round_trip=commission,
            financing_bps_per_trade=financing,
            account_currency=account_currency,
            pnl_currency=pnl_currency,
            convert_on_close_charge_fraction=conversion,
            source_sha256="8" * 64,
        )
        rows[symbol] = {
            "p90_ig_spread_bps": p90,
            "commission_bps_per_round_trip": commission,
            "financing_bps_per_trade": financing,
            "fixed_adverse_execution_debit_bps": (
                screen.FIXED_ADVERSE_EXECUTION_DEBIT_BPS
            ),
            "pre_conversion_geometry_cost_bps": calibration.recorded_cost_bps,
            "profit_loss_currency": pnl_currency,
            "account_currency": account_currency,
            "conversion_rate_of_absolute_profit_or_loss": (
                screen.IG_STANDARD_CONVERT_ON_CLOSE_CHARGE_FRACTION
            ),
            "convert_on_close_charge_fraction_for_screen": conversion,
            "conversion_applies": conversion_applies,
            "conversion_adjusted_break_even_win_probability": (
                calibration.break_even_win_probability
            ),
            "commission_status": "explicit_source_attested",
            "financing_status": (
                "structurally_avoided_by_fixed_rollover_guard"
            ),
            "conversion_status": (
                "debit_absolute_profit_or_loss_when_account_currency_differs"
            ),
        }
    return rows


def _preregistration_body() -> dict[str, Any]:
    source_roles = (
        "ig_mt4_forex_product_details",
        "ig_mt4_crypto_product_details",
        "ig_spread_betting_cfd_product_details",
    )
    attempt = screen.attempt_manifest()
    return {
        "schema_version": handoff.PREREGISTRATION_SCHEMA,
        "sealed_at_utc": (T0 - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "research_only": True,
        "strategy": {
            "strategy_id": screen.STRATEGY_ID,
            "strategy_version": screen.STRATEGY_VERSION,
            "config_id": screen.CONFIG_ID,
            "config_sha256": evaluator.canonical_sha256(asdict(screen.GRID[0])),
            "source_contract_id": screen.SOURCE_CONTRACT_ID,
            "activity_metric_id": screen.ACTIVITY_METRIC_ID,
            "attempt_manifest": attempt,
            "attempt_manifest_sha256": evaluator.canonical_sha256(attempt),
        },
        "scope": {
            "venue_id": handoff.VENUE_ID,
            "scope_version": handoff.SCOPE_VERSION,
            "ordered_symbols": list(screen.MTVCLC_SYMBOLS),
            "sides": ["BUY", "SELL"],
            "cell_order": [
                {"config_id": screen.CONFIG_ID, "symbol": symbol, "side": side}
                for symbol in screen.MTVCLC_SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_654,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_698,
        },
        "prospective_window": {
            "t0_utc_inclusive": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc_exclusive": END.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "consecutive_days": handoff.PROSPECTIVE_WINDOW_DAYS,
            "fixed_before_any_eligible_observation": True,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_or_outcome_evaluation_forbidden": True,
            "interim_performance_statistics_forbidden": True,
            "early_success_forbidden": True,
            "success_evaluation_not_before_utc": END.strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "no_optional_extension_or_restart_after_failure": True,
            "data_quality_monitoring_must_not_compute_performance": True,
        },
        "execution_contract": {
            "entry_type": "immediate_market",
            "pending_orders_forbidden": True,
            "maximum_entries_per_symbol_utc_day": 1,
            "outcome_horizon_m1_bars": 30,
            "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
            "rollover_entry_blackout_half_open": True,
            "signals_inside_blackout_reserve": False,
        },
        "cost_policy": {
            **evaluator.EXPECTED_COST_POLICY,
            "symbols": _cost_rows(),
        },
        "fixed_success_gates": {
            "all_44_cells_must_pass": True,
            "minimum_trades_per_cell": screen.MIN_TRADES_PER_CELL,
            "minimum_independent_utc_days_per_cell": (
                screen.MIN_INDEPENDENT_DAYS_PER_CELL
            ),
            "cell_win_probability_interval": (
                "one_sided_wilson_family_adjusted_over_44_cells"
            ),
            "cell_win_probability_family_confidence": (
                screen.WIN_PROBABILITY_FAMILY_CONFIDENCE
            ),
            "cell_win_probability_lower_bound_strictly_greater_than": (
                "the_exact_per_symbol_conversion_adjusted_break_even_win_"
                "probability_in_cost_policy.symbols"
            ),
            "minimum_unconverted_break_even_win_probability": (
                screen.BASE_COST_BREAK_EVEN_WIN_PROBABILITY
            ),
            "cell_conversion_adjusted_mean_net_bps_strictly_greater_than": 0.0,
            "minimum_total_trades": evaluator.MINIMUM_TOTAL_TRADES,
            "minimum_total_independent_utc_days": (
                evaluator.MINIMUM_TOTAL_INDEPENDENT_DAYS
            ),
            "source_scope_ready_required": True,
            "source_errors_required": [],
            "descriptive_df99_bonferroni_abs_t_threshold": (
                screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
            ),
        },
        "source_identities": {
            "screen_source": _identity(Path(screen.__file__)),
            "collector_source": _fake_identity(
                "capture_ig_mt4_m1_activity.py", "collector"
            ),
            "sealer_source": _fake_identity(
                "seal_mt4_tick_volume_preregistration.py", "sealer"
            ),
            "scope_catalog_source": _fake_identity(
                "ig_mt4_catalog.py", "catalog"
            ),
            "cost_capture": {
                "capture_json": {
                    "filename": "ig_mt4_bid_ask_capture.json",
                    "sha256": "8" * 64,
                    "size_bytes": 100,
                },
                "capture_npz": {
                    "filename": "ig_mt4_bid_ask_samples.npz",
                    "sha256": "9" * 64,
                    "size_bytes": 100,
                },
                "capture_payload_sha256": "a" * 64,
                "capture_mode": "authenticated_same_source_db_history",
                "scope_version": handoff.SCOPE_VERSION,
                "venue_id": handoff.VENUE_ID,
            },
            "fee_attestation": {
                "attestation": {
                    "filename": "mtvclc_fee_attestation.json",
                    "sha256": "b" * 64,
                    "size_bytes": 100,
                },
                "operator_attestation_sha256": "c" * 64,
                "effective_at_utc": "2019-01-01T00:00:00Z",
                "attested_at_utc": "2019-12-31T00:00:00Z",
                "account_currency": "USD",
                "source_documents": [
                    {
                        "role": role,
                        "url": f"https://www.ig.com/{role}",
                        "retrieved_at_utc": "2019-12-31T00:00:00Z",
                        "filename": f"{role}.txt",
                        "sha256": hashlib.sha256(role.encode()).hexdigest(),
                        "size_bytes": len(role),
                    }
                    for role in source_roles
                ],
            },
            "production_runtime_context": {
                "relationship": (
                    "context_only_successor_not_integrated_or_authorized"
                ),
                "engine_identity": {},
                "active_strategy_family_context": "context-only",
                "active_strategy_version_context": "context-only",
                "active_policy_config_sha256_context": "d" * 64,
            },
        },
        "isolation_contract": {
            "artifact_grants_no_input_or_outcome_access": True,
            "outcome_evaluation_requires_a_physically_isolated_research_host": True,
            "production_database_bridge_broker_credentials_registry_and_issuer_must_not_be_mounted": True,
        },
        "authority": dict(handoff.FALSE_AUTHORITY),
    }


def _write_preregistration(root: Path) -> Path:
    root.mkdir(parents=True)
    body = _preregistration_body()
    body_sha = handoff.canonical_sha256(body)
    payload = {**body, "preregistration_body_sha256": body_sha}
    path = root / f"mtvclc_v1_preregistration_{body_sha}.json"
    path.write_bytes(handoff.canonical_json_bytes(payload))
    return path


def _source(source_id: str) -> dict[str, Any]:
    return {
        "market_source_schema": handoff.MARKET_SOURCE_SCHEMA,
        "market_source_id": source_id,
        "market_source_authenticated": True,
        "broker_account_mode": "demo",
        "broker_venue_id": handoff.VENUE_ID,
        "broker_account_scope_sha256": "2" * 64,
        "producer_identity_sha256": "3" * 64,
        "producer_instance_id_sha256": "4" * 64,
        "terminal_lease_scope_sha256": "5" * 64,
        "credential_generation_id_sha256": "6" * 64,
        "bridge_protocol_version": handoff.BRIDGE_PROTOCOL_VERSION,
    }


def _bar(symbol: str, minute: int) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "minute_epoch": minute,
        "bid_open": 1.0,
        "bid_high": 1.0,
        "bid_low": 1.0,
        "bid_close": 1.0,
        "tick_volume": 10,
        "price_basis": handoff.PRICE_BASIS,
        "volume_source": handoff.VOLUME_SOURCE,
    }


def _quote(symbol: str, sequence: int, epoch: float) -> dict[str, Any]:
    token = hashlib.sha256(f"{symbol}:{sequence}".encode()).hexdigest()
    event_received = epoch - 0.1
    snapshot = {
        "symbol": symbol,
        "transport_received_at_epoch": epoch,
        "bid": 1.0,
        "ask": 1.0001,
        "market_event_received_at_epoch": event_received,
        "market_event_sequence": sequence,
        "source_event_token_sha256": token,
    }
    return {
        "symbol": symbol,
        "observation_sequence": sequence,
        "observation_epoch": int(epoch),
        "observed_at_epoch": epoch,
        "bid": 1.0,
        "ask": 1.0001,
        "transport_received_at_epoch": epoch,
        "market_event_received_at_epoch": event_received,
        "market_event_sequence": sequence,
        "source_event_token_sha256": token,
        "snapshot_sha256": handoff.canonical_sha256(snapshot),
    }


def _chunk(
    *,
    binding: handoff.ProspectiveBinding,
    source_id: str,
    utc_hour: str,
    cycle_started: float,
    cycle_completed: float,
    bars: list[dict[str, Any]],
    quotes: list[dict[str, Any]],
    last_bars: Mapping[str, int],
    sequences: Mapping[str, int],
    transports: Mapping[str, float],
    snapshots: Mapping[str, str],
) -> dict[str, Any]:
    return {
        "schema_version": handoff.CHUNK_SCHEMA,
        "collector_schema_version": handoff.COLLECTOR_SCHEMA,
        "source_contract_id": handoff.SOURCE_CONTRACT_ID,
        "activity_metric_id": handoff.ACTIVITY_METRIC_ID,
        "scope_version": handoff.SCOPE_VERSION,
        "symbol_scope": list(handoff.SYMBOLS),
        "timeframe": handoff.TIMEFRAME,
        "minimum_m1_history_bars": handoff.MINIMUM_M1_BARS,
        "maximum_quote_gap_seconds": handoff.MAXIMUM_QUOTE_GAP_SECONDS,
        "requested_bar_limit": 400,
        "configured_tick_interval_seconds": 2.0,
        "utc_hour": utc_hour,
        "segment_index": 1,
        "collector_cycle_started_at_epoch": cycle_started,
        "collector_cycle_completed_at_epoch": cycle_completed,
        "observed_at_epoch": cycle_completed,
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "source": _source(source_id),
        "bars": bars,
        "quotes": quotes,
        "last_bar_epoch_by_symbol": dict(last_bars),
        "last_tick_sequence_by_symbol": dict(sequences),
        "last_tick_transport_epoch_by_symbol": dict(transports),
        "last_tick_snapshot_sha256_by_symbol": dict(snapshots),
        "collection_only": True,
        "evaluation_performed": False,
        "success_claim_authorized": False,
        "authority_granted": False,
        "activation_authorized": False,
        "order_authorized": False,
    }


def _write_capture(root: Path, preregistration: Path) -> Path:
    root.mkdir()
    binding = handoff.load_preregistration(preregistration)
    source_id = "1" * 64
    last_bars = {symbol: 0 for symbol in handoff.SYMBOLS}
    sequences = {symbol: 0 for symbol in handoff.SYMBOLS}
    transports = {symbol: 0.0 for symbol in handoff.SYMBOLS}
    snapshots = {symbol: "0" * 64 for symbol in handoff.SYMBOLS}
    chunks: list[dict[str, Any]] = []

    first_bars: list[dict[str, Any]] = []
    first_quotes: list[dict[str, Any]] = []
    first_minute = int(T0.timestamp()) - 240 * 60
    first_quote_epoch = T0.timestamp() + 1.0
    for symbol in handoff.SYMBOLS:
        for offset in range(240):
            row = _bar(symbol, first_minute + offset * 60)
            first_bars.append(row)
            last_bars[symbol] = row["minute_epoch"]
        quote = _quote(symbol, 1, first_quote_epoch)
        first_quotes.append(quote)
        sequences[symbol] = 1
        transports[symbol] = first_quote_epoch
        snapshots[symbol] = quote["snapshot_sha256"]
    chunks.append(
        _chunk(
            binding=binding,
            source_id=source_id,
            utc_hour=T0.strftime("%Y%m%dT%H"),
            cycle_started=T0.timestamp() + 2.0,
            cycle_completed=T0.timestamp() + 3.0,
            bars=first_bars,
            quotes=first_quotes,
            last_bars=last_bars,
            sequences=sequences,
            transports=transports,
            snapshots=snapshots,
        )
    )

    final_quotes: list[dict[str, Any]] = []
    final_bars: list[dict[str, Any]] = []
    final_quote_epoch = END.timestamp() - 2.0
    for symbol in handoff.SYMBOLS:
        row = _bar(symbol, int(END.timestamp()) - 120)
        final_bars.append(row)
        last_bars[symbol] = row["minute_epoch"]
        quote = _quote(symbol, 2, final_quote_epoch)
        final_quotes.append(quote)
        sequences[symbol] = 2
        transports[symbol] = final_quote_epoch
        snapshots[symbol] = quote["snapshot_sha256"]
    chunks.append(
        _chunk(
            binding=binding,
            source_id=source_id,
            utc_hour=datetime.fromtimestamp(
                final_quote_epoch, tz=UTC
            ).strftime("%Y%m%dT%H"),
            cycle_started=END.timestamp() - 3.0,
            cycle_completed=END.timestamp() - 1.0,
            bars=final_bars,
            quotes=final_quotes,
            last_bars=last_bars,
            sequences=sequences,
            transports=transports,
            snapshots=snapshots,
        )
    )

    previous = handoff.ZERO_SHA256
    manifest_lines: list[bytes] = []
    for sequence, chunk in enumerate(chunks, start=1):
        utc_hour = str(chunk["utc_hour"])
        relative = (
            f"chunks/{utc_hour}/"
            f"ig-mt4-m1-activity-s0001-q{sequence:010d}.json"
        )
        chunk_path = root.joinpath(*relative.split("/"))
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        chunk_raw = handoff.canonical_json_bytes(chunk) + b"\n"
        chunk_path.write_bytes(chunk_raw)
        entry: dict[str, Any] = {
            "schema_version": handoff.MANIFEST_SCHEMA,
            "sequence": sequence,
            "previous_entry_sha256": previous,
            "chunk_path": relative,
            "chunk_sha256": hashlib.sha256(chunk_raw).hexdigest(),
            "chunk_size_bytes": len(chunk_raw),
            "chunk_schema_version": handoff.CHUNK_SCHEMA,
            "utc_hour": utc_hour,
            "segment_index": 1,
            "market_source_id": source_id,
            "preregistration_body_sha256": binding.preregistration_body_sha256,
            "preregistration_artifact_sha256": (
                binding.preregistration_artifact_sha256
            ),
            "prospective_t0_utc_inclusive": binding.t0_utc,
            "prospective_end_utc_exclusive": binding.end_utc_exclusive,
            "bar_rows": len(chunk["bars"]),
            "quote_rows": len(chunk["quotes"]),
            "last_bar_epoch_by_symbol": chunk["last_bar_epoch_by_symbol"],
            "last_tick_sequence_by_symbol": chunk[
                "last_tick_sequence_by_symbol"
            ],
            "last_tick_transport_epoch_by_symbol": chunk[
                "last_tick_transport_epoch_by_symbol"
            ],
            "last_tick_snapshot_sha256_by_symbol": chunk[
                "last_tick_snapshot_sha256_by_symbol"
            ],
        }
        entry["manifest_entry_sha256"] = handoff.canonical_sha256(entry)
        previous = entry["manifest_entry_sha256"]
        manifest_lines.append(handoff.canonical_json_bytes(entry) + b"\n")
    (root / handoff.MANIFEST_FILENAME).write_bytes(b"".join(manifest_lines))
    return root


@pytest.fixture
def sealed_capture(tmp_path: Path) -> tuple[Path, Path]:
    prereg = _write_preregistration(tmp_path / "sealed")
    capture = _write_capture(tmp_path / "capture", prereg)
    return prereg, capture


def test_refuses_before_end_without_accessing_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prereg = _write_preregistration(tmp_path / "sealed")
    capture = tmp_path / "capture-must-not-be-opened"
    monkeypatch.setattr(handoff.time, "time", lambda: END.timestamp() - 1.0)
    with pytest.raises(evaluator.EvaluationRefusal, match="window_not_closed"):
        evaluator.evaluate_capture(
            preregistration_path=prereg,
            capture_root=capture,
            output_root=tmp_path / "output",
        )
    assert not capture.exists()
    assert not (tmp_path / "output").exists()


def test_tampered_capture_refuses_before_screen(
    sealed_capture: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prereg, capture = sealed_capture
    chunk = next((capture / "chunks").glob("*/*.json"))
    chunk.write_bytes(chunk.read_bytes() + b" ")
    monkeypatch.setattr(
        screen,
        "screen_universe",
        lambda **_kwargs: pytest.fail("screen must not run on tampered capture"),
    )
    with pytest.raises(evaluator.EvaluationRefusal, match="hash_or_size"):
        evaluator.evaluate_capture(
            preregistration_path=prereg,
            capture_root=capture,
            output_root=tmp_path / "output",
        )


def test_verified_failure_evaluates_screen_once_and_stays_authority_free(
    sealed_capture: tuple[Path, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prereg, capture = sealed_capture
    original_verify = handoff.verify_capture_handoff
    original_screen = screen.screen_universe
    events: list[str] = []

    def verified(**kwargs: Any) -> dict[str, Any]:
        result = original_verify(**kwargs)
        events.append("verified")
        return result

    def screened(**kwargs: Any) -> dict[str, Any]:
        assert events == ["verified"]
        events.append("screened")
        return original_screen(**kwargs)

    monkeypatch.setattr(handoff, "verify_capture_handoff", verified)
    monkeypatch.setattr(screen, "screen_universe", screened)
    result = evaluator.evaluate_capture(
        preregistration_path=prereg,
        capture_root=capture,
        output_root=tmp_path / "output",
    )
    assert events == ["verified", "screened"]
    assert result["all_fixed_research_gates_pass"] is False
    assert result["research_only"] is True
    assert not any(result["authority"].values())
    bundle = Path(result["bundle_path"])
    summary = json.loads(next(bundle.glob("mtvclc_gate_summary_*.json")).read_text())
    assert summary["execution_contract"] == {
        "entry_type": "immediate_market",
        "pending_orders_forbidden": True,
        "sides": ["BUY", "SELL"],
    }
    assert summary["all_fixed_research_gates_pass"] is False


def _passing_result(
    calibrations: Mapping[str, screen.MT4CostCalibration],
    source_hashes: Mapping[str, str],
) -> dict[str, Any]:
    outcomes: list[dict[str, Any]] = []
    reservations: list[dict[str, Any]] = []
    base_epoch = int(T0.timestamp())
    for symbol in screen.MTVCLC_SYMBOLS:
        for side_index, side in enumerate(("BUY", "SELL")):
            for day_index in range(30):
                day_offset = side_index * 30 + day_index
                epoch = base_epoch + day_offset * 86_400 + 60
                day = datetime.fromtimestamp(epoch, tz=UTC).date().isoformat()
                common = {
                    "config_id": screen.CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "signal_epoch": epoch,
                    "entry_day": day,
                }
                reservations.append(
                    {
                        **common,
                        "signal_index": day_offset + screen.BASELINE_M1_BARS,
                        "expected_entry_epoch": epoch + 60,
                        "volume_v90": 10.0,
                        "signal_tick_volume": 20,
                        "bid_body_bps": 5.0,
                        "bid_close_location": 0.9,
                        "p90_spread_bps": calibrations[symbol].p90_spread_bps,
                        "recorded_cost_bps": calibrations[
                            symbol
                        ].recorded_cost_bps,
                        "convert_on_close_charge_fraction": calibrations[
                            symbol
                        ].convert_on_close_charge_fraction,
                        "target_bps": 16.0,
                        "stop_bps": 32.0,
                        "p_star": calibrations[
                            symbol
                        ].break_even_win_probability,
                        "entry_status": "admitted",
                    }
                )
                outcomes.append(
                    {
                        **common,
                        "entry_epoch": epoch + 60,
                        "exit_epoch": epoch + 62,
                        "entry_price": 1.0,
                        "exit_price": 1.0016,
                        "exit_reason": "TAKE_PROFIT",
                        "full_target_hit_first": True,
                        "gross_quote_bps": (
                            screen.TARGET_COST_MULTIPLE
                            * calibrations[symbol].recorded_cost_bps
                        ),
                        "recorded_cost_bps": calibrations[
                            symbol
                        ].recorded_cost_bps,
                        "currency_conversion_debit_bps": (
                            screen.TARGET_COST_MULTIPLE
                            * calibrations[symbol].recorded_cost_bps
                            * calibrations[
                                symbol
                            ].convert_on_close_charge_fraction
                        ),
                        "net_bps": (
                            screen.TARGET_COST_MULTIPLE
                            * calibrations[symbol].recorded_cost_bps
                            - calibrations[symbol].recorded_cost_bps
                            - screen.TARGET_COST_MULTIPLE
                            * calibrations[symbol].recorded_cost_bps
                            * calibrations[
                                symbol
                            ].convert_on_close_charge_fraction
                        ),
                    }
                )
    cells: list[dict[str, Any]] = []
    for symbol in screen.MTVCLC_SYMBOLS:
        for side in ("BUY", "SELL"):
            wilson = evaluator._wilson_lower(30, 30)
            p_star = calibrations[symbol].break_even_win_probability
            mean_net = (
                screen.TARGET_COST_MULTIPLE
                * calibrations[symbol].recorded_cost_bps
                - calibrations[symbol].recorded_cost_bps
                - screen.TARGET_COST_MULTIPLE
                * calibrations[symbol].recorded_cost_bps
                * calibrations[symbol].convert_on_close_charge_fraction
            )
            assert wilson > p_star
            cells.append(
                {
                    "config_id": screen.CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "source_ready": True,
                    "reservations": 30,
                    "wins": 30,
                    "independent_days": 30,
                    "full_target_rate": 1.0,
                    "win_probability_wilson_lower": wilson,
                    "base_break_even_probability": p_star,
                    "mean_net_bps": mean_net,
                    "passes_fixed_cell_screen": True,
                }
            )
    return {
        "schema_version": "fxstack.scalp.mtvclc_screen_result.v1",
        "strategy_id": screen.STRATEGY_ID,
        "strategy_version": screen.STRATEGY_VERSION,
        "config_ids": [screen.CONFIG_ID],
        "symbol_scope": list(screen.MTVCLC_SYMBOLS),
        "source_contract_id": screen.SOURCE_CONTRACT_ID,
        "activity_metric_id": screen.ACTIVITY_METRIC_ID,
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_654,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_698,
        },
        "source_scope_ready": True,
        "source_sha256_by_symbol": dict(source_hashes),
        "source_errors": [],
        "costs": {
            symbol: asdict(calibrations[symbol])
            for symbol in screen.MTVCLC_SYMBOLS
        },
        "cells": cells,
        "reservation_ledger": reservations,
        "outcome_ledger": outcomes,
        "all_cells_pass_fixed_screen": True,
        "attempt_manifest": screen.attempt_manifest(),
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "order_authorized": False,
    }


def test_pass_gate_requires_all_cells_and_panel_gates() -> None:
    prereg = evaluator._validate_preregistration_for_screen(
        _preregistration_body()
    )
    source_hashes = {symbol: "e" * 64 for symbol in screen.MTVCLC_SYMBOLS}
    result = _passing_result(prereg.calibrations, source_hashes)
    summary = evaluator._validate_and_summarize_result(
        result=result,
        calibrations=prereg.calibrations,
        source_hashes=source_hashes,
    )
    assert summary["all_fixed_research_gates_pass"] is True
    assert summary["fixed_cell_gates"]["all_44_cells_pass"] is True
    assert summary["panel_gates"]["total_trades"] == 1_320
    assert summary["panel_gates"]["independent_utc_days"] == 60

    result["cells"][0]["passes_fixed_cell_screen"] = False
    result["all_cells_pass_fixed_screen"] = False
    with pytest.raises(evaluator.EvaluationRefusal, match="gate_mismatch"):
        evaluator._validate_and_summarize_result(
            result=result,
            calibrations=prereg.calibrations,
            source_hashes=source_hashes,
        )

    forged = _passing_result(prereg.calibrations, source_hashes)
    forged["outcome_ledger"][0]["full_target_hit_first"] = False
    forged["outcome_ledger"][0]["exit_reason"] = "STOP_LOSS"
    forged["outcome_ledger"][0]["gross_quote_bps"] = -32.0
    forged["outcome_ledger"][0]["currency_conversion_debit_bps"] = 0.0
    forged["outcome_ledger"][0]["net_bps"] = -36.0
    with pytest.raises(evaluator.EvaluationRefusal, match="gate_mismatch"):
        evaluator._validate_and_summarize_result(
            result=forged,
            calibrations=prereg.calibrations,
            source_hashes=source_hashes,
        )


def test_cost_rows_preserve_conversion_and_refuse_drift() -> None:
    payload = _preregistration_body()
    prereg = evaluator._validate_preregistration_for_screen(payload)
    assert prereg.calibrations["EURUSD"].conversion_applies is False
    assert prereg.calibrations["USDJPY"].conversion_applies is True
    assert prereg.calibrations["USDJPY"].convert_on_close_charge_fraction == 0.005
    assert prereg.calibrations["USDJPY"].break_even_win_probability > 0.75

    payload["cost_policy"]["symbols"]["USDJPY"][
        "pre_conversion_geometry_cost_bps"
    ] += 0.01
    with pytest.raises(evaluator.EvaluationRefusal, match="cost_row_drift"):
        evaluator._validate_preregistration_for_screen(payload)


def test_evaluation_bundle_is_deterministic(
    sealed_capture: tuple[Path, Path], tmp_path: Path
) -> None:
    prereg, capture = sealed_capture
    first = evaluator.evaluate_capture(
        preregistration_path=prereg,
        capture_root=capture,
        output_root=tmp_path / "one",
    )
    second = evaluator.evaluate_capture(
        preregistration_path=prereg,
        capture_root=capture,
        output_root=tmp_path / "two",
    )
    first_root = Path(first["bundle_path"])
    second_root = Path(second["bundle_path"])
    assert first_root.name == second_root.name
    first_files = {path.name: path.read_bytes() for path in first_root.iterdir()}
    second_files = {path.name: path.read_bytes() for path in second_root.iterdir()}
    assert first_files == second_files


def test_source_has_no_external_or_authority_import_surface() -> None:
    source = Path(evaluator.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        str(node.module or "")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    forbidden = {
        "requests",
        "urllib",
        "socket",
        "sqlite3",
        "sqlalchemy",
        "psycopg",
        "cryptography",
    }
    assert not any(
        name == item or name.startswith(f"{item}.")
        for name in imports
        for item in forbidden
    )
    assert "external_scalp_validation_release" not in source
    assert "screen_universe(" in source
    assert "pending_orders_forbidden" in source
