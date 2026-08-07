from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v4.py"
COLLECTOR_TEMPLATE = (
    ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"
)
INSPECTOR = (
    ROOT / "tools" / "check_mt4_tick_volume_collector_continuity_resilient_v3.py"
)
CORE = ROOT / "tools" / "check_mt4_tick_volume_collector_continuity_resilient.py"
GUARD = ROOT / "ops" / "windows" / "27_guard_mtvclc_collector_resilient_v3.ps1"
ENSURE = (
    ROOT / "ops" / "windows" / "29_ensure_mtvclc_collector_resilient_v3.ps1"
)
REGISTER = (
    ROOT
    / "ops"
    / "windows"
    / "29_register_mtvclc_collector_resilient_watchdog_v3.ps1"
)
V5_SEALER = (
    ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v5.py"
)
BASE_SEALER_TEST = (
    ROOT
    / "fx-quant-stack"
    / "tests"
    / "test_seal_mt4_tick_volume_preregistration.py"
)
BRIDGE_EA_REPOSITORY = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _identity(path: Path) -> dict[str, object]:
    raw = path.read_bytes()
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }


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


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    specification = importlib.util.spec_from_file_location(name, path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[name] = module
    specification.loader.exec_module(module)
    return module


def _preview_inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path, str]:
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(BRIDGE_EA_REPOSITORY.read_bytes())
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"test-only compiled BridgeEA identity")
    body_sha256 = "a" * 64
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
                    "producer_software_body_sha256": "b" * 64,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    output = tmp_path / "capture"
    output.mkdir()
    api_key = tmp_path / "key.txt"
    api_key.write_text("preview-secret-must-not-be-read", encoding="utf-8")
    return preregistration, output, api_key, deployed_source, deployed_ex4, body_sha256


def _synthetic_v5_tuple(
    tmp_path: Path,
) -> tuple[dict[str, object], Path, Path, Path, Path, Path]:
    sealer = _load("_mtvclc_supervision_v5_sealer", V5_SEALER)
    base_test = _load("_mtvclc_supervision_base_sealer_test", BASE_SEALER_TEST)
    inputs_root = tmp_path / "sealed-inputs"
    inputs_root.mkdir()
    cost_capture, cost_samples, fee_attestation = base_test._inputs(inputs_root)
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    deployed_source = deployed / "BridgeEA.mq4"
    deployed_source.write_bytes(BRIDGE_EA_REPOSITORY.read_bytes())
    deployed_ex4 = deployed / "BridgeEA.ex4"
    deployed_ex4.write_bytes(b"test-only compiled BridgeEA identity")
    payload = sealer.build_preregistration(
        cost_capture_json=cost_capture,
        cost_capture_npz=cost_samples,
        fee_attestation=fee_attestation,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
        sealed_at=datetime.now(UTC).replace(microsecond=0),
        start_delay_seconds=3600,
    )
    body_sha256 = str(payload["preregistration_body_sha256"])
    preregistration = (
        tmp_path / f"mtvclc_gap_v3_preregistration_{body_sha256}.json"
    )
    preregistration.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "capture"
    output.mkdir()
    api_key = tmp_path / "key.txt"
    api_key.write_text("health-secret-must-not-be-read", encoding="utf-8")
    return payload, preregistration, output, api_key, deployed_source, deployed_ex4


