from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools import check_mt4_tick_volume_collector_continuity_resilient_v2 as inspector

ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"
INSPECTOR = (
    ROOT / "tools" / "check_mt4_tick_volume_collector_continuity_resilient_v2.py"
)
CORE = ROOT / "tools" / "check_mt4_tick_volume_collector_continuity_resilient.py"
GUARD = ROOT / "ops" / "windows" / "27_guard_mtvclc_collector_resilient_v2.ps1"
ENSURE = ROOT / "ops" / "windows" / "29_ensure_mtvclc_collector_resilient_v2.ps1"
REGISTER = (
    ROOT
    / "ops"
    / "windows"
    / "29_register_mtvclc_collector_resilient_watchdog_v2.ps1"
)
# Successor collector wires can append their registrar here without weakening
# the scheduler-ownership, exact-identity, or bounded-health assertions below.
DURABLE_TASK_START_REGISTRARS = (REGISTER,)
V4_SEALER = (
    ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v4.py"
)
BASE_SEALER_TEST = (
    ROOT
    / "fx-quant-stack"
    / "tests"
    / "test_seal_mt4_tick_volume_preregistration.py"
)
BRIDGE_EA_REPOSITORY = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"


def _parse_powershell(path: Path) -> None:
    command = (
        "$errors=$null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}',"
        "[ref]$null,[ref]$errors); "
        "if($errors.Count -gt 0){$errors | ForEach-Object { Write-Error $_ }; exit 1}"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


def _write_preview_inputs(
    tmp_path: Path,
    *,
    body_sha256: str,
) -> tuple[Path, Path, Path]:
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(BRIDGE_EA_REPOSITORY.read_bytes())
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"test-only deployed BridgeEA executable identity")
    preregistration = tmp_path / "preregistration.json"
    preregistration.write_text(
        json.dumps(
            {
                "preregistration_body_sha256": body_sha256,
                "source_identities": {
                    "production_engine_component:MQL4/Experts/BridgeEA.mq4": (
                        _identity(BRIDGE_EA_REPOSITORY)
                    ),
                    "bridge_ea_deployed_source": _identity(deployed_source),
                    "bridge_ea_deployed_ex4": _identity(deployed_ex4),
                },
                "upstream_producer_software": {
                    "producer_software_body_sha256": "c" * 64,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return preregistration, deployed_source, deployed_ex4


def _synthetic_v4_preregistration(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, Path, Path]:
    sealer_spec = importlib.util.spec_from_file_location(
        "_mtvclc_gap_v3_supervision_v4_sealer",
        V4_SEALER,
    )
    assert sealer_spec is not None and sealer_spec.loader is not None
    sealer = importlib.util.module_from_spec(sealer_spec)
    sys.modules[sealer_spec.name] = sealer
    sealer_spec.loader.exec_module(sealer)

    base_spec = importlib.util.spec_from_file_location(
        "_mtvclc_gap_v3_supervision_base_sealer_test",
        BASE_SEALER_TEST,
    )
    assert base_spec is not None and base_spec.loader is not None
    base_test = importlib.util.module_from_spec(base_spec)
    sys.modules[base_spec.name] = base_test
    base_spec.loader.exec_module(base_test)

    sealed_inputs = tmp_path / "synthetic-v4-sealed-inputs"
    sealed_inputs.mkdir()
    cost_capture, cost_samples, fee_attestation = base_test._inputs(
        sealed_inputs
    )
    deployed = tmp_path / "synthetic-v4-deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(BRIDGE_EA_REPOSITORY.read_bytes())
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"test-only synthetic BridgeEA executable")
    payload = sealer.build_preregistration(
        cost_capture_json=cost_capture,
        cost_capture_npz=cost_samples,
        fee_attestation=fee_attestation,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
        sealed_at=datetime.now(UTC).replace(microsecond=0),
        start_delay_seconds=3600,
    )
    claimed = payload["preregistration_body_sha256"]
    preregistration = (
        tmp_path / f"mtvclc_gap_v3_preregistration_{claimed}.json"
    )
    preregistration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return payload, preregistration, deployed_source, deployed_ex4


def _load_v3_collector_cases():  # type: ignore[no-untyped-def]
    path = ROOT / "tests" / "test_mtvclc_resilient_v3_collector.py"
    spec = importlib.util.spec_from_file_location("_mtvclc_v3_cases", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _capture_fixture(
    tmp_path: Path,
    *,
    window_already_ended: bool = False,
) -> dict[str, object]:
    cases = _load_v3_collector_cases()
    capture = inspector.collector_v3
    t0 = cases.NOW
    payload = cases._preregistration_payload()
    if window_already_ended:
        end = float(int(time.time() // 60) * 60 - 60)
        t0 = end - capture.PROSPECTIVE_WINDOW_DAYS * 86_400
        payload["sealed_at_utc"] = datetime.fromtimestamp(
            t0 - 900,
            tz=UTC,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload["prospective_window"]["t0_utc_inclusive"] = datetime.fromtimestamp(
            t0,
            tz=UTC,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        payload["prospective_window"]["end_utc_exclusive"] = datetime.fromtimestamp(
            end,
            tz=UTC,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        cases._rehash_preregistration(payload)
    preregistration = cases._write_preregistration(
        tmp_path / "candidate.json",
        payload,
    )
    producer_paths = cases._producer_paths(tmp_path / "deployed")
    output = tmp_path / "capture"
    output.mkdir()
    api_key = tmp_path / "key.txt"
    api_key.write_text("metadata-only-credential", encoding="utf-8")
    policy = inspector.GuardPolicy(
        tick_interval_secs=2.0,
        bar_interval_secs=60.0,
        bar_limit=400,
        http_timeout_secs=5.0,
    )
    inspect_kwargs = {
        "preregistration": preregistration,
        "output_dir": output,
        "api_key_file": api_key,
        "bridge_ea_repository_source": producer_paths[0],
        "bridge_ea_deployed_source": producer_paths[1],
        "bridge_ea_deployed_ex4": producer_paths[2],
        "base_url": "http://127.0.0.1:58710",
        "policy": policy,
    }
    binding = capture.load_preregistration(
        preregistration,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
    )
    if window_already_ended:
        initialization_lock = capture.ExclusiveDataWriterLock(output).acquire()
        try:
            capture.ManifestLedger(output, writer_lock=initialization_lock)
        finally:
            initialization_lock.release()
    inspector.inspect_continuity(**inspect_kwargs, initialize_guard=True)
    source = cases.preserved_cases._source()
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    try:
        ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
        receipt = capture.StartEdgeDurabilityReceipt(
            output,
            binding=binding,
            ledger=ledger,
            writer_lock=writer_lock,
        )
        activity = capture.ProspectiveActivityCollector(
            client=capture.BridgeReadClient(
                base_url="http://127.0.0.1:58710",
                api_key=cases.preserved_cases.API_KEY,
                timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
                transport=cases._transport(
                    source,
                    now=t0 + 1,
                    bars=cases._bar_round(source, now=t0 + 1),
                    event_sequence=7,
                ),
            ),
            ledger=ledger,
            binding=binding,
            receipt=receipt,
            policy=capture.CollectionPolicy(),
            clock=cases.preserved_cases.ManualClock(t0 + 1),
        )
        activity.capture_cycle(include_bars=True)
    finally:
        writer_lock.release()
    return {
        "cases": cases,
        "capture": capture,
        "binding": binding,
        "source": source,
        "t0": t0,
        "preregistration": preregistration,
        "producer_paths": producer_paths,
        "output": output,
        "api_key": api_key,
        "inspect_kwargs": inspect_kwargs,
    }


def _append_stranded_active_cycle(fixture: dict[str, object]) -> None:
    cases = fixture["cases"]
    capture = fixture["capture"]
    binding = fixture["binding"]
    source = fixture["source"]
    output = fixture["output"]
    t0 = float(fixture["t0"])
    second_cycle_epoch = (
        t0 + 1.0 + float(capture.MINIMUM_RESERVED_CYCLE_CADENCE_SECONDS)
    )
    assert isinstance(output, Path)
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    try:
        ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
        receipt = capture.StartEdgeDurabilityReceipt(
            output,
            binding=binding,
            ledger=ledger,
            writer_lock=writer_lock,
        )
        activity = capture.ProspectiveActivityCollector(
            client=capture.BridgeReadClient(
                base_url="http://127.0.0.1:58710",
                api_key=cases.preserved_cases.API_KEY,
                timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
                transport=cases._transport(
                    source,
                    now=second_cycle_epoch,
                    bars=cases._bar_round(source, now=second_cycle_epoch),
                    event_sequence=8,
                ),
            ),
            ledger=ledger,
            binding=binding,
            receipt=receipt,
            policy=capture.CollectionPolicy(),
            clock=cases.preserved_cases.ManualClock(second_cycle_epoch),
        )
        activity.capture_cycle(include_bars=False)
    finally:
        writer_lock.release()


def test_gap_v3_supervision_sources_parse_and_are_version_isolated() -> None:
    ast.parse(INSPECTOR.read_text(encoding="utf-8"))
    for path in (GUARD, ENSURE, REGISTER):
        _parse_powershell(path)

    inspector = INSPECTOR.read_text(encoding="utf-8")
    assert "capture_ig_mt4_m1_activity_resilient_v3" in inspector
    assert "check_mt4_tick_volume_collector_continuity_resilient as core" in inspector
    assert "collector-guard.identity.gap-v3.v1.json" in inspector
    assert "fxstack.mtvclc_collector_continuity_inspection.gap_v3.v1" in inspector
    assert "continuity_core_source_sha256" in inspector
    assert "start_edge_durable_receipt_valid" in inspector
    assert "upstream_producer_software_body_sha256" in inspector
    assert "validate_tail_commitment_registry" in inspector
    assert "capture_tail_commitment_proof" in inspector
    assert "late_unseen_epoch_is_permanent_gap" in inspector
    assert "late_unseen_epoch_is_never_backfilled" in inspector
    assert "MAXIMUM_START_EDGE_LAG_SECONDS" in inspector

    guard = GUARD.read_text(encoding="utf-8")
    ensure = ENSURE.read_text(encoding="utf-8")
    register = REGISTER.read_text(encoding="utf-8")
    for source in (guard, ensure, register):
        assert "capture_ig_mt4_m1_activity_resilient_v3.py" in source
        assert "supervision-gap-v3" in (source + guard)
        assert "TradingAgentMtvclcResilientCollector" not in source

    assert "27_guard_mtvclc_collector_resilient.ps1\"" not in ensure
    assert "29_ensure_mtvclc_collector_resilient.ps1\"" not in register
    assert '[string]$TaskName = "TradingAgentMtvclcGapV3Collector"' in register


def test_guard_requires_runtime_source_and_preregistration_digests() -> None:
    source = GUARD.read_text(encoding="utf-8")

    required = (
        "ExpectedCollectorSha256",
        "ExpectedInspectorSha256",
        "ExpectedContinuityCoreSha256",
        "ExpectedPreregistrationArtifactSha256",
        "ExpectedPreregistrationBodySha256",
    )
    for name in required:
        assert f"[string]${name}" in source
    assert source.count('[ValidatePattern("^[0-9a-fA-F]{64}$")]') >= len(required)
    assert "Get-FileSha256 $CollectorPath" in source
    assert "Get-FileSha256 $InspectorPath" in source
    assert "Get-FileSha256 $ContinuityCorePath" in source
    assert "Get-FileSha256 $ResolvedPreregistration" in source
    assert (
        "[string]$inspection.preregistration_body_sha256 -ne "
        "$ExpectedPreregistrationBodySha256"
    ) in source
    assert "continuity_preflight_identity_mismatch" in source
    for producer_option in (
        "BridgeEaRepositorySource",
        "BridgeEaDeployedSource",
        "BridgeEaDeployedEx4",
    ):
        assert f"[string]${producer_option}" in source

    # The digest is supplied at execution/registration time; no collector hash
    # from an unsealed working copy is embedded in the supervisor.
    scrubbed = source.replace("0.000000001", "")
    assert re.search(r'(?i)"[0-9a-f]{64}"', scrubbed) is None


def test_guard_and_watchdog_preserve_single_writer_restart_boundary() -> None:
    guard = GUARD.read_text(encoding="utf-8")
    ensure = ENSURE.read_text(encoding="utf-8")

    assert "Get-CollectorWriterGroups" in guard
    assert "CommandLineToArgvW" in guard
    assert 'if ($writers.Count -gt 1)' in guard
    assert '$status["status"] = "duplicate_writer_groups"' in guard
    assert '$status["status"] = "writer_configuration_mismatch"' in guard
    assert 'Get-OptionValue $arguments "--bridge-ea-repository-source"' in guard
    assert 'Get-OptionValue $arguments "--bridge-ea-deployed-source"' in guard
    assert 'Get-OptionValue $arguments "--bridge-ea-deployed-ex4"' in guard
    assert "[IO.FileShare]::None" in guard
    assert 'reason = "exclusive_gap_v3_writer_lock_unavailable"' in guard
    assert "Invoke-ContinuityInspection -InitializeGuard" in guard
    assert "Assert-PinnedSourceIdentity" in guard
    assert "& $ResolvedPython @collectorArguments" in guard

    inspection_index = guard.rindex("Invoke-ContinuityInspection -InitializeGuard")
    rehash_index = guard.index("Assert-PinnedSourceIdentity", inspection_index)
    collector_index = guard.index("& $ResolvedPython @collectorArguments")
    assert inspection_index < rehash_index < collector_index

    assert 'New-GuardArguments "Health"' in ensure
    assert 'New-GuardArguments "StartOrResume"' in ensure
    health_index = ensure.index('New-GuardArguments "Health"')
    proof_index = ensure.index("$restartAllowed =")
    start_index = ensure.index('New-GuardArguments "StartOrResume"')
    assert health_index < proof_index < start_index
    assert "$health.ExitCode -eq 3" in ensure
    assert '@("stopped_before_t0", "stopped_during_window")' in ensure
    assert '[string]$report.reason -eq "collector_writer_absent"' in ensure
    assert "[int]$report.writer_group_count -eq 0" in ensure
    assert '@("absent", "available") -contains $lockState' in ensure
    assert "Test-ExactGuardIdentity" in ensure
    assert "-WindowStyle Hidden" in ensure
    assert '"-BridgeEaRepositorySource", $ResolvedBridgeEaRepositorySource' in ensure
    assert '"-BridgeEaDeployedSource", $ResolvedBridgeEaDeployedSource' in ensure
    assert '"-BridgeEaDeployedEx4", $ResolvedBridgeEaDeployedEx4' in ensure

    lowered_guard = guard.lower()
    lowered_ensure = ensure.lower()
    for forbidden in ("stop-process", "taskkill", "register-scheduledtask"):
        assert forbidden not in lowered_guard
        assert forbidden not in lowered_ensure


def test_synthetic_v4_is_accepted_by_gap_v3_watchdog_guard_contract(
    tmp_path: Path,
) -> None:
    payload, preregistration, deployed_source, deployed_ex4 = (
        _synthetic_v4_preregistration(tmp_path)
    )
    body_sha256 = str(payload["preregistration_body_sha256"])
    output = tmp_path / "synthetic-v4-capture"
    output.mkdir()
    api_key_file = tmp_path / "synthetic-key-file.txt"
    api_key_sentinel = "test-only-key-bytes-must-not-be-read"
    api_key_file.write_text(api_key_sentinel, encoding="utf-8")

    binding = inspector.collector_v3.load_preregistration(
        preregistration,
        bridge_ea_repository_source=BRIDGE_EA_REPOSITORY,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
    )
    inspection = inspector.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key_file,
        bridge_ea_repository_source=BRIDGE_EA_REPOSITORY,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
        base_url="http://127.0.0.1:58710",
        policy=inspector.GuardPolicy(
            tick_interval_secs=2.0,
            bar_interval_secs=60.0,
            bar_limit=400,
            http_timeout_secs=5.0,
        ),
    )

    assert binding.preregistration_body_sha256 == body_sha256
    assert binding.preregistration_artifact_sha256 == _sha256(preregistration)
    assert inspection["status"] == "continuity_preflight_passed"
    assert inspection["preregistration_body_sha256"] == body_sha256
    assert inspection["collector_source_sha256"] == _sha256(COLLECTOR)
    assert inspection["guard_identity_present"] is False
    assert inspection["collection_only"] is True
    assert inspection["runtime_authorized"] is False
    assert inspection["immediate_market_trade_authorized"] is False

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(GUARD),
            "-Action",
            "Health",
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key_file),
            "-BridgeEaRepositorySource",
            str(BRIDGE_EA_REPOSITORY),
            "-BridgeEaDeployedSource",
            str(deployed_source),
            "-BridgeEaDeployedEx4",
            str(deployed_ex4),
            "-ExpectedCollectorSha256",
            _sha256(COLLECTOR),
            "-ExpectedInspectorSha256",
            _sha256(INSPECTOR),
            "-ExpectedContinuityCoreSha256",
            _sha256(CORE),
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            body_sha256,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )

    assert completed.returncode == 3, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "stopped_before_t0"
    assert report["reason"] == "collector_writer_absent"
    assert report["writer_group_count"] == 0
    assert report["preregistration_body_sha256"] == body_sha256
    assert report["preregistration_artifact_sha256"] == _sha256(
        preregistration
    )
    assert report["collector_source_sha256"] == _sha256(COLLECTOR)
    assert report["continuity_inspector_source_sha256"] == _sha256(INSPECTOR)
    assert report["continuity_core_source_sha256"] == _sha256(CORE)
    assert report["collection_only"] is True
    assert report["runtime_authorized"] is False
    assert report["immediate_market_trade_authorized"] is False
    assert api_key_sentinel not in completed.stdout
    assert api_key_sentinel not in completed.stderr


def test_guard_completion_fails_closed_without_receipt_or_with_active_journal() -> None:
    source = GUARD.read_text(encoding="utf-8")

    active_index = source.index(
        'if ([bool]$inspection.active_journal_present)',
        source.index('if ($now -ge $end)'),
    )
    receipt_index = source.index(
        "-not [bool]$inspection.start_edge_durable_receipt_valid",
        active_index,
    )
    tail_index = source.index(
        'validated_finalized_tail_commitment_proof_required',
        active_index,
    )
    complete_index = source.index(
        '$status["status"] = "prospective_window_complete"',
        receipt_index,
    )
    assert active_index < tail_index < receipt_index < complete_index
    assert '"post_window_finalization_required"' in source
    assert '"active_journal_requires_integrity_only_finalization"' in source
    assert '"validated_start_edge_durable_receipt_required"' in source
    assert '[string]$tailProof.committed_state_kind -ne "manifest"' in source


def test_supervision_has_no_authority_runtime_or_trade_launch_path() -> None:
    for path in (INSPECTOR, GUARD, ENSURE, REGISTER):
        source = path.read_text(encoding="utf-8").lower()
        assert "private_key" not in source
        assert "ed25519" not in source
        assert "21_start_runtime" not in source
        assert "/v2/commands" not in source
        assert "execution_egress_control" not in source
        assert "stop-scheduledtask" not in source
        assert "immediate_market_trade_authorized" in source or path == INSPECTOR

    for path in (INSPECTOR, GUARD, ENSURE):
        source = path.read_text(encoding="utf-8").lower()
        assert "start-scheduledtask" not in source
    registrar = REGISTER.read_text(encoding="utf-8").lower()
    assert registrar.count("start-scheduledtask") == 1

    ensure = ENSURE.read_text(encoding="utf-8")
    for field in (
        "evaluation_performed",
        "signal_computation_authorized",
        "outcome_access_authorized",
        "performance_computation_authorized",
        "success_claim_authorized",
        "issuer_authorized",
        "signature_authorized",
        "authority_granted",
        "runtime_authorized",
        "activation_authorized",
        "order_authorized",
        "immediate_market_trade_authorized",
    ):
        assert field in ensure


def test_task_preview_pins_exact_gap_v3_tuple_without_mutation(tmp_path: Path) -> None:
    api_key = tmp_path / "key.txt"
    output = tmp_path / "capture"
    body_sha256 = "a" * 64
    preregistration, deployed_source, deployed_ex4 = _write_preview_inputs(
        tmp_path,
        body_sha256=body_sha256,
    )
    api_key.write_text("preview-must-not-read-this-value", encoding="utf-8")
    output.mkdir()

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REGISTER),
            "-Action",
            "Preview",
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key),
            "-BridgeEaRepositorySource",
            str(BRIDGE_EA_REPOSITORY),
            "-BridgeEaDeployedSource",
            str(deployed_source),
            "-BridgeEaDeployedEx4",
            str(deployed_ex4),
            "-ExpectedCollectorSha256",
            _sha256(COLLECTOR),
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            body_sha256,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    preview = json.loads(completed.stdout)
    assert preview["task_name"] == "TradingAgentMtvclcGapV3Collector"
    assert preview["trigger_mode"] == "AtLogOn"
    assert preview["multiple_instances"] == "IgnoreNew"
    assert preview["mutation_performed"] is False
    assert preview["collector_source_sha256"] == _sha256(COLLECTOR)
    assert preview["preregistration_artifact_sha256"] == _sha256(preregistration)
    assert preview["preregistration_body_sha256"] == body_sha256
    assert preview["output_root"] == str(output.resolve())
    assert preview["continuity_inspector_source_sha256"] == _sha256(INSPECTOR)
    assert preview["continuity_core_source_sha256"] == _sha256(CORE)
    assert preview["bridge_ea_repository_source_path"] == str(
        BRIDGE_EA_REPOSITORY.resolve()
    )
    assert preview["bridge_ea_deployed_source_path"] == str(deployed_source.resolve())
    assert preview["bridge_ea_deployed_ex4_path"] == str(deployed_ex4.resolve())
    for option in (
        "-ExpectedCollectorSha256",
        "-ExpectedInspectorSha256",
        "-ExpectedContinuityCoreSha256",
        "-ExpectedPreregistrationArtifactSha256",
        "-ExpectedPreregistrationBodySha256",
        "-ExpectedWatchdogSha256",
        "-ExpectedGuardSha256",
        "-BridgeEaRepositorySource",
        "-BridgeEaDeployedSource",
        "-BridgeEaDeployedEx4",
    ):
        assert option in preview["arguments"]
    assert "preview-must-not-read-this-value" not in completed.stdout


def test_continuity_guard_binds_final_v3_collector_wrapper_and_base(
    tmp_path: Path,
) -> None:
    cases = _load_v3_collector_cases()
    capture = inspector.collector_v3
    preregistration = cases._write_preregistration(tmp_path / "candidate.json")
    producer_paths = cases._producer_paths(tmp_path / "deployed")
    output = tmp_path / "capture"
    output.mkdir()
    api_key = tmp_path / "key.txt"
    api_key.write_text("metadata-only-credential", encoding="utf-8")
    policy = inspector.GuardPolicy(
        tick_interval_secs=2.0,
        bar_interval_secs=60.0,
        bar_limit=400,
        http_timeout_secs=5.0,
    )

    initialized = inspector.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
        base_url="http://127.0.0.1:58710",
        policy=policy,
        initialize_guard=True,
    )
    required = inspector.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
        base_url="http://127.0.0.1:58710",
        policy=policy,
        require_guard=True,
    )

    assert initialized["guard_identity_present"] is True
    assert required["guard_identity_sha256"] == initialized["guard_identity_sha256"]
    assert initialized["collector_source_sha256"] == _sha256(COLLECTOR)
    assert initialized["collector_wrapper_source_sha256"] == inspector.collector_v3.SUPPORT_SHA256
    assert initialized["collector_base_source_sha256"] == inspector.collector_v3.BASE_SUPPORT_SHA256
    assert initialized["start_edge_durable_receipt_present"] is False
    assert initialized["start_edge_durable_receipt_valid"] is False
    assert initialized["capture_tail_commitment_proof"] is None
    assert initialized["capture_tail_commitment_contract"] == {
        "schema_version": capture.TAIL_COMMITMENT_SCHEMA_VERSION,
        "path": str(output / capture.TAIL_COMMITMENT_FILENAME),
        "validator_api": "validate_tail_commitment_registry",
        "collector_source_sha256": capture.MODULE_SOURCE_SHA256,
        "tail_proof_required_after_collector_initialization": True,
        "dynamic_tail_proof_embedded_in_immutable_identity": False,
    }
    assert initialized["bridge_ea_repository_source_path"] == str(producer_paths[0])
    assert initialized["bridge_ea_deployed_source_path"] == str(producer_paths[1])
    assert initialized["bridge_ea_deployed_ex4_path"] == str(producer_paths[2])
    assert initialized["immediate_market_trade_authorized"] is False
    assert initialized["runtime_authorized"] is False

    source = cases.preserved_cases._source()
    writer_lock = capture.ExclusiveDataWriterLock(output).acquire()
    ledger = capture.ManifestLedger(output, writer_lock=writer_lock)
    binding = capture.load_preregistration(
        preregistration,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
    )
    receipt = capture.StartEdgeDurabilityReceipt(
        output,
        binding=binding,
        ledger=ledger,
        writer_lock=writer_lock,
    )
    activity = capture.ProspectiveActivityCollector(
        client=capture.BridgeReadClient(
            base_url="http://127.0.0.1:58710",
            api_key=cases.preserved_cases.API_KEY,
            timeout_secs=capture.DEFAULT_HTTP_TIMEOUT_SECS,
            transport=cases._transport(
                source,
                now=cases.NOW,
                bars=cases._bar_round(source),
                event_sequence=7,
            ),
        ),
        ledger=ledger,
        binding=binding,
        receipt=receipt,
        policy=capture.CollectionPolicy(),
        clock=cases.preserved_cases.ManualClock(cases.NOW + 1),
    )
    activity.capture_cycle(include_bars=True)
    writer_lock.release()

    captured = inspector.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        bridge_ea_repository_source=producer_paths[0],
        bridge_ea_deployed_source=producer_paths[1],
        bridge_ea_deployed_ex4=producer_paths[2],
        base_url="http://127.0.0.1:58710",
        policy=policy,
        require_guard=True,
    )
    assert captured["manifest_present"] is True
    assert captured["manifest_sequence"] == 1
    assert captured["manifest_last_market_source_id"] == source.source_id
    assert captured["active_journal_present"] is False
    assert captured["start_edge_durable_receipt_present"] is True
    assert captured["start_edge_durable_receipt_valid"] is True
    assert len(captured["start_edge_durable_receipt_sha256"]) == 64
    assert captured["start_edge_durable_receipt_validation_side_effect_free"] is True
    tail_proof = captured["capture_tail_commitment_proof"]
    assert tail_proof == capture.validate_tail_commitment_registry(output)
    assert tail_proof["committed_state_kind"] == "manifest"
    assert tail_proof["physical_journal_present"] is False


def test_continuity_refuses_missing_start_edge_receipt_after_manifest(
    tmp_path: Path,
) -> None:
    fixture = _capture_fixture(tmp_path)
    capture = fixture["capture"]
    output = fixture["output"]
    assert isinstance(output, Path)
    receipt = output / capture.START_EDGE_RECEIPT_FILENAME
    receipt.unlink()

    with pytest.raises(
        inspector.ContinuityRefusal,
        match="start_edge_durable_receipt_missing",
    ):
        inspector.inspect_continuity(**fixture["inspect_kwargs"], require_guard=True)


def test_continuity_refuses_invalid_start_edge_receipt_after_manifest(
    tmp_path: Path,
) -> None:
    fixture = _capture_fixture(tmp_path)
    capture = fixture["capture"]
    output = fixture["output"]
    assert isinstance(output, Path)
    receipt = output / capture.START_EDGE_RECEIPT_FILENAME
    receipt.write_text("{}\n", encoding="utf-8")

    with pytest.raises(
        inspector.ContinuityRefusal,
        match="start_edge_durable_receipt_(?:invalid|not_canonical)",
    ):
        inspector.inspect_continuity(**fixture["inspect_kwargs"], require_guard=True)


def test_continuity_refuses_missing_tail_commitment_after_capture(
    tmp_path: Path,
) -> None:
    fixture = _capture_fixture(tmp_path)
    capture = fixture["capture"]
    output = fixture["output"]
    assert isinstance(output, Path)
    (output / capture.TAIL_COMMITMENT_FILENAME).unlink()

    with pytest.raises(
        inspector.ContinuityRefusal,
        match="tail_commitment_proof_required",
    ):
        inspector.inspect_continuity(**fixture["inspect_kwargs"], require_guard=True)


def test_continuity_refuses_valid_prefix_tail_commitment_rollback(
    tmp_path: Path,
) -> None:
    fixture = _capture_fixture(tmp_path)
    capture = fixture["capture"]
    output = fixture["output"]
    assert isinstance(output, Path)
    tail = output / capture.TAIL_COMMITMENT_FILENAME
    lines = tail.read_bytes().splitlines(keepends=True)
    assert len(lines) > 1
    tail.write_bytes(b"".join(lines[:-1]))

    with pytest.raises(inspector.ContinuityRefusal, match="tail_commitment"):
        inspector.inspect_continuity(**fixture["inspect_kwargs"], require_guard=True)


def test_post_end_stranded_active_journal_is_nonzero_finalization_required(
    tmp_path: Path,
) -> None:
    fixture = _capture_fixture(tmp_path, window_already_ended=True)
    _append_stranded_active_cycle(fixture)
    inspection = inspector.inspect_continuity(
        **fixture["inspect_kwargs"],
        require_guard=True,
    )
    assert inspection["active_journal_present"] is True
    assert inspection["start_edge_durable_receipt_valid"] is True

    preregistration = fixture["preregistration"]
    producer_paths = fixture["producer_paths"]
    output = fixture["output"]
    api_key = fixture["api_key"]
    assert isinstance(preregistration, Path)
    assert isinstance(output, Path)
    assert isinstance(api_key, Path)
    payload = json.loads(preregistration.read_text(encoding="utf-8"))
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(GUARD),
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
            "-BridgeEaRepositorySource",
            str(producer_paths[0]),
            "-BridgeEaDeployedSource",
            str(producer_paths[1]),
            "-BridgeEaDeployedEx4",
            str(producer_paths[2]),
            "-ExpectedCollectorSha256",
            _sha256(COLLECTOR),
            "-ExpectedInspectorSha256",
            _sha256(INSPECTOR),
            "-ExpectedContinuityCoreSha256",
            _sha256(CORE),
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            payload["preregistration_body_sha256"],
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )

    assert completed.returncode == 3, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "post_window_finalization_required"
    assert report["reason"] == "active_journal_requires_integrity_only_finalization"
    assert report["active_journal_present"] is True
    assert report["start_edge_durable_receipt_valid"] is True


