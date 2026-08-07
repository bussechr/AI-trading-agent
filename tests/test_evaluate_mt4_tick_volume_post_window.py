from __future__ import annotations

from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Any, cast

import pytest

from tools import evaluate_mt4_tick_volume_post_window as evaluator
from tools import verify_mt4_tick_volume_capture_handoff as handoff


HELPER_PATH = Path(__file__).with_name("test_verify_mt4_tick_volume_capture_handoff.py")
HELPER_SPEC = importlib.util.spec_from_file_location(
    "mtvclc_handoff_test_helpers",
    HELPER_PATH,
)
assert HELPER_SPEC is not None and HELPER_SPEC.loader is not None
helpers = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(helpers)

T0: datetime = helpers.T0
END: datetime = helpers.END


def _identity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _cost_policy() -> tuple[dict[str, Any], dict[str, Any]]:
    cost_source = {
        "filename": "ig_mt4_bid_ask_capture.json",
        "sha256": "a" * 64,
        "size_bytes": 123,
    }
    rows: dict[str, dict[str, Any]] = {}
    for symbol in handoff.SYMBOLS:
        pnl_currency = symbol[3:]
        account_currency = "USD"
        conversion_applies = pnl_currency != account_currency
        screen_fraction = 0.005 if conversion_applies else 0.0
        calibration = evaluator.screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=3.0,
            commission_bps_per_round_trip=0.0,
            financing_bps_per_trade=0.0,
            account_currency=account_currency,
            pnl_currency=pnl_currency,
            convert_on_close_charge_fraction=screen_fraction,
            source_sha256=cost_source["sha256"],
        )
        rows[symbol] = {
            "p90_ig_spread_bps": 3.0,
            "commission_bps_per_round_trip": 0.0,
            "financing_bps_per_trade": 0.0,
            "fixed_adverse_execution_debit_bps": 1.0,
            "pre_conversion_geometry_cost_bps": 4.0,
            "profit_loss_currency": pnl_currency,
            "account_currency": account_currency,
            "conversion_rate_of_absolute_profit_or_loss": 0.005,
            "convert_on_close_charge_fraction_for_screen": screen_fraction,
            "conversion_applies": conversion_applies,
            "conversion_adjusted_break_even_win_probability": (
                calibration.break_even_win_probability
            ),
            "commission_status": "explicit_source_attested",
            "financing_status": ("structurally_avoided_by_fixed_rollover_guard"),
            "conversion_status": (
                "debit_absolute_profit_or_loss_when_account_currency_differs"
            ),
        }
    policy = {
        "formula": evaluator.EXPECTED_COST_POLICY["formula"],
        "conversion_treatment": evaluator.EXPECTED_COST_POLICY["conversion_treatment"],
        "geometry_uses_pre_conversion_cost": True,
        "final_cell_mean_uses_conversion_adjusted_net": True,
        "unknown_commission_financing_or_conversion_refuses_evaluation": True,
        "fee_schedule_change_or_source_uncertainty_refuses_evaluation": True,
        "symbols": rows,
    }
    identity = {
        "capture_json": cost_source,
        "capture_npz": {
            "filename": "ig_mt4_bid_ask_samples.npz",
            "sha256": "b" * 64,
            "size_bytes": 456,
        },
        "capture_payload_sha256": "c" * 64,
        "capture_mode": "authenticated_same_source_db_history",
        "scope_version": handoff.SCOPE_VERSION,
        "venue_id": handoff.VENUE_ID,
    }
    return policy, identity


def _write_evaluator_preregistration(root: Path) -> Path:
    original = helpers._write_preregistration(root)
    payload = json.loads(original.read_text(encoding="utf-8"))
    body = {
        key: value
        for key, value in payload.items()
        if key != "preregistration_body_sha256"
    }
    policy, cost_identity = _cost_policy()
    body["cost_policy"] = policy
    body["source_identities"]["cost_capture"] = cost_identity
    body["source_identities"]["fee_attestation"] = {"account_currency": "USD"}
    body["source_identities"]["screen_source"] = _identity(evaluator.SCREEN_PATH)
    attempt_manifest = evaluator.screen.attempt_manifest()
    body["strategy"]["attempt_manifest"] = attempt_manifest
    body["strategy"]["attempt_manifest_sha256"] = evaluator.canonical_sha256(
        attempt_manifest
    )
    body["attempt_accounting"] = {
        "prior_attempted_cells_lower_bound": (
            evaluator.screen.IMMUTABLE_PRIOR_ATTEMPTED_CELLS
        ),
        "current_attempted_cells": (evaluator.screen.IMMUTABLE_CURRENT_ATTEMPTED_CELLS),
        "cumulative_attempted_cells_lower_bound": (
            evaluator.screen.IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
        ),
    }
    digest = handoff.canonical_sha256(body)
    target = root / f"mtvclc_v1_preregistration_{digest}.json"
    target.write_bytes(
        handoff.canonical_json_bytes({**body, "preregistration_body_sha256": digest})
    )
    return target


