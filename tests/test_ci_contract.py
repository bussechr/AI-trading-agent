from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_github_actions_are_disabled() -> None:
    workflows = REPO_ROOT / ".github" / "workflows"
    tracked_workflows = (
        [
            path
            for pattern in ("*.yml", "*.yaml")
            for path in workflows.glob(pattern)
        ]
        if workflows.exists()
        else []
    )

    assert tracked_workflows == []


def test_dashboard_package_manager_stays_locked() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["packageManager"] == "pnpm@10.6.2"