@pytest.mark.parametrize(
    "registrar",
    DURABLE_TASK_START_REGISTRARS,
    ids=lambda path: path.stem,
)
def test_registrar_has_one_explicit_exact_task_scheduler_start_boundary(
    registrar: Path,
) -> None:
    source = registrar.read_text(encoding="utf-8")
    assert '[ValidateSet("Install", "Preview", "Start", "Remove")]' in source
    assert '[string]$Action = "Preview"' in source
    assert '[ValidateSet("AtLogOn", "AtStartup")]' in source
    assert "[ValidateRange(30, 300)]" in source
    assert "[int]$StartConfirmationSeconds = 60" in source
    assert "Unregister-ScheduledTask" in source
    assert "scheduled_task_identity_mismatch_refusing_remove" in source
    assert "scheduled_task_already_exists_remove_it_explicitly_before_install" in source
    assert "Register-ScheduledTask" in source
    assert "-MultipleInstances IgnoreNew" in source
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in source
    assert "exact_collector_preregistration_and_producer_identity_required" in source
    assert "Get-ExactScheduledTaskDiagnostics" in source
    assert "Assert-DurableLaunchSourceIdentity" in source
    assert "Test-GuardAuthorityEnvelope" in source
    assert "Test-ExactGuardIdentity" in source
    assert "Test-HealthyLockedWriter" in source
    assert 'reason = "scheduled_task_absent_refusing_start"' in source
    assert 'reason = "scheduled_task_exact_identity_mismatch_refusing_start"' in source
    assert "$restartAllowed =" in source
    assert 'reason = "guard_health_did_not_prove_restartable_absent_writer"' in source
    assert "Start-ScheduledTask" in source
    assert "Get-ScheduledTaskInfo" in source
    assert "$preStartLastRunTime" in source
    assert "$confirmedLastRunTime -gt $preStartLastRunTime" in source
    assert 'reason = "scheduled_task_run_not_observed_after_request"' in source
    assert "$postTaskCompletionHealthySamples" in source
    assert 'if ($confirmedTaskState -ne "Ready")' in source
    assert "[int64]$confirmedTaskInfo.LastTaskResult -ne 0" in source
    assert 'status = "task_scheduler_owned_restart_confirmed"' in source
    assert "task_identity_reconfirmed = $true" in source

    start_branch = source.index('if ($Action -eq "Start")')
    exact_identity = source.index("Get-ExactScheduledTaskDiagnostics", start_branch)
    health_preflight = source.index("Invoke-DirectGuardHealth", exact_identity)
    absent_proof = source.index("$restartAllowed =", health_preflight)
    task_run_preflight = source.index("$preStartTaskInfo", absent_proof)
    scheduler_start = source.index("Start-ScheduledTask", task_run_preflight)
    bounded_confirmation = source.index("$confirmationDeadline", scheduler_start)
    health_confirmation = source.index(
        "Invoke-DirectGuardHealth", bounded_confirmation
    )
    identity_reconfirmation = source.index(
        "Get-ExactScheduledTaskDiagnostics", health_confirmation
    )
    task_completion = source.index(
        'if ($confirmedTaskState -ne "Ready")', identity_reconfirmation
    )
    second_healthy_sample = source.index(
        "$postTaskCompletionHealthySamples -lt 2", task_completion
    )
    confirmed = source.index(
        'status = "task_scheduler_owned_restart_confirmed"',
        second_healthy_sample,
    )
    assert (
        exact_identity
        < health_preflight
        < absent_proof
        < task_run_preflight
        < scheduler_start
        < bounded_confirmation
        < health_confirmation
        < identity_reconfirmation
        < task_completion
        < second_healthy_sample
        < confirmed
    )

    lowered = source.lower()
    assert lowered.count("start-scheduledtask") == 1
    assert "stop-scheduledtask" not in lowered
    assert "start-process" not in lowered
    assert "stop-process" not in lowered
    assert "taskkill" not in lowered
    register_call = source[source.index("Register-ScheduledTask") :]
    assert re.search(r"Register-ScheduledTask[^\r\n]*-Force", register_call) is None