def _freeze_capture_snapshot(capture_root: Path) -> None:
    paths = list(capture_root.rglob("*"))
    for path in paths:
        if path.is_file():
            path.chmod(stat.S_IREAD)
    for path in sorted(
        (item for item in paths if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        path.chmod(stat.S_IREAD | stat.S_IEXEC)
    capture_root.chmod(stat.S_IREAD | stat.S_IEXEC)


def _thaw_capture_snapshot(capture_root: Path) -> None:
    capture_root.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    for path in capture_root.rglob("*"):
        if path.is_dir():
            path.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        elif path.is_file():
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def _closed_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    preregistration = _write_evaluator_preregistration(tmp_path)
    capture_root = helpers._write_capture(tmp_path, preregistration)
    verified = handoff.verify_capture_handoff(
        preregistration_path=preregistration,
        capture_root=capture_root,
        now_epoch=END.timestamp(),
    )
    handoff_root = tmp_path / "handoff"
    handoff_path = handoff.publish_handoff(
        output_root=handoff_root,
        handoff=verified,
    )
    _freeze_capture_snapshot(capture_root)
    return preregistration, capture_root, handoff_path


def _closed_replacement_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    preregistration = helpers._write_replacement_preregistration(tmp_path)
    capture_root = helpers._write_capture(tmp_path, preregistration)
    verified = handoff.verify_capture_handoff(
        preregistration_path=preregistration,
        capture_root=capture_root,
        now_epoch=END.timestamp(),
    )
    handoff_root = tmp_path / "handoff"
    handoff_path = handoff.publish_handoff(
        output_root=handoff_root,
        handoff=verified,
    )
    _freeze_capture_snapshot(capture_root)
    return preregistration, capture_root, handoff_path


def _write_rehashed_handoff(root: Path, payload: dict[str, Any]) -> Path:
    payload["capture_inventory_sha256"] = evaluator.canonical_sha256(
        payload["capture_inventory"]
    )
    body = dict(payload)
    body.pop("handoff_body_sha256", None)
    payload["handoff_body_sha256"] = evaluator.canonical_sha256(body)
    target = root / (f"mtvclc_capture_handoff_{payload['handoff_body_sha256']}.json")
    target.write_bytes(evaluator.canonical_json_bytes(payload) + b"\n")
    return target


@pytest.mark.parametrize(
    ("scope", "field", "value"),
    (
        ("policy", "formula", "drifted-formula"),
        ("policy", "geometry_uses_pre_conversion_cost", 1),
        ("row", "commission_status", "unknown"),
        ("row", "financing_status", "conservative_upper_bound"),
        ("row", "conversion_status", "unknown"),
    ),
)
def test_exact_sealed_cost_policy_and_statuses_are_required(
    scope: str,
    field: str,
    value: Any,
) -> None:
    policy, cost_identity = _cost_policy()
    if scope == "policy":
        policy[field] = value
    else:
        policy["symbols"]["EURUSD"][field] = value
    preregistration = {
        "cost_policy": policy,
        "source_identities": {
            "cost_capture": cost_identity,
            "fee_attestation": {"account_currency": "USD"},
        },
    }

    with pytest.raises(evaluator.EvaluationRefusal, match="sealed_cost"):
        evaluator._load_costs(preregistration)


class _CapturePathBomb:
    def __fspath__(self) -> str:
        raise AssertionError("capture path was touched before the sealed end")


def test_early_refusal_never_accesses_capture_or_handoff(tmp_path: Path) -> None:
    preregistration = helpers._write_preregistration(tmp_path)
    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="prospective_window_not_closed",
    ):
        evaluator.evaluate_post_window(
            preregistration_path=preregistration,
            handoff_path=tmp_path / "must-not-be-opened.json",
            capture_root=cast(Path, _CapturePathBomb()),
            staging_root=tmp_path,
            output_root=tmp_path / "output",
            now_epoch=END.timestamp() - 1.0,
        )


def test_executed_source_hash_binds_the_bytes_executed_not_later_path(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mutable_screen.py"
    trusted = b'VALUE = "trusted"\n'
    source.write_bytes(trusted)
    image = evaluator._read_executed_source(source)
    module_name = "mtvclc_test_immutable_source_image"
    module = evaluator._execute_source_image(image, module_name=module_name)
    try:
        source.write_bytes(b'VALUE = "attacker"\n')
        sealed_identity = {
            "filename": source.name,
            "sha256": hashlib.sha256(trusted).hexdigest(),
            "size_bytes": len(trusted),
        }

        assert module.VALUE == "trusted"
        assert image.raw == trusted
        assert evaluator._identity_matches_source_image(
            sealed_identity,
            image,
        )
        assert image.sha256 != hashlib.sha256(source.read_bytes()).hexdigest()
    finally:
        sys.modules.pop(module_name, None)


def test_replacement_screen_uses_exact_sealed_wrapper_and_support_bytes() -> None:
    replacement_image = evaluator._read_executed_source(
        evaluator.REPLACEMENT_SCREEN_PATH
    )
    replacement = evaluator._execute_replacement_screen(replacement_image)
    manifest = replacement.attempt_manifest()
    preregistration = {
        "source_identities": {
            "screen_source": replacement_image.identity(),
            "screen_support_source": (evaluator.BASE_SCREEN_SOURCE_IMAGE.identity()),
        },
        "strategy": {
            "attempt_manifest": manifest,
            "attempt_manifest_sha256": evaluator.canonical_sha256(manifest),
        },
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_742,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_786,
        },
    }

    selected, selected_image, support_image = evaluator._select_executed_screen(
        preregistration
    )

    assert selected.IMMUTABLE_PRIOR_ATTEMPTED_CELLS == 4_742
    assert selected.IMMUTABLE_CURRENT_ATTEMPTED_CELLS == 44
    assert selected.IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS == 4_786
    assert selected.attempt_manifest() == manifest
    assert selected_image.sha256 == replacement_image.sha256
    assert support_image.sha256 == (evaluator.BASE_SCREEN_SOURCE_IMAGE.sha256)


def test_replacement_screen_refuses_unsealed_support_bytes() -> None:
    replacement_image = evaluator._read_executed_source(
        evaluator.REPLACEMENT_SCREEN_PATH
    )
    replacement = evaluator._execute_replacement_screen(replacement_image)
    manifest = replacement.attempt_manifest()
    preregistration = {
        "source_identities": {
            "screen_source": replacement_image.identity(),
            "screen_support_source": {
                **evaluator.BASE_SCREEN_SOURCE_IMAGE.identity(),
                "sha256": "f" * 64,
            },
        },
        "strategy": {
            "attempt_manifest": manifest,
            "attempt_manifest_sha256": evaluator.canonical_sha256(manifest),
        },
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_742,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_786,
        },
    }

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="frozen_screen_support_identity_mismatch",
    ):
        evaluator._select_executed_screen(preregistration)


