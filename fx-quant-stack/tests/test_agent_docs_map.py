from __future__ import annotations

import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "docs" / "agents"
SYSTEM_MAP = DOCS_ROOT / "system-map.yaml"
AGENTS_MD = REPO_ROOT / "AGENTS.md"
TIER1_FILES = [
    REPO_ROOT / "fx-quant-stack/src/fxstack/runtime/runner.py",
    REPO_ROOT / "fx-quant-stack/src/fxstack/runtime/service.py",
    REPO_ROOT / "fx-quant-stack/src/fxstack/runtime/postgres_store.py",
    REPO_ROOT / "fx-quant-stack/src/fxstack/api/app.py",
    REPO_ROOT / "fx-quant-stack/src/fxstack/live/scorer.py",
    REPO_ROOT / "fx-quant-stack/src/fxstack/live/policy.py",
    REPO_ROOT / "fx-quant-stack/src/fxstack/backtest/adaptive_policy.py",
    REPO_ROOT / "tools/fxstack_digital_twin_backtest.py",
    REPO_ROOT / "app/api/trading/state/route.ts",
    REPO_ROOT / "lib/hooks/use-live-bridge-state.ts",
    REPO_ROOT / "components/live-signals.tsx",
    REPO_ROOT / "ops/windows/21_start_runtime.bat",
    REPO_ROOT / "ops/windows/_env.bat",
]
WSL_RESET_FILES = {
    REPO_ROOT / "ops/windows/90_stop_all.bat": [
        "wsl.exe",
        "src\\.trader\\.cli bridge serve",
        "src\\.trader\\.cli runtime run",
        "src\\.trader\\.cli monitor confidence",
        "next start -p",
        ".next.*standalone.*server\\.js",
    ],
    REPO_ROOT / "ops/windows/20_start_bridge.bat": [
        "wsl.exe",
        "src\\.trader\\.cli bridge serve.*--port %TARGET_PORT%",
    ],
    REPO_ROOT / "ops/windows/21_start_runtime.bat": [
        "wsl.exe",
        "src\\.trader\\.cli runtime run",
    ],
    REPO_ROOT / "ops/windows/22_start_dashboard.bat": [
        "wsl.exe",
        "next start -p %TARGET_PORT%",
        ".next.*standalone.*server\\.js",
    ],
}
HEADER_FIELDS = [
    "ROLE:",
    "ENTRYPOINT:",
    "PRIMARY INPUTS:",
    "PRIMARY OUTPUTS:",
    "DEPENDS ON:",
    "CALLED BY:",
    "STATE / SIDE EFFECTS:",
    "HANDSHAKES:",
    "SEE:",
]
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)#]+)(?:#[^)]+)?\)")


def _load_system_map() -> dict:
    return json.loads(SYSTEM_MAP.read_text(encoding="utf-8"))


def _registry_ids(system_map: dict) -> set[str]:
    ids: set[str] = set()
    for key in ("systems", "files", "handshakes", "overlaps", "entrypoints", "state_stores", "environment_sources", "dashboard_consumers"):
        for item in system_map.get(key, []):
            item_id = str(item.get("id") or "").strip()
            if item_id:
                ids.add(item_id)
    return ids


def _iter_paths(system_map: dict) -> list[Path]:
    out: list[Path] = []
    for system in system_map.get("systems", []):
        out.append(REPO_ROOT / system["path"])
        for entrypoint_path in system.get("entrypoints", []):
            out.append(REPO_ROOT / entrypoint_path)
    for key in ("files", "entrypoints", "state_stores", "environment_sources", "dashboard_consumers"):
        for item in system_map.get(key, []):
            out.append(REPO_ROOT / item["path"])
    return out


def test_system_map_parses_and_paths_exist() -> None:
    system_map = _load_system_map()
    assert system_map.get("version") == 1
    missing = [str(path) for path in _iter_paths(system_map) if not path.exists()]
    assert not missing, f"missing registry paths: {missing}"



def test_handshake_references_and_file_handshakes_resolve() -> None:
    system_map = _load_system_map()
    ids = _registry_ids(system_map)
    for handshake in system_map.get("handshakes", []):
        assert handshake["from"] in ids, f"unknown handshake from id: {handshake['from']}"
        assert handshake["to"] in ids, f"unknown handshake to id: {handshake['to']}"
    handshake_ids = {item["id"] for item in system_map.get("handshakes", [])}
    for file_item in system_map.get("files", []):
        for handshake_id in file_item.get("handshakes", []):
            assert handshake_id in handshake_ids, f"unknown file handshake id: {file_item['id']} -> {handshake_id}"



def test_tier1_files_have_agent_headers() -> None:
    for path in TIER1_FILES:
        head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:25])
        assert "AGENT:" in head, f"missing AGENT header in {path}"
        for field in HEADER_FIELDS:
            assert field in head, f"missing header field {field} in {path}"


def test_windows_ops_scripts_include_wsl_reset_paths() -> None:
    for path, snippets in WSL_RESET_FILES.items():
        text = path.read_text(encoding="utf-8")
        for snippet in snippets:
            assert snippet in text, f"missing WSL reset marker {snippet!r} in {path}"


def test_windows_ops_foreground_run_paths_reset_before_launch() -> None:
    bridge_text = (REPO_ROOT / "ops/windows/20_start_bridge.bat").read_text(encoding="utf-8")
    runtime_text = (REPO_ROOT / "ops/windows/21_start_runtime.bat").read_text(encoding="utf-8")
    worker_text = (REPO_ROOT / "ops/windows/24_start_feature_push_worker.bat").read_text(encoding="utf-8")

    assert "call :reset_bridge_processes %PORT%" in bridge_text
    assert 'call :reset_runtime_processes %BRIDGE_PORT% ""' in runtime_text
    assert 'call "%~dp024_start_feature_push_worker.bat" --background' in runtime_text
    assert 'powershell -NoProfile -Command "$workerArgs=@(' in worker_text
    assert 'FXSTACK_FEATURE_PUSH_WORKER_STARTUP_TIMEOUT_SECS=60' in worker_text
    assert 'Start-Sleep -Seconds 1' in worker_text
    assert '[feature-push-worker] ready' in worker_text
    assert 'findstr /I /C:"Traceback" /C:"RuntimeError:" /C:"last_run_rc="' in worker_text



def test_docs_agent_links_resolve() -> None:
    for path in DOCS_ROOT.glob("*.md"):
        text = path.read_text(encoding="utf-8")
        for match in LINK_RE.finditer(text):
            target_txt = match.group(1).strip()
            if not target_txt or target_txt.startswith(("http://", "https://", "mailto:")):
                continue
            target = (path.parent / target_txt).resolve()
            assert target.exists(), f"broken link in {path}: {target_txt}"



def test_agents_md_points_to_agent_index() -> None:
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "docs/agents/README.md" in text