def test_task_start_refuses_when_exact_scheduled_task_is_absent(
    tmp_path: Path,
) -> None:
    api_key = tmp_path / "key.txt"
    output = tmp_path / "capture"
    body_sha256 = "e" * 64
    preregistration, deployed_source, deployed_ex4 = _write_preview_inputs(
        tmp_path,
        body_sha256=body_sha256,
    )
    key_sentinel = "start-refusal-must-not-read-this-key-value"
    api_key.write_text(key_sentinel, encoding="utf-8")
    output.mkdir()
    task_name = (
        "FxStackMissingGapV3Test"
        + hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]
    )

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REGISTER),
            "-Action",
            "Start",
            "-TaskName",
            task_name,
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key),
            "-BridgeEaRepositorySource",
            str(BRIDGE_EA_REPOSITORY),
            "-BridgeEaDeployedSource",
            str(deployed_source),
            "-BridgeEaDeployedEx4",
            str(deployed_ex4),
            "-ExpectedCollectorSha256",
            _sha256(COLLECTOR),
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            body_sha256,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )

    assert completed.returncode == 4, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "task_start_refused"
    assert report["reason"] == "scheduled_task_absent_refusing_start"
    assert report["mutation_performed"] is False
    assert report["runtime_authorized"] is False
    assert report["immediate_market_trade_authorized"] is False
    assert key_sentinel not in completed.stdout
    assert key_sentinel not in completed.stderr


