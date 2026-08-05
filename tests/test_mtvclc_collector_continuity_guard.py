from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

import pytest

from tools import capture_ig_mt4_m1_activity as capture
from tools import check_mt4_tick_volume_collector_continuity as continuity


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_GUARD = ROOT / "ops" / "windows" / "26_guard_mtvclc_collector.ps1"
ZERO_SHA256 = "0" * 64


def _preregistration_payload() -> dict[str, Any]:
    now = int(datetime.now(tz=UTC).timestamp())
    sealed_epoch = now - 120
    t0_epoch = sealed_epoch + 60
    end_epoch = t0_epoch + capture.PROSPECTIVE_WINDOW_DAYS * 86_400
    body: dict[str, Any] = {
        "schema_version": capture.PREREGISTRATION_SCHEMA_VERSION,
        "research_only": True,
        "authority": dict(capture._FIXED_FALSE_AUTHORITY),
        "sealed_at_utc": datetime.fromtimestamp(sealed_epoch, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "scope": {
            "scope_version": capture.SCOPE_VERSION,
            "venue_id": capture.VENUE_ID,
            "ordered_symbols": list(capture.SYMBOLS),
            "cell_order": [
                {"config_id": "guard-test", "symbol": symbol, "side": side}
                for symbol in capture.SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "strategy": {
            "source_contract_id": capture.SOURCE_CONTRACT_ID,
            "activity_metric_id": capture.ACTIVITY_METRIC_ID,
        },
        "execution_contract": {
            "entry_type": "immediate_market",
            "pending_orders_forbidden": True,
            "maximum_entries_per_symbol_utc_day": 1,
        },
        "prospective_window": {
            "consecutive_days": capture.PROSPECTIVE_WINDOW_DAYS,
            "fixed_before_any_eligible_observation": True,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_or_outcome_evaluation_forbidden": True,
            "t0_utc_inclusive": datetime.fromtimestamp(t0_epoch, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "end_utc_exclusive": datetime.fromtimestamp(end_epoch, tz=UTC).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
        "source_identities": {
            "collector_source": {
                "sha256": hashlib.sha256(capture.TOOL_PATH.read_bytes()).hexdigest(),
            }
        },
    }
    return {
        **body,
        "preregistration_body_sha256": capture.canonical_sha256(body),
    }


def _fixture_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    preregistration = tmp_path / "preregistration.json"
    preregistration.write_text(
        json.dumps(_preregistration_payload(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    api_key = tmp_path / "bridge-api-key.txt"
    api_key.write_text("guard-test-secret-value\n", encoding="utf-8")
    output = tmp_path / "capture"
    output.mkdir()
    return preregistration, api_key, output


def _policy() -> continuity.GuardPolicy:
    return continuity.GuardPolicy(
        tick_interval_secs=2.0,
        bar_interval_secs=60.0,
        bar_limit=400,
        http_timeout_secs=5.0,
    )


def _emit_collection_only_chunk(preregistration: Path, output: Path) -> None:
    binding = capture.load_preregistration(preregistration)
    ledger = capture.ManifestLedger(output)
    ledger.emit(
        {
            "schema_version": capture.CHUNK_SCHEMA_VERSION,
            "utc_hour": datetime.now(tz=UTC).strftime("%Y%m%dT%H"),
            "segment_index": 1,
            **binding.chunk_fields(),
            "source": {"market_source_id": "a" * 64},
            "bars": [],
            "quotes": [],
            "last_bar_epoch_by_symbol": {symbol: 0 for symbol in capture.SYMBOLS},
            "last_tick_sequence_by_symbol": {symbol: 0 for symbol in capture.SYMBOLS},
            "last_tick_transport_epoch_by_symbol": {
                symbol: 0.0 for symbol in capture.SYMBOLS
            },
            "last_tick_snapshot_sha256_by_symbol": {
                symbol: ZERO_SHA256 for symbol in capture.SYMBOLS
            },
            "collection_only": True,
            "evaluation_performed": False,
            "success_claim_authorized": False,
            "authority_granted": False,
            "activation_authorized": False,
            "order_authorized": False,
        }
    )


def test_guard_identity_pins_resume_without_reading_secret_into_artifact(
    tmp_path: Path,
) -> None:
    preregistration, api_key, output = _fixture_paths(tmp_path)
    _emit_collection_only_chunk(preregistration, output)

    first = continuity.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        base_url="http://127.0.0.1:58710",
        policy=_policy(),
        initialize_guard=True,
    )
    second = continuity.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        base_url="http://127.0.0.1:58710",
        policy=_policy(),
        require_guard=True,
    )

    identity = output / continuity.GUARD_IDENTITY_FILENAME
    identity_bytes = identity.read_bytes()
    assert first["guard_identity_present"] is True
    assert second["guard_identity_sha256"] == first["guard_identity_sha256"]
    assert second["manifest_sequence"] == 1
    assert second["manifest_last_market_source_id"] == "a" * 64
    assert b"guard-test-secret-value" not in identity_bytes
    assert json.loads(identity_bytes)["api_key_file_path"] == str(api_key.resolve())
    assert json.loads(identity_bytes)["resume_contract"] == {
        "market_source_rollover_refused": True,
        "observed_gaps_preserved": True,
        "same_collector_source_required": True,
        "same_output_root_required": True,
        "same_preregistration_required": True,
        "t0_reset_forbidden": True,
    }


def test_guard_refuses_configuration_drift_after_identity_publication(
    tmp_path: Path,
) -> None:
    preregistration, api_key, output = _fixture_paths(tmp_path)
    continuity.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        base_url="http://127.0.0.1:58710",
        policy=_policy(),
        initialize_guard=True,
    )

    with pytest.raises(continuity.ContinuityRefusal, match="guard_identity_mismatch"):
        continuity.inspect_continuity(
            preregistration=preregistration,
            output_dir=output,
            api_key_file=api_key,
            base_url="http://127.0.0.1:58711",
            policy=_policy(),
            require_guard=True,
        )


def test_continuity_monitor_refuses_partial_manifest_tail(tmp_path: Path) -> None:
    preregistration, api_key, output = _fixture_paths(tmp_path)
    _emit_collection_only_chunk(preregistration, output)
    manifest = output / capture.MANIFEST_FILENAME
    with manifest.open("ab") as handle:
        handle.write(b'{"interrupted":')

    with pytest.raises(
        continuity.ContinuityRefusal,
        match="manifest_trailing_partial_line",
    ):
        continuity.inspect_continuity(
            preregistration=preregistration,
            output_dir=output,
            api_key_file=api_key,
            base_url="http://127.0.0.1:58710",
            policy=_policy(),
        )


def test_continuity_guard_rejects_filesystem_root_and_repository_output(
    tmp_path: Path,
) -> None:
    preregistration, api_key, _output = _fixture_paths(tmp_path)
    with pytest.raises(continuity.ContinuityRefusal, match="must_be_external"):
        continuity.inspect_continuity(
            preregistration=preregistration,
            output_dir=Path(tmp_path.anchor),
            api_key_file=api_key,
            base_url="http://127.0.0.1:58710",
            policy=_policy(),
        )
    with pytest.raises(continuity.ContinuityRefusal, match="must_be_external"):
        continuity.inspect_continuity(
            preregistration=preregistration,
            output_dir=ROOT,
            api_key_file=api_key,
            base_url="http://127.0.0.1:58710",
            policy=_policy(),
        )


def test_windows_guard_contract_is_collection_only_and_duplicate_safe() -> None:
    source = POWERSHELL_GUARD.read_text(encoding="utf-8")
    assert 'ValidateSet("Health", "AdoptRunning", "StartOrResume")' in source
    assert "[IO.FileShare]::None" in source
    assert "Get-CimInstance Win32_Process" in source
    assert "more_than_one_independent_collector_writer" in source
    assert "running_writer_not_pinned_to_guard_configuration" in source
    assert '"--api-key-file", $ResolvedApiKeyFile' in source
    assert re.search(r"--api-key(?:\s|\")", source) is None
    assert '"--rollover-mode", $RolloverMode' in source
    assert '$RolloverMode = "refuse"' in source
    assert "--duration" not in source
    assert "screen_mt4_tick_volume" not in source
    assert "run_evaluation" not in source
    assert "compute_performance" not in source
    assert "Start-Process" not in source
    assert "Stop-Process" not in source
    assert "taskkill" not in source.lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell health contract")
def test_windows_health_is_read_only_and_reports_absent_writer(tmp_path: Path) -> None:
    preregistration, api_key, output = _fixture_paths(tmp_path)
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_GUARD),
            "-Action",
            "Health",
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )

    assert completed.returncode == 3, completed.stderr
    report = json.loads(completed.stdout.strip())
    assert report["status"] == "stopped_during_window"
    assert report["writer_group_count"] == 0
    assert report["evaluation_performed"] is False
    assert report["order_authorized"] is False
    assert not (output / continuity.GUARD_IDENTITY_FILENAME).exists()
    assert not (output / "supervision").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell adoption contract")
def test_windows_adopts_one_writer_with_omitted_default_options(
    tmp_path: Path,
) -> None:
    preregistration, api_key, output = _fixture_paths(tmp_path)
    # The inert process only presents the collector's argument identity to WMI;
    # it never imports the collector or contacts the bridge.
    dummy = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            str(capture.TOOL_PATH),
            "--base-url",
            "http://127.0.0.1:58710",
            "--api-key-file",
            str(api_key),
            "--preregistration",
            str(preregistration),
            "--output-dir",
            str(output),
            # All optional values are intentionally omitted. The guard may
            # accept them only as the collector's exact sealed defaults.
        ]
    )
    adopt_command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(POWERSHELL_GUARD),
        "-Action",
        "AdoptRunning",
        "-PythonExe",
        sys.executable,
        "-Preregistration",
        str(preregistration),
        "-OutputDir",
        str(output),
        "-ApiKeyFile",
        str(api_key),
    ]
    adopter = subprocess.Popen(
        adopt_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=ROOT,
    )
    try:
        identity = output / continuity.GUARD_IDENTITY_FILENAME
        lock = output / "supervision" / "collector-writer.lock"
        for _attempt in range(100):
            if identity.exists() and lock.exists():
                break
            if adopter.poll() is not None:
                break
            time.sleep(0.05)
        assert adopter.poll() is None
        assert identity.is_file()
        assert lock.is_file()

        health_command = list(adopt_command)
        health_command[health_command.index("AdoptRunning")] = "Health"
        health = subprocess.run(
            health_command,
            check=False,
            capture_output=True,
            text=True,
            cwd=ROOT,
            timeout=30,
        )
        assert health.returncode == 0, health.stderr
        health_report = json.loads(health.stdout.strip())
        assert health_report["status"] == "starting"
        assert health_report["writer_group_count"] == 1
        assert health_report["supervisor_lock_held"] is True
    finally:
        dummy.terminate()
        dummy.wait(timeout=10)
    stdout, stderr = adopter.communicate(timeout=15)
    assert adopter.returncode == 0, stderr
    reports = [json.loads(line) for line in stdout.splitlines() if line.strip()]
    assert reports[0]["status"] == "running_writer_adopted"
    assert reports[0]["supervisor_lock_held"] is True
    assert reports[-1]["status"] == "adopted_writer_exited"