def _write_bars(path: Path, bars: list[Any]) -> None:
    with path.open("wb") as handle:
        for bar in bars:
            handle.write(
                evaluator.BAR_RECORD.pack(
                    bar.epoch,
                    bar.bid_open,
                    bar.bid_high,
                    bar.bid_low,
                    bar.bid_close,
                    bar.tick_volume,
                )
            )


def _write_quotes(path: Path, quotes: list[Any]) -> None:
    with path.open("wb") as handle:
        for sequence, quote in enumerate(quotes, start=1):
            handle.write(
                evaluator.QUOTE_RECORD.pack(
                    sequence,
                    quote.epoch,
                    quote.bid,
                    quote.ask,
                    quote.market_event_sequence,
                    bytes.fromhex(quote.source_event_token_sha256),
                )
            )


def _calibration(symbol: str = "EURUSD") -> Any:
    return evaluator.screen.MT4CostCalibration(
        symbol=symbol,
        p90_spread_bps=3.0,
        commission_bps_per_round_trip=0.0,
        financing_bps_per_trade=0.0,
        account_currency="USD",
        pnl_currency=symbol[3:],
        convert_on_close_charge_fraction=(0.0 if symbol[3:] == "USD" else 0.005),
        source_sha256="a" * 64,
    )


def _bar(epoch: int, *, signal: bool = False) -> Any:
    if signal:
        return evaluator.screen.MT4BidBar(
            epoch=epoch,
            bid_open=1.0,
            bid_high=1.021,
            bid_low=0.999,
            bid_close=1.020,
            tick_volume=100,
        )
    return evaluator.screen.MT4BidBar(
        epoch=epoch,
        bid_open=1.0,
        bid_high=1.001,
        bid_low=0.999,
        bid_close=1.0,
        tick_volume=1,
    )


def _bars_with_signals(
    first_signal_epoch: int,
    signal_offsets_minutes: tuple[int, ...],
    *,
    trailing_minutes: int = 1,
) -> list[Any]:
    bars = [_bar(first_signal_epoch - (240 - index) * 60) for index in range(240)]
    final_offset = max(signal_offsets_minutes, default=0) + trailing_minutes
    signals = set(signal_offsets_minutes)
    bars.extend(
        _bar(
            first_signal_epoch + offset * 60,
            signal=offset in signals,
        )
        for offset in range(final_offset + 1)
    )
    return bars


def _quote(
    epoch: int,
    *,
    bid: float = 1.0,
    ask: float = 1.0002,
    sequence: int = 1,
) -> Any:
    return evaluator.screen.MT4Quote(
        epoch=epoch,
        bid=bid,
        ask=ask,
        source_event_token_sha256=hashlib.sha256(
            f"quote:{sequence}".encode("ascii")
        ).hexdigest(),
        market_event_sequence=sequence,
    )


