from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import random
import sys

import numpy as np

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS, IG_MT4_VENUE_ID
from fxstack.schemas.entry import EntryBar, EntryEvaluationRequest
from fxstack.strategy.scalp_dislocation import evaluate_dislocation


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "production_scalp_evidence.py"
SPEC = importlib.util.spec_from_file_location("production_scalp_evidence_test", TOOL_PATH)
assert SPEC is not None and SPEC.loader is not None
producer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = producer
SPEC.loader.exec_module(producer)


def _bars(seed: int) -> tuple[EntryBar, ...]:
    rng = random.Random(seed)
    close = 1.1
    rows: list[EntryBar] = []
    for index in range(30):
        open_price = close
        close = max(0.5, open_price + rng.uniform(-0.0015, 0.0015))
        spread = close * rng.uniform(0.2, 1.5) / 1e4
        rows.append(
            EntryBar(
                symbol="EURUSD",
                venue_id=IG_MT4_VENUE_ID,
                source_id="test_source",
                source_version="v1",
                minute_epoch=1_800_000_000 + index * 60,
                bar_seconds=60,
                open=open_price,
                high=max(open_price, close) + 0.0001,
                low=min(open_price, close) - 0.0001,
                close=close,
                bid_close=close - spread / 2.0,
                ask_close=close + spread / 2.0,
                closed=True,
                quality_flags=(),
            )
        )
    return tuple(rows)


def test_attempt_manifest_accounts_for_disclosed_grid_and_preregistered_trials() -> None:
    assert len(producer.FIXED_TRIALS) == 232
    assert producer.FIXED_TRIALS[0].attempt_id == producer.SELECTED_ATTEMPT_ID
    assert producer.FIXED_TRIALS[0].policy.config_sha256() == (
        producer.DislocationPolicy().config_sha256()
    )
    assert len({item.attempt_id for item in producer.FIXED_TRIALS}) == 232
    assert len({item.policy.config_sha256() for item in producer.FIXED_TRIALS}) == 232
    assert sum(item.attempt_id.startswith("disclosed_grid_") for item in producer.FIXED_TRIALS) == 223


def test_fast_prefilter_has_no_false_negative_against_production_evaluator() -> None:
    for seed in range(20):
        bars = _bars(seed)
        features = producer._signal_features(bars, producer.DislocationPolicy())
        spread_bps = (bars[-1].ask_close - bars[-1].bid_close) / bars[-1].close * 1e4
        for trial in producer.FIXED_TRIALS:
            proposal = evaluate_dislocation(
                EntryEvaluationRequest(
                    symbol="EURUSD",
                    bars=bars,
                    spread_bps=spread_bps,
                ),
                trial.policy,
            )
            prefilter = producer._fast_policy_can_signal(
                policy=trial.policy,
                features=features,
                spread_bps=spread_bps,
            )
            assert not proposal.allowed or prefilter


def test_stdlib_npz_writer_is_numpy_compatible(tmp_path: Path) -> None:
    raw = producer._write_raw_array(
        tmp_path / "values.raw",
        descr="<f8",
        shape=(2, 3),
        values=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
    )
    output = tmp_path / "values.npz"
    producer._write_npz_from_raw(output, {"values": raw})

    with np.load(output, allow_pickle=False) as arrays:
        assert arrays.files == ["values"]
        assert arrays["values"].dtype == np.dtype("float64")
        assert arrays["values"].shape == (2, 3)
        assert arrays["values"].tolist() == [
            [1.0, 2.0, 3.0],
            [4.0, 5.0, 6.0],
        ]


def test_fee_schedule_requires_hashed_source_and_exact_scope(tmp_path: Path) -> None:
    source = tmp_path / "ig-fees.txt"
    source.write_text("operator supplied IG demo fee schedule\n", encoding="utf-8")
    payload = {
        "schema_version": "fxstack.external_ig_mt4_fee_schedule.v1",
        "source_errors": [],
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": "demo",
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "effective_at_epoch": 1_800_000_000.0,
        "source_document_path": source.name,
        "source_document_sha256": producer._file_sha256(source),
        "symbols": {
            symbol: {
                "commission_bps_per_round_trip": 0.0,
                "configured_financing_bps_per_trade": 0.0,
            }
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
    }
    payload["operator_attestation_sha256"] = producer._canonical_sha256(payload)
    schedule = tmp_path / "fee-schedule.json"
    schedule.write_text(json.dumps(payload), encoding="utf-8")

    loaded = producer._load_fee_schedule(schedule)

    assert loaded.rows[IG_MT4_SCALP_SYMBOLS[0]][
        "commission_bps_per_round_trip"
    ] == 0.0
    payload["symbols"].pop(IG_MT4_SCALP_SYMBOLS[-1])
    payload["operator_attestation_sha256"] = producer._canonical_sha256(
        {key: value for key, value in payload.items() if key != "operator_attestation_sha256"}
    )
    schedule.write_text(json.dumps(payload), encoding="utf-8")
    try:
        producer._load_fee_schedule(schedule)
    except producer.EvidenceRefusal as exc:
        assert str(exc) == "fee_schedule_symbol_scope_invalid"
    else:  # pragma: no cover - fail-closed assertion
        raise AssertionError("incomplete fee scope was accepted")

