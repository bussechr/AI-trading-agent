from __future__ import annotations

import json
from pathlib import Path

from tools import audit_agent_nav_graph


def test_system_map_audit_checks_declared_paths_but_ignores_launch_descriptions(
    tmp_path: Path, monkeypatch
) -> None:
    existing = tmp_path / "tools" / "worker.py"
    existing.parent.mkdir(parents=True)
    existing.write_text("", encoding="utf-8")
    system_map = tmp_path / "system-map.yaml"
    system_map.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "path": "tools/worker.py",
                        "launches": "qualified BUY/SELL with SL/TP under a runtime policy",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_agent_nav_graph, "REPO", tmp_path)

    assert audit_agent_nav_graph.audit_system_map(system_map) == []


def test_system_map_audit_reports_missing_declared_path(tmp_path: Path, monkeypatch) -> None:
    system_map = tmp_path / "system-map.yaml"
    system_map.write_text(json.dumps({"nodes": [{"path": "missing/file.py"}]}), encoding="utf-8")
    monkeypatch.setattr(audit_agent_nav_graph, "REPO", tmp_path)

    broken = audit_agent_nav_graph.audit_system_map(system_map)

    assert broken == [("system-map.yaml::missing/file.py", "missing/file.py")]


def test_system_map_id_audit_reports_duplicate_and_unresolved_references(
    tmp_path: Path, monkeypatch
) -> None:
    system_map = tmp_path / "system-map.yaml"
    system_map.write_text(
        json.dumps(
            {
                "systems": [
                    {"id": "runtime", "depends_on": ["missing-system"]},
                    {"id": "runtime"},
                ],
                "files": [
                    {
                        "id": "runtime.runner",
                        "depends_on": ["runtime"],
                        "called_by": ["missing-entrypoint"],
                        "handshakes": ["runtime.ready"],
                    }
                ],
                "handshakes": [
                    {"id": "runtime.ready", "from": "runtime.runner", "to": "runtime"}
                ],
                "entrypoints": [],
                "state_stores": [],
                "environment_sources": [],
                "dashboard_consumers": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_agent_nav_graph, "REPO", tmp_path)

    assert audit_agent_nav_graph.audit_system_map_ids(system_map) == [
        (
            "system-map.yaml::systems::runtime",
            "duplicate-id:first-declared-in:systems",
        ),
        ("system-map.yaml::systems::runtime::depends_on", "missing-system"),
        (
            "system-map.yaml::files::runtime.runner::called_by",
            "missing-entrypoint",
        ),
    ]


def test_main_returns_nonzero_when_any_reference_is_broken(monkeypatch) -> None:
    monkeypatch.setattr(audit_agent_nav_graph, "audit_markdown", lambda _files: [("README.md", "missing.md")])
    monkeypatch.setattr(audit_agent_nav_graph, "audit_system_map", lambda _path: [])
    monkeypatch.setattr(audit_agent_nav_graph, "audit_system_map_ids", lambda _path: [])
    monkeypatch.setattr(audit_agent_nav_graph, "audit_agent_breadcrumbs", lambda: [])

    assert audit_agent_nav_graph.main() == 1


def test_breadcrumb_audit_allows_documented_generated_installer_env(
    tmp_path: Path, monkeypatch
) -> None:
    env_script = tmp_path / "ops" / "windows" / "_env.bat"
    env_script.parent.mkdir(parents=True)
    env_script.write_text(
        "REM AGENT: DEPENDS ON: optional `ops/windows/installed_env.bat`\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(audit_agent_nav_graph, "REPO", tmp_path)

    assert audit_agent_nav_graph.audit_agent_breadcrumbs() == []