def _frozen_reference_symbol(
    *,
    symbol: str,
    bars: list[Any],
    quotes: list[Any],
    cost: Any,
    t0_epoch: int,
    end_epoch_exclusive: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Frozen screen loop with only the sealed-window outer boundary added."""

    prepared_bars = evaluator.screen.prepare_bars(bars)
    prepared_quotes = evaluator.screen.prepare_quotes(quotes)
    reservations: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    reserved_days: set[str] = set()
    for signal_index in range(evaluator.screen.BASELINE_M1_BARS, len(bars)):
        bar = prepared_bars[signal_index]
        if (
            not t0_epoch <= bar.epoch < end_epoch_exclusive
            or bar.epoch + 60 >= end_epoch_exclusive
            or bar.bid_close == bar.bid_open
        ):
            continue
        side = "BUY" if bar.bid_close > bar.bid_open else "SELL"
        closed, _reason = evaluator.screen.evaluate_closed_signal(
            prepared=prepared_bars,
            signal_index=signal_index,
            symbol=symbol,
            side=side,
            cost=cost,
        )
        if closed is None or closed.entry_day in reserved_days:
            continue
        signal, entry_reason = evaluator.screen.attach_entry(
            closed=closed,
            quotes=prepared_quotes,
        )
        if entry_reason == "live_spread_above_frozen_p90":
            continue
        reserved_days.add(closed.entry_day)
        reservations.append(
            {**evaluator.asdict(closed), "entry_status": entry_reason or "admitted"}
        )
        outcome = (
            evaluator.screen.adverse_missing_entry_outcome(closed)
            if signal is None
            else evaluator.screen.score_signal(signal, quotes=prepared_quotes)
        )
        outcomes.append(evaluator.asdict(outcome))
    return reservations, outcomes


def _evaluate_and_compare(
    tmp_path: Path,
    *,
    bars: list[Any],
    quotes: list[Any],
    t0_epoch: int,
    end_epoch_exclusive: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bar_path = tmp_path / "EURUSD.bars.bin"
    quote_path = tmp_path / "EURUSD.quotes.bin"
    _write_bars(bar_path, bars)
    _write_quotes(quote_path, quotes)
    cost = _calibration()
    actual = evaluator.evaluate_materialized_symbol(
        symbol="EURUSD",
        bar_path=bar_path,
        quote_path=quote_path,
        cost=cost,
        t0_epoch=float(t0_epoch),
        end_epoch_exclusive=float(end_epoch_exclusive),
    )
    expected = _frozen_reference_symbol(
        symbol="EURUSD",
        bars=bars,
        quotes=quotes,
        cost=cost,
        t0_epoch=t0_epoch,
        end_epoch_exclusive=end_epoch_exclusive,
    )
    assert actual == expected
    return actual


def test_pre_t0_signal_bar_is_context_only(tmp_path: Path) -> None:
    t0 = int(T0.timestamp())
    bars = [_bar(t0 - (241 - index) * 60) for index in range(240)]
    bars.append(_bar(t0 - 60, signal=True))
    bars.append(_bar(t0))
    path = tmp_path / "EURUSD.bars.bin"
    _write_bars(path, bars)

    closed = list(
        evaluator.iter_post_t0_closed_signals(
            bar_path=path,
            symbol="EURUSD",
            cost=_calibration(),
            t0_epoch=float(t0),
            end_epoch_exclusive=float(t0 + 3600),
        )
    )

    assert closed == []


def test_streaming_symbol_evaluator_matches_frozen_screen(tmp_path: Path) -> None:
    t0 = int(T0.timestamp())
    bars = [_bar(t0 - (240 - index) * 60) for index in range(240)]
    bars.extend((_bar(t0, signal=True), _bar(t0 + 60)))
    token = "1" * 64
    quotes = [
        evaluator.screen.MT4Quote(
            epoch=t0 + 60 + offset,
            bid=(1.003 if offset >= 5 else 1.0),
            ask=(1.0032 if offset >= 5 else 1.0002),
            source_event_token_sha256=token,
            market_event_sequence=index,
        )
        for index, offset in enumerate(range(0, 1806, 5), start=1)
    ]
    bar_path = tmp_path / "EURUSD.bars.bin"
    quote_path = tmp_path / "EURUSD.quotes.bin"
    _write_bars(bar_path, bars)
    _write_quotes(quote_path, quotes)
    cost = _calibration()

    reservations, outcomes = evaluator.evaluate_materialized_symbol(
        symbol="EURUSD",
        bar_path=bar_path,
        quote_path=quote_path,
        cost=cost,
        t0_epoch=float(t0),
        end_epoch_exclusive=float(t0 + 7200),
    )

    prepared_bars = evaluator.screen.prepare_bars(bars)
    prepared_quotes = evaluator.screen.prepare_quotes(quotes)
    closed, reason = evaluator.screen.evaluate_closed_signal(
        prepared=prepared_bars,
        signal_index=240,
        symbol="EURUSD",
        side="BUY",
        cost=cost,
    )
    assert closed is not None, reason
    signal, reason = evaluator.screen.attach_entry(
        closed=closed,
        quotes=prepared_quotes,
    )
    assert signal is not None, reason
    expected_reservation = {**evaluator.asdict(closed), "entry_status": "admitted"}
    expected_outcome = evaluator.asdict(
        evaluator.screen.score_signal(signal, quotes=prepared_quotes)
    )
    assert reservations == [expected_reservation]
    assert outcomes == [expected_outcome]


def test_wide_spread_candidate_does_not_reserve_symbol_day(
    tmp_path: Path,
) -> None:
    t0 = int(T0.timestamp())
    bars = _bars_with_signals(t0, (0, 2, 4), trailing_minutes=5)
    quotes = [
        _quote(t0 + 60, ask=1.001, sequence=1),
        _quote(t0 + 180, sequence=2),
        _quote(t0 + 185, bid=1.003, ask=1.0032, sequence=3),
    ]

    reservations, outcomes = _evaluate_and_compare(
        tmp_path,
        bars=bars,
        quotes=quotes,
        t0_epoch=t0,
        end_epoch_exclusive=t0 + 3600,
    )

    assert len(reservations) == len(outcomes) == 1
    assert reservations[0]["signal_epoch"] == t0 + 120
    assert reservations[0]["entry_status"] == "admitted"
    assert outcomes[0]["exit_reason"] == "TAKE_PROFIT"


def test_missing_entry_quote_reserves_day_and_scores_adverse(
    tmp_path: Path,
) -> None:
    t0 = int(T0.timestamp())
    bars = _bars_with_signals(t0, (0, 2), trailing_minutes=3)
    quotes = [_quote(t0 + 66, sequence=1)]

    reservations, outcomes = _evaluate_and_compare(
        tmp_path,
        bars=bars,
        quotes=quotes,
        t0_epoch=t0,
        end_epoch_exclusive=t0 + 3600,
    )

    assert len(reservations) == len(outcomes) == 1
    assert reservations[0]["signal_epoch"] == t0
    assert reservations[0]["entry_status"] == ("contemporaneous_entry_quote_missing")
    assert outcomes[0]["exit_reason"] == "ENTRY_QUOTE_MISSING"
    assert outcomes[0]["full_target_hit_first"] is False


@pytest.mark.parametrize(
    ("second_offset", "second_bid", "second_ask", "expected_reason"),
    (
        (6, 1.0, 1.0002, "QUOTE_GAP_ADVERSE"),
        (5, 0.996, 0.9962, "STOP_LOSS"),
        (5, 1.003, 1.0032, "TAKE_PROFIT"),
        (None, None, None, "INCOMPLETE_HORIZON_ADVERSE"),
    ),
)
def test_streamed_outcome_exit_parity(
    tmp_path: Path,
    second_offset: int | None,
    second_bid: float | None,
    second_ask: float | None,
    expected_reason: str,
) -> None:
    t0 = int(T0.timestamp())
    bars = _bars_with_signals(t0, (0,))
    entry = t0 + 60
    quotes = [_quote(entry, sequence=1)]
    if second_offset is not None:
        assert second_bid is not None and second_ask is not None
        quotes.append(
            _quote(
                entry + second_offset,
                bid=second_bid,
                ask=second_ask,
                sequence=2,
            )
        )

    _reservations, outcomes = _evaluate_and_compare(
        tmp_path,
        bars=bars,
        quotes=quotes,
        t0_epoch=t0,
        end_epoch_exclusive=t0 + 3600,
    )

    assert [row["exit_reason"] for row in outcomes] == [expected_reason]


def test_overlapping_symbol_outcomes_match_frozen_screen(tmp_path: Path) -> None:
    first_signal = int(T0.timestamp()) + 23 * 3600 + 58 * 60
    bars = _bars_with_signals(first_signal, (0, 2), trailing_minutes=3)
    first_entry = first_signal + 60
    second_entry = first_signal + 180
    quote_epochs = list(range(first_entry, second_entry + 6, 5))
    quotes = [
        _quote(
            epoch,
            bid=1.003 if epoch == quote_epochs[-1] else 1.0,
            ask=1.0032 if epoch == quote_epochs[-1] else 1.0002,
            sequence=index,
        )
        for index, epoch in enumerate(quote_epochs, start=1)
    ]

    reservations, outcomes = _evaluate_and_compare(
        tmp_path,
        bars=bars,
        quotes=quotes,
        t0_epoch=first_signal,
        end_epoch_exclusive=first_signal + 3600,
    )

    assert len(reservations) == len(outcomes) == 2
    assert len({row["entry_day"] for row in outcomes}) == 2
    assert [row["exit_reason"] for row in outcomes] == [
        "TAKE_PROFIT",
        "TAKE_PROFIT",
    ]
    assert outcomes[0]["exit_epoch"] == outcomes[1]["exit_epoch"]


def test_entry_at_exclusive_end_is_never_a_trial(tmp_path: Path) -> None:
    signal_epoch = int(END.timestamp()) - 60
    bars = _bars_with_signals(signal_epoch, (0,))

    reservations, outcomes = _evaluate_and_compare(
        tmp_path,
        bars=bars,
        quotes=[],
        t0_epoch=signal_epoch,
        end_epoch_exclusive=int(END.timestamp()),
    )

    assert reservations == []
    assert outcomes == []


def test_global_trade_and_day_gates_are_independently_enforced() -> None:
    outcomes = (
        [{"entry_day": f"2020-01-{index % 30 + 1:02d}"} for index in range(150)]
        + [{"entry_day": f"2020-02-{index % 29 + 1:02d}"} for index in range(145)]
        + [{"entry_day": "2020-03-01"} for _index in range(5)]
    )
    result = {
        "outcome_ledger": outcomes,
        "all_cells_pass_fixed_screen": True,
        "source_scope_ready": True,
        "source_errors": [],
    }
    gates = evaluator.enforce_global_gates(result)
    assert gates["total_trades"] == 300
    assert gates["total_independent_utc_days"] == 60
    assert gates["all_preregistered_success_gates_pass"] is True

    result["outcome_ledger"] = outcomes[:-1]
    assert (
        evaluator.enforce_global_gates(result)["all_preregistered_success_gates_pass"]
        is False
    )


def test_handoff_tamper_refuses_before_artifact_publication(tmp_path: Path) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    handoff_path.chmod(stat.S_IREAD | stat.S_IWRITE)
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    payload["capture_inventory"]["quote_rows"] += 1
    handoff_path.write_text(json.dumps(payload), encoding="utf-8")
    staging = tmp_path / "staging"
    staging.mkdir()

    with pytest.raises(evaluator.EvaluationRefusal, match="handoff"):
        evaluator.evaluate_post_window(
            preregistration_path=preregistration,
            handoff_path=handoff_path,
            capture_root=capture_root,
            staging_root=staging,
            output_root=tmp_path / "output",
            now_epoch=END.timestamp(),
        )
    assert not (tmp_path / "output").exists()


def test_writable_capture_refuses_before_handoff_or_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    capture_root.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    staging = tmp_path / "staging"
    staging.mkdir()
    handoff_called = False

    def unexpected_handoff(**_kwargs: Any) -> dict[str, Any]:
        nonlocal handoff_called
        handoff_called = True
        raise AssertionError("writable capture reached handoff verification")

    monkeypatch.setattr(
        evaluator.handoff,
        "verify_capture_handoff",
        unexpected_handoff,
    )
    try:
        with pytest.raises(
            evaluator.EvaluationRefusal,
            match="capture_snapshot_root_not_read_only",
        ):
            evaluator.evaluate_post_window(
                preregistration_path=preregistration,
                handoff_path=handoff_path,
                capture_root=capture_root,
                staging_root=staging,
                output_root=tmp_path / "output",
                now_epoch=END.timestamp(),
            )
        assert handoff_called is False
        assert not (tmp_path / "output").exists()
    finally:
        _freeze_capture_snapshot(capture_root)


def test_unfinalized_active_hour_journal_refuses_snapshot(tmp_path: Path) -> None:
    _preregistration, capture_root, _handoff_path = _closed_fixture(tmp_path)
    _thaw_capture_snapshot(capture_root)
    (capture_root / evaluator.ACTIVE_HOUR_JOURNAL_FILENAME).write_bytes(
        b'{"unfinished":true}\n'
    )
    _freeze_capture_snapshot(capture_root)

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_snapshot_active_hour_not_finalized",
    ):
        evaluator._require_stopped_read_only_capture_root(
            capture_root,
            require_data_writer_lock=False,
        )


def test_resilient_snapshot_requires_data_writer_lock_file(tmp_path: Path) -> None:
    _preregistration, capture_root, _handoff_path = _closed_fixture(tmp_path)

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_snapshot_data_writer_lock_missing",
    ):
        evaluator._require_stopped_read_only_capture_root(
            capture_root,
            require_data_writer_lock=True,
        )


def test_held_resilient_data_writer_lock_refuses_snapshot(tmp_path: Path) -> None:
    _preregistration, capture_root, _handoff_path = _closed_fixture(tmp_path)
    _thaw_capture_snapshot(capture_root)
    lock_path = capture_root / evaluator.DATA_WRITER_LOCK_FILENAME
    lock_path.write_bytes(b"\0")
    _freeze_capture_snapshot(capture_root)

    with lock_path.open("rb") as held:
        held.seek(0)
        if os.name == "nt":
            lock_api = __import__("msvcrt")
            lock_api.locking(held.fileno(), lock_api.LK_NBLCK, 1)
        else:
            lock_api = __import__("fcntl")
            lock_api.flock(
                held.fileno(),
                lock_api.LOCK_EX | lock_api.LOCK_NB,
            )
        try:
            with pytest.raises(
                evaluator.EvaluationRefusal,
                match="capture_snapshot_data_writer_still_active",
            ):
                evaluator._require_stopped_read_only_capture_root(
                    capture_root,
                    require_data_writer_lock=True,
                )
        finally:
            held.seek(0)
            if os.name == "nt":
                lock_api.locking(held.fileno(), lock_api.LK_UNLCK, 1)
            else:
                lock_api.flock(held.fileno(), lock_api.LOCK_UN)


def test_manifest_append_after_projection_is_caught_before_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    original_materialize = evaluator.materialize_verified_capture

    def append_after_projection(**kwargs: Any) -> Any:
        materialized = original_materialize(**kwargs)
        manifest = capture_root / handoff.MANIFEST_FILENAME
        _thaw_capture_snapshot(capture_root)
        with manifest.open("ab") as handle:
            handle.write(b"{}\n")
        _freeze_capture_snapshot(capture_root)
        return materialized

    monkeypatch.setattr(
        evaluator,
        "materialize_verified_capture",
        append_after_projection,
    )

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_snapshot_manifest_changed",
    ):
        evaluator.evaluate_post_window(
            preregistration_path=preregistration,
            handoff_path=handoff_path,
            capture_root=capture_root,
            staging_root=staging,
            output_root=tmp_path / "output",
            now_epoch=END.timestamp(),
        )
    assert not any(staging.iterdir())
    assert not (tmp_path / "output").exists()


def test_chunk_replacement_after_projection_is_caught_by_full_reverification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    original_materialize = evaluator.materialize_verified_capture
    chunk = next((capture_root / handoff.CHUNK_DIRECTORY).rglob("*.json"))

    def replace_after_projection(**kwargs: Any) -> Any:
        materialized = original_materialize(**kwargs)
        capture_root.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        chunks_root = capture_root / handoff.CHUNK_DIRECTORY
        chunks_root.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        chunk.parent.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
        chunk.chmod(stat.S_IREAD | stat.S_IWRITE)
        chunk.write_bytes(chunk.read_bytes() + b" ")
        chunk.chmod(stat.S_IREAD)
        chunk.parent.chmod(stat.S_IREAD | stat.S_IEXEC)
        chunks_root.chmod(stat.S_IREAD | stat.S_IEXEC)
        capture_root.chmod(stat.S_IREAD | stat.S_IEXEC)
        return materialized

    monkeypatch.setattr(
        evaluator,
        "materialize_verified_capture",
        replace_after_projection,
    )

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_snapshot_post_materialization_invalid",
    ):
        evaluator.evaluate_post_window(
            preregistration_path=preregistration,
            handoff_path=handoff_path,
            capture_root=capture_root,
            staging_root=staging,
            output_root=tmp_path / "output",
            now_epoch=END.timestamp(),
        )
    assert not any(staging.iterdir())
    assert not (tmp_path / "output").exists()


def test_manifest_append_after_outcomes_is_caught_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    original_screen = evaluator._screen_result_from_materialization

    def append_after_outcomes(**kwargs: Any) -> dict[str, Any]:
        result = original_screen(**kwargs)
        manifest = capture_root / handoff.MANIFEST_FILENAME
        _thaw_capture_snapshot(capture_root)
        with manifest.open("ab") as handle:
            handle.write(b"{}\n")
        _freeze_capture_snapshot(capture_root)
        return result

    monkeypatch.setattr(
        evaluator,
        "_screen_result_from_materialization",
        append_after_outcomes,
    )

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_snapshot_manifest_changed",
    ):
        evaluator.evaluate_post_window(
            preregistration_path=preregistration,
            handoff_path=handoff_path,
            capture_root=capture_root,
            staging_root=staging,
            output_root=tmp_path / "output",
            now_epoch=END.timestamp(),
        )
    assert not any(staging.iterdir())
    assert not (tmp_path / "output").exists()


def test_replacement_guard_mutation_after_outcomes_is_caught_before_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preregistration, capture_root, handoff_path = _closed_replacement_fixture(tmp_path)
    staging = tmp_path / "staging"
    staging.mkdir()
    original_screen = evaluator._screen_result_from_materialization

    def mutate_guard_after_outcomes(**kwargs: Any) -> dict[str, Any]:
        result = original_screen(**kwargs)
        guard_path = capture_root / handoff.GUARD_IDENTITY_FILENAME
        _thaw_capture_snapshot(capture_root)
        guard_path.write_bytes(guard_path.read_bytes() + b" ")
        _freeze_capture_snapshot(capture_root)
        return result

    monkeypatch.setattr(
        evaluator,
        "_screen_result_from_materialization",
        mutate_guard_after_outcomes,
    )

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_snapshot_guard_identity_changed",
    ):
        evaluator.evaluate_post_window(
            preregistration_path=preregistration,
            handoff_path=handoff_path,
            capture_root=capture_root,
            staging_root=staging,
            output_root=tmp_path / "output",
            now_epoch=END.timestamp(),
        )
    assert not any(staging.iterdir())
    assert not (tmp_path / "output").exists()


def test_validly_rehashed_handoff_cannot_change_preregistration_binding(
    tmp_path: Path,
) -> None:
    preregistration, _capture_root, handoff_path = _closed_fixture(tmp_path)
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    payload["capture_inventory"]["preregistration_body_sha256"] = "f" * 64
    payload["capture_inventory_sha256"] = evaluator.canonical_sha256(
        payload["capture_inventory"]
    )
    body = dict(payload)
    body.pop("handoff_body_sha256")
    payload["handoff_body_sha256"] = evaluator.canonical_sha256(body)
    rebound = tmp_path / (
        f"mtvclc_capture_handoff_{payload['handoff_body_sha256']}.json"
    )
    rebound.write_bytes(evaluator.canonical_json_bytes(payload) + b"\n")

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_handoff_contract_invalid",
    ):
        evaluator._load_handoff(
            rebound,
            binding=handoff.load_preregistration(preregistration),
        )


def test_base_handoff_inventory_rejects_replacement_guard_identity(
    tmp_path: Path,
) -> None:
    preregistration, _capture_root, handoff_path = _closed_fixture(tmp_path)
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    payload["capture_inventory"]["guard_identity_sha256"] = "a" * 64
    rebound = _write_rehashed_handoff(tmp_path, payload)

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_handoff_inventory_invalid",
    ):
        evaluator._load_handoff(
            rebound,
            binding=handoff.load_preregistration(preregistration),
            replacement_profile=False,
        )


def test_replacement_handoff_inventory_requires_exact_guard_topology(
    tmp_path: Path,
) -> None:
    preregistration, _capture_root, handoff_path = _closed_fixture(tmp_path)
    binding = handoff.load_preregistration(preregistration)

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_handoff_inventory_invalid",
    ):
        evaluator._load_handoff(
            handoff_path,
            binding=binding,
            replacement_profile=True,
        )

    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    payload["capture_inventory"]["guard_identity_sha256"] = "a" * 64
    rebound = _write_rehashed_handoff(tmp_path, payload)
    verified, _artifact_sha256 = evaluator._load_handoff(
        rebound,
        binding=binding,
        replacement_profile=True,
    )

    assert verified["capture_inventory"]["guard_identity_sha256"] == "a" * 64
    assert set(verified["capture_inventory"]) == evaluator.REPLACEMENT_INVENTORY_FIELDS


@pytest.mark.parametrize("guard_identity", ("A" * 64, f"{'a' * 64} "))
def test_replacement_handoff_inventory_rejects_noncanonical_guard_identity(
    tmp_path: Path,
    guard_identity: str,
) -> None:
    preregistration, _capture_root, handoff_path = _closed_fixture(tmp_path)
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    payload["capture_inventory"]["guard_identity_sha256"] = guard_identity
    rebound = _write_rehashed_handoff(tmp_path, payload)

    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="capture_handoff_guard_identity_invalid",
    ):
        evaluator._load_handoff(
            rebound,
            binding=handoff.load_preregistration(preregistration),
            replacement_profile=True,
        )


def test_verified_handoff_inventory_proves_screen_source_ready(
    tmp_path: Path,
) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    sealed = evaluator.load_sealed_inputs(
        preregistration_path=preregistration,
        handoff_path=handoff_path,
        now_epoch=END.timestamp(),
    )
    staging = tmp_path / "staging"
    staging.mkdir()
    snapshot_fence = evaluator._build_capture_snapshot_fence(
        capture_root=capture_root,
        verified_handoff=sealed.handoff_payload,
    )
    materialized = evaluator.materialize_verified_capture(
        sealed=sealed,
        capture_root=capture_root,
        staging_root=staging,
        snapshot_fence=snapshot_fence,
    )
    try:
        assert (
            evaluator.verified_handoff_proves_source_ready(
                sealed=sealed,
                materialized=materialized,
            )
            is True
        )
    finally:
        evaluator._cleanup_materialization(
            materialized.root,
            materialized.bar_paths,
            materialized.quote_paths,
        )


def test_content_addressed_publication_refuses_identical_overwrite(
    tmp_path: Path,
) -> None:
    root = tmp_path / "output"
    root.mkdir()
    payload = b'{"research_only":true}\n'
    identity = evaluator._publish_content_addressed(
        root=root,
        prefix="artifact",
        suffix=".json",
        payload=payload,
    )
    with pytest.raises(
        evaluator.EvaluationRefusal,
        match="research_artifact_output_exists",
    ):
        evaluator._publish_content_addressed(
            root=root,
            prefix="artifact",
            suffix=".json",
            payload=payload,
        )
    (root / identity.filename).chmod(stat.S_IREAD | stat.S_IWRITE)


def test_full_replacement_evaluation_binds_guard_identity(
    tmp_path: Path,
) -> None:
    preregistration, capture_root, handoff_path = _closed_replacement_fixture(tmp_path)
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()
    guard_sha256 = hashlib.sha256(
        (capture_root / handoff.GUARD_IDENTITY_FILENAME).read_bytes()
    ).hexdigest()

    _report_path, report = evaluator.evaluate_post_window(
        preregistration_path=preregistration,
        handoff_path=handoff_path,
        capture_root=capture_root,
        staging_root=staging,
        output_root=output,
        now_epoch=END.timestamp(),
    )

    assert report["capture_snapshot_data_writer_lock_required"] is True
    assert report["capture_snapshot_data_writer_lock_proven_free"] is True
    assert report["evidence_binding"]["guard_identity_sha256"] == guard_sha256
    assert not any(report["authority"].values())
    assert not any(staging.iterdir())


def test_full_empty_signal_evaluation_publishes_no_authority(
    tmp_path: Path,
) -> None:
    preregistration, capture_root, handoff_path = _closed_fixture(tmp_path)
    staging = tmp_path / "staging"
    output = tmp_path / "output"
    staging.mkdir()

    report_path, report = evaluator.evaluate_post_window(
        preregistration_path=preregistration,
        handoff_path=handoff_path,
        capture_root=capture_root,
        staging_root=staging,
        output_root=output,
        now_epoch=END.timestamp(),
    )

    assert report["evaluation_performed"] is True
    assert report["screen_source_scope_ready"] is True
    assert report["pre_t0_signal_bars_evaluated"] == 0
    assert report["pre_t0_quotes_evaluated"] == 0
    assert report["capture_snapshot_required_stopped_and_read_only"] is True
    assert report["capture_snapshot_active_hour_journal_absent"] is True
    assert report["capture_snapshot_data_writer_lock_required"] is False
    assert report["capture_snapshot_data_writer_lock_proven_free"] is False
    assert report["capture_snapshot_reverified_after_materialization"] is True
    assert report["capture_snapshot_fence_rechecked_before_publication"] is True
    assert report["frozen_screen_hash_is_exact_executed_bytes"] is True
    assert report["handoff_verifier_hash_is_exact_executed_bytes"] is True
    assert report["evidence_binding"]["frozen_screen_sha256"] == (
        evaluator.BASE_SCREEN_SOURCE_IMAGE.sha256
    )
    assert report["evidence_binding"]["handoff_verifier_sha256"] == (
        evaluator.HANDOFF_SOURCE_IMAGE.sha256
    )
    assert report["evidence_binding"]["evaluator_source_sha256"] == (
        evaluator.EVALUATOR_SOURCE_IMAGE.sha256
    )
    assert report["schema_version"] == (
        "fxstack.scalp.mtvclc_post_window_report.v2"
    )
    assert "guard_identity_sha256" not in report["evidence_binding"]
    assert report["preregistered_success_criteria_observed"] is False
    assert report["authority"] == handoff.FALSE_AUTHORITY
    assert not any(report["authority"].values())
    for field in (
        "success_claim_authorized",
        "promotion_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "runtime_authorized",
        "issuer_authorized",
        "signature_authorized",
        "broker_access_authorized",
        "order_authorized",
    ):
        assert report[field] is False
    assert report_path.name == (
        f"mtvclc_post_window_report_{hashlib.sha256(report_path.read_bytes()).hexdigest()}.json"
    )
    assert not any(staging.iterdir())
    cell_identity = report["ledgers"]["cell"]
    cell_path = output / cell_identity["filename"]
    rows = [json.loads(line) for line in cell_path.read_text().splitlines()]
    assert len(rows) == 44
    assert all(row["research_only"] is True for row in rows)
    assert all(not any(row["authority"].values()) for row in rows)
    if os.name != "nt":
        assert report_path.stat().st_mode & stat.S_IWUSR == 0
