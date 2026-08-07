from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "ops" / "linux" / "40_full_scale_backtest_gpu.sh"


def test_gpu_backtest_pipeline_uses_focused_entrypoints_directly() -> None:
    source = PIPELINE.read_text(encoding="utf-8")
    expected_paths = (
        "fx-quant-stack/scripts/preflight.py",
        "fx-quant-stack/scripts/gpu_check.py",
        "fx-quant-stack/scripts/ingest_bars.py",
        "fx-quant-stack/scripts/build_features.py",
        "fx-quant-stack/scripts/build_labels.py",
        "fx-quant-stack/scripts/train_all.py",
        "fx-quant-stack/scripts/activate_models.py",
        "fx-quant-stack/scripts/backtest.py",
        "tools/fetch_dukascopy_matrix.py",
        "tools/dukascopy_coverage_gate.py",
        "tools/fxstack_full_backtest.py",
    )
    for path in expected_paths:
        assert path in source

    assert "src.trader.cli" not in source
    assert "tests/test_trader_cli.py" in source
    assert "tests/test_public_docs_contract.py" in source
    assert "test_trader_cli_fxstack_commands.py" not in source
    assert "test_runtime_service_v2.py" not in source
    assert "TRADER_BRIDGE_IMPL" not in source
    assert "TRADER_RUNTIME_IMPL" not in source
    assert "run_phase train_deep_stale" not in source
    assert "train_deep_stale()" not in source
    assert 'local belief_arg="--with-belief"' in source
    assert 'belief_arg="--no-with-belief"' in source