def test_task_preview_refuses_a_stale_collector_digest(tmp_path: Path) -> None:
    api_key = tmp_path / "key.txt"
    output = tmp_path / "capture"
    body_sha256 = "b" * 64
    preregistration, deployed_source, deployed_ex4 = _write_preview_inputs(
        tmp_path,
        body_sha256=body_sha256,
    )
    api_key.write_text("not-read", encoding="utf-8")
    output.mkdir()

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REGISTER),
            "-Action",
            "Preview",
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key),
            "-BridgeEaRepositorySource",
            str(BRIDGE_EA_REPOSITORY),
            "-BridgeEaDeployedSource",
            str(deployed_source),
            "-BridgeEaDeployedEx4",
            str(deployed_ex4),
            "-ExpectedCollectorSha256",
            "0" * 64,
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            body_sha256,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "collector_source_identity_mismatch" in completed.stderr
    assert completed.stdout.strip() == ""


def test_task_preview_refuses_deployed_bridge_ea_source_drift(tmp_path: Path) -> None:
    api_key = tmp_path / "key.txt"
    output = tmp_path / "capture"
    body_sha256 = "d" * 64
    preregistration, deployed_source, deployed_ex4 = _write_preview_inputs(
        tmp_path,
        body_sha256=body_sha256,
    )
    api_key.write_text("not-read", encoding="utf-8")
    output.mkdir()
    deployed_source.write_bytes(deployed_source.read_bytes() + b"\n// drift\n")

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(REGISTER),
            "-Action",
            "Preview",
            "-PythonExe",
            sys.executable,
            "-Preregistration",
            str(preregistration),
            "-OutputDir",
            str(output),
            "-ApiKeyFile",
            str(api_key),
            "-BridgeEaRepositorySource",
            str(BRIDGE_EA_REPOSITORY),
            "-BridgeEaDeployedSource",
            str(deployed_source),
            "-BridgeEaDeployedEx4",
            str(deployed_ex4),
            "-ExpectedCollectorSha256",
            _sha256(COLLECTOR),
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            body_sha256,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )

    assert completed.returncode != 0
    assert "bridge_ea_producer_identity_mismatch" in completed.stderr
    assert completed.stdout.strip() == ""
