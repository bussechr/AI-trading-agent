from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest

from fxstack.backtest.harness.lean_codegen import (
    LeanGateThresholds,
    render_lean_algorithm,
    render_lean_config,
    thresholds_from_config,
    write_lean_project,
)

# A representative loop-tuned config: distinctive values so we can assert they are
# actually templated into the generated source rather than the dataclass defaults.
TUNED_CONFIG = {
    "gates": {
        "min_swing_prob": 0.63,
        "min_entry_prob": 0.67,
        "min_trade_prob": 0.61,
        "min_expected_edge_bps": 4.5,
        "min_expected_edge_rescue_margin_bps": 0.3,
        "max_allowed_spread_bps": 2.25,
    },
    "cost_model": {"slippage_bps": 0.15},
    "risk": {
        "max_total_positions": 4,
        "max_pair_positions": 1,
        "default_order_lots": 0.05,
        "max_pair_exposure": 0.015,
        "max_total_exposure": 0.04,
    },
    "portfolio": {"max_realized_corr_share": 0.7},
}

PAIRS = ["EURUSD", "GBPUSD", "USDJPY"]


def test_render_defines_qcalgorithm_subclass_and_parses() -> None:
    src = render_lean_algorithm(
        TUNED_CONFIG, pairs=PAIRS, start="2022-01-01", end="2022-12-31"
    )
    # Parses as valid Python.
    tree = ast.parse(src)

    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert len(classes) == 1
    cls = classes[0]
    base_names = {b.id for b in cls.bases if isinstance(b, ast.Name)}
    assert "QCAlgorithm" in base_names

    method_names = {
        n.name for n in cls.body if isinstance(n, ast.FunctionDef)
    }
    assert {"Initialize", "OnData"} <= method_names


def test_configured_thresholds_appear_in_source() -> None:
    src = render_lean_algorithm(
        TUNED_CONFIG, pairs=PAIRS, start="2022-01-01", end="2022-12-31", cash=250000
    )
    # Each tuned threshold value must be present verbatim.
    assert "0.63" in src  # min_swing_prob
    assert "0.67" in src  # min_entry_prob
    assert "4.5" in src  # min_expected_edge_bps
    assert "2.25" in src  # max_allowed_spread_bps
    assert "0.15" in src  # slippage_bps
    assert "0.05" in src  # default_order_lots
    # Threshold keys are emitted so the gate logic is self-documenting.
    assert "min_swing_prob" in src
    assert "max_allowed_spread_bps" in src
    assert "max_total_positions" in src
    # Cash is templated.
    assert "250000" in src


def test_pairs_are_embedded_and_normalized() -> None:
    src = render_lean_algorithm(
        TUNED_CONFIG, pairs=["eur/usd", "gbpusd "], start="2022-01-01", end="2022-06-30"
    )
    assert "EURUSD" in src
    assert "GBPUSD" in src
    # Subscriptions go through AddForex.
    assert "AddForex" in src


def test_dates_render_into_initialize() -> None:
    src = render_lean_algorithm(
        TUNED_CONFIG, pairs=["EURUSD"], start=date(2021, 3, 4), end=date(2021, 9, 8)
    )
    assert "datetime(2021, 3, 4)" in src
    assert "datetime(2021, 9, 8)" in src
    assert "SetStartDate" in src
    assert "SetEndDate" in src


def test_deterministic_output() -> None:
    kwargs = dict(pairs=PAIRS, start="2022-01-01", end="2022-12-31", cash=123456.0)
    a = render_lean_algorithm(TUNED_CONFIG, **kwargs)
    b = render_lean_algorithm(TUNED_CONFIG, **kwargs)
    assert a == b


def test_thresholds_from_config_falls_back_to_defaults() -> None:
    thr = thresholds_from_config(None)
    assert thr == LeanGateThresholds()
    # Partial config: only one gate overridden, rest defaulted.
    partial = thresholds_from_config({"gates": {"min_swing_prob": 0.71}})
    assert partial.min_swing_prob == 0.71
    assert partial.min_entry_prob == LeanGateThresholds().min_entry_prob


def test_render_config_payload() -> None:
    payload = render_lean_config(
        TUNED_CONFIG, pairs=PAIRS, start="2022-01-01", end="2022-12-31", cash=100000
    )
    assert payload["algorithm-language"] == "Python"
    assert payload["algorithm-location"] == "main.py"
    params = payload["parameters"]
    assert params["pairs"] == "EURUSD,GBPUSD,USDJPY"
    assert params["start-date"] == "2022-01-01"
    assert params["end-date"] == "2022-12-31"
    assert params["min-swing-prob"] == repr(0.63)


def test_write_lean_project_creates_files(tmp_path: Path) -> None:
    paths = write_lean_project(
        TUNED_CONFIG,
        tmp_path,
        pairs=PAIRS,
        start="2022-01-01",
        end="2022-12-31",
    )
    main_path = Path(paths["main"])
    config_path = Path(paths["config"])
    assert main_path.exists()
    assert config_path.exists()
    assert main_path.name == "main.py"
    assert config_path.name == "config.json"

    # main.py is valid Python.
    ast.parse(main_path.read_text(encoding="utf-8"))
    # config.json is valid JSON with the expected structure.
    loaded = json.loads(config_path.read_text(encoding="utf-8"))
    assert loaded["algorithm-type-name"]
    assert loaded["parameters"]["pairs"] == "EURUSD,GBPUSD,USDJPY"


def test_class_name_override_is_sanitized() -> None:
    src = render_lean_algorithm(
        TUNED_CONFIG,
        pairs=["EURUSD"],
        start="2022-01-01",
        end="2022-12-31",
        class_name="My Strat 7!",
    )
    tree = ast.parse(src)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert classes[0].name == "MyStrat7"


def test_invalid_inputs_raise() -> None:
    with pytest.raises(ValueError):
        render_lean_algorithm(TUNED_CONFIG, pairs=[], start="2022-01-01", end="2022-12-31")
    with pytest.raises(ValueError):
        render_lean_algorithm(
            TUNED_CONFIG, pairs=["EURUSD"], start="2022-12-31", end="2022-01-01"
        )
    with pytest.raises(ValueError):
        render_lean_algorithm(
            TUNED_CONFIG, pairs=["EURUSD"], start="not-a-date", end="2022-01-01"
        )