def test_v5_sources_parse_and_bind_distinct_full_lifetime_contract() -> None:
    ast.parse(INSPECTOR.read_text(encoding="utf-8"))
    for path in (GUARD, ENSURE, REGISTER):
        _parse_powershell(path)

    inspector_source = INSPECTOR.read_text(encoding="utf-8")
    assert "capture_ig_mt4_m1_activity_resilient_v4" in inspector_source
    assert "_fxstack_mtvclc_continuity_core_gap_v5_v1" in inspector_source
    assert "collector-guard.identity.gap-v5.v1.json" in inspector_source
    assert "fxstack.mtvclc_collector_guard_identity.gap_v5.v1" in inspector_source
    assert "collector_template_source_sha256" in inspector_source
    assert ".read_bytes()" not in inspector_source

    guard_source = GUARD.read_text(encoding="utf-8")
    ensure_source = ENSURE.read_text(encoding="utf-8")
    register_source = REGISTER.read_text(encoding="utf-8")
    for source in (guard_source, ensure_source, register_source):
        assert "capture_ig_mt4_m1_activity_resilient_v4.py" in source
        assert "capture_ig_mt4_m1_activity_resilient_v3.py" in source
        assert "supervision-gap-v5" in (source + guard_source)
        assert "ExpectedCollectorTemplateSha256" in source

    assert "Start-Process" not in ensure_source
    assert 'New-GuardArguments "AdoptRunning"' not in ensure_source
    assert '$guardAction = "AdoptRunning"' in ensure_source
    assert 'status = "detached_supervisor_detected"' in ensure_source
    assert 'status = "orphan_writer_adoption_allowed"' not in ensure_source
    assert 'launchStatus = "orphan_writer_adoption_allowed"' in ensure_source
    assert "& $PowerShellHost @startArguments" in ensure_source
    assert '"guard.supervisor.stdout.log"' in ensure_source
    assert '"guard.supervisor.stderr.log"' in ensure_source
    assert "} $guardExitCode" in ensure_source

    lowered = register_source.lower()
    assert "$registeredtask.getinstances(0)" in lowered
    assert "instanceguid" in lowered
    assert "enginepid" in lowered
    assert "test-exacttaskactionprocess" in lowered
    assert "test-processdescendsfrom" in lowered
    assert "get-exactguardancestorpid" in lowered
    assert 'state -ne 4' in lowered
    assert "task_scheduler_full_lifetime_ownership_confirmed" in lowered
    assert "separated_healthy_owned_samples = 2" in lowered
    assert "live_confirmation_used_last_task_result = $false" in lowered
    assert "orphaned_writer_not_task_owned" in lowered
    assert "timeout_did_not_stop_task_or_process = $true" in lowered
    assert "lasttaskresult -ne 0" not in lowered
    assert "stop-scheduledtask" not in lowered
    assert "stop-process" not in lowered
    assert "taskkill" not in lowered
    assert lowered.count("start-scheduledtask") == 1


def test_private_v5_inspector_never_mutates_canonical_core_globals() -> None:
    canonical = importlib.import_module(
        "tools.check_mt4_tick_volume_collector_continuity_resilient"
    )
    before = (
        canonical.collector,
        canonical.TOOL_PATH,
        canonical.GUARD_SCHEMA_VERSION,
        canonical.INSPECTION_SCHEMA_VERSION,
        canonical.GUARD_IDENTITY_FILENAME,
    )
    successor = importlib.import_module(
        "tools.check_mt4_tick_volume_collector_continuity_resilient_v3"
    )
    after = (
        canonical.collector,
        canonical.TOOL_PATH,
        canonical.GUARD_SCHEMA_VERSION,
        canonical.INSPECTION_SCHEMA_VERSION,
        canonical.GUARD_IDENTITY_FILENAME,
    )
    assert before == after
    assert successor.core is not canonical
    assert successor.core.__name__ == "_fxstack_mtvclc_continuity_core_gap_v5_v1"

    reverse_order = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from tools import check_mt4_tick_volume_collector_continuity_resilient_v3 as v3;"
                "from tools import check_mt4_tick_volume_collector_continuity_resilient as v1;"
                "assert v3.core is not v1;"
                "assert v1.GUARD_IDENTITY_FILENAME == "
                "'collector-guard.identity.resilient.v1.json'"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert reverse_order.returncode == 0, reverse_order.stderr


