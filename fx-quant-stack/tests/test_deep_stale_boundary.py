from __future__ import annotations

import json
import time
from pathlib import Path

from fxstack.tasks import _is_stale


def _write_meta(path: Path, created_at: float) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({"created_at": float(created_at)}), encoding="utf-8")


def test_deep_artifact_stale_boundary(tmp_path: Path):
    now = time.time()
    fresh = tmp_path / "fresh"
    old = tmp_path / "old"
    _write_meta(fresh, now - (23.0 * 3600.0))
    _write_meta(old, now - (25.0 * 3600.0))

    is_fresh_stale, fresh_age = _is_stale(fresh, 24.0)
    is_old_stale, old_age = _is_stale(old, 24.0)

    assert is_fresh_stale is False
    assert is_old_stale is True
    assert fresh_age is not None and fresh_age < 24.0
    assert old_age is not None and old_age > 24.0
