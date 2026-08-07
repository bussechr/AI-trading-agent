from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_ROOT = REPO_ROOT / "tools"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for path in (TOOLS_ROOT, FXSTACK_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import scalp_causal_walk_forward as scalp_wf  # noqa: E402


PAIRS = ["EURUSD", "USDJPY"]
T0 = dt.datetime(2026, 1, 5, 6, 0, tzinfo=dt.timezone.utc)


def _write_m1_csv(root: Path, pair: str, *, rows: int = 100) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{pair}_M1.csv"
    lines = [
        "timestamp,bid_open,bid_high,bid_low,bid_close,"
        "ask_open,ask_high,ask_low,ask_close,volume"
    ]
    base = 1.10 if pair == "EURUSD" else 150.0
    point = 0.0001 if pair == "EURUSD" else 0.01
    half_spread = point * 0.25
    for index in range(rows):
        timestamp = T0 + dt.timedelta(minutes=index)
        wave = math.sin(index / 3.0) * point * 2.0
        trend = index * point * 0.02
        open_mid = base + wave + trend
        close_mid = open_mid + math.sin(index / 2.0) * point * 0.4
        high_mid = max(open_mid, close_mid) + point * 0.8
        low_mid = min(open_mid, close_mid) - point * 0.8
        bid = [value - half_spread for value in (open_mid, high_mid, low_mid, close_mid)]
        ask = [value + half_spread for value in (open_mid, high_mid, low_mid, close_mid)]
        lines.append(
            ",".join(
                [
                    timestamp.isoformat().replace("+00:00", "Z"),
                    *(f"{value:.8f}" for value in (*bid, *ask)),
                    "10",
                ]
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _args(source_root: Path, *, resume: bool = False) -> Namespace:
    return Namespace(
        fill_delay_bars=1,
        mode=None,
        scalp_csv_root=str(source_root),
        scalp_extra_spread_bps=0.25,
        scalp_sl_extra_slip_bps=0.1,
        resume=resume,
    )


def _windows() -> list[dict[str, object]]:
    return [
        {
            "name": "window_01",
            "train_end": T0 + dt.timedelta(minutes=40),
            "test_start": T0 + dt.timedelta(minutes=45),
            "test_end": T0 + dt.timedelta(minutes=90),
        }
    ]


def test_scalp_walk_forward_seals_inputs_and_gates_economics(tmp_path: Path) -> None:
    source_root = tmp_path / "external_source"
    for pair in PAIRS:
        _write_m1_csv(source_root, pair)
    output_root = tmp_path / "research_bundle"
    output_root.mkdir()

    summary = scalp_wf.run_scalp_walk_forward(
        args=_args(source_root),
        windows=_windows(),
        requested_pairs=PAIRS,
        root=output_root,
    )

    window_root = output_root / "window_01"
    audit = json.loads((window_root / "point_in_time_audit.json").read_text(encoding="utf-8"))
    observations = json.loads(
        (window_root / "raw_replay_observations.json").read_text(encoding="utf-8")
    )
    bundle = json.loads((window_root / "input_bundle_manifest.json").read_text(encoding="utf-8"))
    train = json.loads(
        (window_root / "inputs" / "train_m1" / "snapshot_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    replay = json.loads(
        (window_root / "inputs" / "replay_m1" / "snapshot_manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert summary["passed"] is True
    assert summary["economics_interpretation_allowed"] is False
    assert summary["strategy_goal"]["target_win_rate"] == 0.90
    assert summary["strategy_goal"]["passed"] is False
    assert summary["success_claim_authorized"] is False
    assert audit["passed"] is True
    assert audit["requested_pair_completeness"] is True
    assert audit["separate_physical_train_and_replay_snapshots"] is True
    assert audit["fill_delay_bars"] == 1
    assert audit["future_data_access"] == "forbidden"
    assert observations["economics_interpretation_allowed"] is False
    assert observations["audit_status"] == "pending"
    assert observations["economic_metrics_finite_and_complete"] is False
    assert not (window_root / "advisory_economics.json").exists()
    assert observations["spread_model"] == {
        "extra_spread_bps": 0.25,
        "optimistic_mode": False,
        "sl_extra_slip_bps": 0.1,
        "source_bid_ask_ohlc": True,
        "source_spread_intrinsic": True,
    }
    assert train["requested_pairs"] == PAIRS
    assert replay["requested_pairs"] == PAIRS
    assert {item["pair"] for item in train["files"]} == set(PAIRS)
    assert {item["pair"] for item in replay["files"]} == set(PAIRS)
    assert all(item["rows"] == 40 for item in train["files"])
    assert all(item["rows"] == 90 for item in replay["files"])
    assert all(item["max_knowledge_ts"] == "2026-01-05T06:40:00Z" for item in train["files"])
    assert all(item["max_knowledge_ts"] == "2026-01-05T07:30:00Z" for item in replay["files"])
    assert all(not Path(item["path"]).is_absolute() for item in bundle["files"])
    assert str(source_root.resolve()) not in json.dumps(bundle)
    assert "source_root" not in json.dumps(bundle)
    for manifest_path in output_root.rglob("*.json"):
        manifest_text = manifest_path.read_text(encoding="utf-8")
        assert str(source_root.resolve()) not in manifest_text
        assert '"source_path":' not in manifest_text
        assert '"source_root":' not in manifest_text
    for pair in PAIRS:
        train_path = window_root / "inputs" / "train_m1" / f"{pair}_M1.csv"
        replay_path = window_root / "inputs" / "replay_m1" / f"{pair}_M1.csv"
        assert train_path.resolve() != replay_path.resolve()
        assert train_path.read_bytes() != replay_path.read_bytes()
    for entry in observations["replay_inputs"]:
        assert (window_root / entry).resolve().is_relative_to(window_root.resolve())
    timeline_path = window_root / observations["entry_timeline"]["path"]
    for line in timeline_path.read_text(encoding="utf-8").splitlines():
        assert json.loads(line)["observed_fill_delay_bars"] == 1
    replay_csv = window_root / "inputs" / "replay_m1" / "EURUSD_M1.csv"
    replay_csv.write_text(replay_csv.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="file hash mismatch"):
        scalp_wf.verify_input_bundle(window_root, requested_pairs=PAIRS)


def test_scalp_snapshot_fails_closed_on_missing_requested_pair(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _write_m1_csv(source_root, "EURUSD")
    output_root = tmp_path / "output"
    output_root.mkdir()

    with pytest.raises(FileNotFoundError, match="USDJPY_M1.csv"):
        scalp_wf.run_scalp_walk_forward(
            args=_args(source_root),
            windows=_windows(),
            requested_pairs=PAIRS,
            root=output_root,
        )

    assert not (output_root / "causal_walk_forward_summary.json").exists()
    assert not (output_root / "window_01" / "advisory_economics.json").exists()


def test_scalp_bundle_rejects_manifest_path_escape(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    for pair in PAIRS:
        _write_m1_csv(source_root, pair)
    bundle_root = tmp_path / "bundle"
    scalp_wf._prepare_window(
        bundle_root=bundle_root,
        source_csv_root=source_root,
        requested_pairs=PAIRS,
        train_end=T0 + dt.timedelta(minutes=40),
        test_end=T0 + dt.timedelta(minutes=90),
        extra_spread_bps=0.25,
        sl_extra_slip_bps=0.1,
        resume=False,
    )
    manifest_path = bundle_root / "input_bundle_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.csv"
    manifest.pop("manifest_content_sha256", None)
    manifest["manifest_content_sha256"] = hashlib.sha256(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="escapes root"):
        scalp_wf.verify_input_bundle(bundle_root, requested_pairs=PAIRS)


def test_scalp_windows_and_child_environment_fail_closed() -> None:
    windows = _windows()
    out_of_order = [
        {
            "name": "later",
            "train_end": T0 + dt.timedelta(days=2),
            "test_start": T0 + dt.timedelta(days=2, minutes=5),
            "test_end": T0 + dt.timedelta(days=2, minutes=10),
        },
        windows[0],
    ]
    with pytest.raises(ValueError, match="chronological order"):
        scalp_wf._validate_ordered_windows(out_of_order)
    escaped = [dict(windows[0], name="../outside")]
    with pytest.raises(ValueError, match="safe identifier"):
        scalp_wf._validate_ordered_windows(escaped)
    overlapping = [
        windows[0],
        {
            "name": "window_02",
            "train_end": T0 + dt.timedelta(minutes=80),
            "test_start": T0 + dt.timedelta(minutes=85),
            "test_end": T0 + dt.timedelta(minutes=100),
        },
    ]
    with pytest.raises(ValueError, match="must not overlap"):
        scalp_wf._validate_ordered_windows(overlapping)

    safe = scalp_wf.scalp_child_environment(
        {
            "PATH": "safe",
            "FXSCALP_SIGNAL_MODE": "momentum",
            "FXSCALP_BRIDGE_URL": "http://live",
            "FXSTACK_DATABASE_URL": "postgresql://live",
            "FXSTACK_REGISTRY_ROOT": "active-registry",
            "FXSTACK_MODEL_ACTIVATION_MANIFEST": "active.json",
            "MT4_BRIDGE_URL": "http://live",
            "AWS_ACCESS_KEY_ID": "credential",
            "HOME": "/credential-discovery",
            "USERPROFILE": "C:/credential-discovery",
            "SSH_AUTH_SOCK": "agent.sock",
        }
    )
    assert safe == {
        "PATH": "safe",
        "FXSTACK_EXECUTION_PROVIDER": "offline",
        "FXSTACK_MARKET_DATA_PROVIDER": "offline",
    }


def test_fixed_scalp_replay_refuses_non_one_bar_delay(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    for pair in PAIRS:
        _write_m1_csv(source_root, pair)
    output_root = tmp_path / "output"
    output_root.mkdir()
    args = _args(source_root)
    args.fill_delay_bars = 2

    with pytest.raises(ValueError, match="fill_delay_bars=1"):
        scalp_wf.run_scalp_walk_forward(
            args=args,
            windows=_windows(),
            requested_pairs=PAIRS,
            root=output_root,
        )
    assert not (output_root / "causal_walk_forward_summary.json").exists()


def test_authoritative_walk_forward_cli_dispatches_opt_in_scalp_path(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _write_m1_csv(source_root, "EURUSD")
    output_root = tmp_path / "output"
    command = [
        sys.executable,
        str(TOOLS_ROOT / "run_causal_walk_forward.py"),
        "--research-engine",
        "scalp",
        "--pairs",
        "EURUSD",
        "--window",
        "window_01,2026-01-05T06:40:00Z,2026-01-05T06:45:00Z,2026-01-05T07:30:00Z",
        "--out-root",
        str(output_root),
        "--scalp-csv-root",
        str(source_root),
        "--scalp-extra-spread-bps",
        "0.25",
        "--scalp-sl-extra-slip-bps",
        "0.1",
    ]

    completed = subprocess.run(
        command,
        cwd=str(REPO_ROOT),
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(
        (output_root / "causal_walk_forward_summary.json").read_text(encoding="utf-8")
    )
    assert summary["research_engine"] == "scalp"
    assert summary["passed"] is True
    assert summary["economics_interpretation_allowed"] is False
    assert '"passed": true' in completed.stdout.lower()
    assert (output_root / "window_01" / "point_in_time_audit.json").is_file()
    assert not (output_root / "window_01" / "advisory_economics.json").exists()


def test_economics_readiness_rejects_null_nonfinite_and_untraded_metrics() -> None:
    finite = {
        "trades": 3,
        "win_rate": 2 / 3,
        "total_r": 1.0,
        "mean_r": 1 / 3,
        "mean_r_ci95": [-0.5, 1.0],
        "mean_pnl_bps": 2.0,
        "profit_factor": 2.5,
        "max_drawdown_r": 1.0,
        "avg_bars_held": 4.0,
    }
    assert scalp_wf._economic_metrics_finite_and_complete([finite]) is True
    assert scalp_wf._economic_metrics_finite_and_complete([{**finite, "profit_factor": None}]) is False
    assert scalp_wf._economic_metrics_finite_and_complete([{**finite, "mean_r": math.inf}]) is False
    assert scalp_wf._economic_metrics_finite_and_complete([{**finite, "trades": 0}]) is False


def test_resume_rejects_linked_outputs_and_stale_economics(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    _write_m1_csv(source_root, "EURUSD")
    output_root = tmp_path / "output"
    output_root.mkdir()
    scalp_wf.run_scalp_walk_forward(
        args=_args(source_root),
        windows=_windows(),
        requested_pairs=["EURUSD"],
        root=output_root,
    )
    window_root = output_root / "window_01"
    audit_path = window_root / "point_in_time_audit.json"
    audit_bytes = audit_path.read_bytes()
    outside = tmp_path / "outside.json"
    outside.write_text("sentinel", encoding="utf-8")
    audit_path.unlink()
    os.link(outside, audit_path)

    with pytest.raises(RuntimeError, match="private regular file"):
        scalp_wf.run_scalp_walk_forward(
            args=_args(source_root, resume=True),
            windows=_windows(),
            requested_pairs=["EURUSD"],
            root=output_root,
        )
    assert outside.read_text(encoding="utf-8") == "sentinel"

    audit_path.unlink()
    audit_path.write_bytes(audit_bytes)
    (window_root / "advisory_economics.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="published advisory economics"):
        scalp_wf.run_scalp_walk_forward(
            args=_args(source_root, resume=True),
            windows=_windows(),
            requested_pairs=["EURUSD"],
            root=output_root,
        )