def test_v5_task_preview_pins_adapter_template_and_does_not_read_key(
    tmp_path: Path,
) -> None:
    preregistration, output, api_key, deployed_source, deployed_ex4, body = (
        _preview_inputs(tmp_path)
    )
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
            body,
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    preview = json.loads(completed.stdout)
    assert preview["task_name"] == "TradingAgentMtvclcV5Collector"
    assert preview["collector_source_sha256"] == _sha256(COLLECTOR)
    assert preview["collector_template_source_sha256"] == _sha256(
        COLLECTOR_TEMPLATE
    )
    assert preview["continuity_inspector_source_sha256"] == _sha256(INSPECTOR)
    assert preview["continuity_core_source_sha256"] == _sha256(CORE)
    assert preview["multiple_instances"] == "IgnoreNew"
    assert preview["execution_time_limit_seconds"] == 0
    assert preview["mutation_performed"] is False
    assert "-WindowStyle Hidden" in preview["arguments"]
    assert "-ExpectedCollectorTemplateSha256" in preview["arguments"]
    assert "preview-secret-must-not-be-read" not in completed.stdout
    assert "preview-secret-must-not-be-read" not in completed.stderr


def test_v5_inspector_and_guard_health_use_exact_gap_v5_identity(
    tmp_path: Path,
) -> None:
    payload, preregistration, output, api_key, deployed_source, deployed_ex4 = (
        _synthetic_v5_tuple(tmp_path)
    )
    inspector = importlib.import_module(
        "tools.check_mt4_tick_volume_collector_continuity_resilient_v3"
    )
    policy = inspector.GuardPolicy(
        tick_interval_secs=2.0,
        bar_interval_secs=60.0,
        bar_limit=400,
        http_timeout_secs=5.0,
    )
    report = inspector.inspect_continuity(
        preregistration=preregistration,
        output_dir=output,
        api_key_file=api_key,
        bridge_ea_repository_source=BRIDGE_EA_REPOSITORY,
        bridge_ea_deployed_source=deployed_source,
        bridge_ea_deployed_ex4=deployed_ex4,
        base_url="http://127.0.0.1:58710",
        policy=policy,
        initialize_guard=True,
    )
    assert report["schema_version"] == (
        "fxstack.mtvclc_collector_continuity_inspection.gap_v5.v1"
    )
    assert report["collector_source_sha256"] == _sha256(COLLECTOR)
    assert report["collector_template_source_sha256"] == _sha256(
        COLLECTOR_TEMPLATE
    )
    guard_identity = output / "collector-guard.identity.gap-v5.v1.json"
    assert guard_identity.is_file()
    assert not (output / "collector-guard.identity.gap-v3.v1.json").exists()

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
            str(BRIDGE_EA_REPOSITORY),
            "-BridgeEaDeployedSource",
            str(deployed_source),
            "-BridgeEaDeployedEx4",
            str(deployed_ex4),
            "-ExpectedCollectorSha256",
            _sha256(COLLECTOR),
            "-ExpectedCollectorTemplateSha256",
            _sha256(COLLECTOR_TEMPLATE),
            "-ExpectedInspectorSha256",
            _sha256(INSPECTOR),
            "-ExpectedContinuityCoreSha256",
            _sha256(CORE),
            "-ExpectedPreregistrationArtifactSha256",
            _sha256(preregistration),
            "-ExpectedPreregistrationBodySha256",
            str(payload["preregistration_body_sha256"]),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=60,
    )
    assert completed.returncode == 3, completed.stderr
    health = json.loads(completed.stdout)
    assert health["schema_version"] == (
        "fxstack.mtvclc_collector_guard_status.gap_v5.v1"
    )
    assert health["status"] == "stopped_before_t0"
    assert health["writer_group_count"] == 0
    assert health["supervisor_lock_held"] is False
    assert health["collector_source_sha256"] == _sha256(COLLECTOR)
    assert health["collector_template_source_sha256"] == _sha256(
        COLLECTOR_TEMPLATE
    )
    assert health["immediate_market_trade_authorized"] is False
    assert "health-secret-must-not-be-read" not in completed.stdout
    assert "health-secret-must-not-be-read" not in completed.stderr
