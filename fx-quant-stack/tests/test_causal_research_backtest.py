from __future__ import annotations

import importlib.util
import hashlib
import json
from argparse import Namespace
from pathlib import Path
import sys

import pandas as pd
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_causal_research_backtest.py"
WALK_FORWARD_PATH = REPO_ROOT / "tools" / "run_causal_walk_forward.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.mlops.model_uri import normalize_artifact_ref  # noqa: E402


def _artifact_path(value: object) -> str:
    return str(normalize_artifact_ref(value).get("path") or "").strip()


def _load_module():
    spec = importlib.util.spec_from_file_location("fxstack_causal_research_backtest_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_walk_forward_module():
    tools_root = str(REPO_ROOT / "tools")
    if tools_root not in sys.path:
        sys.path.insert(0, tools_root)
    spec = importlib.util.spec_from_file_location("run_causal_walk_forward_isolation_test", WALK_FORWARD_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_offline_contract(tmp_path: Path) -> tuple[Path, Path]:
    raw_root = tmp_path / "sealed_raw"
    raw_root.mkdir()
    (raw_root / "point_in_time_raw_snapshot.json").write_text(
        json.dumps(
            {
                "version": "point_in_time_raw_snapshot_v1",
                "future_data_access": "forbidden",
                "cutoff_inclusive": "2026-03-21T00:00:00Z",
                "output_root": str(raw_root.resolve()),
                "rows": [],
            }
        ),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "research_models.json"
    manifest = {
        "version": "fxstack_research_manifest_v1",
        "research_only": True,
        "runtime_store_updated": False,
        "active_model_sets": {},
    }
    manifest["manifest_content_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return raw_root, manifest_path


def test_causal_execution_timeline_never_fills_on_decision_bar() -> None:
    module = _load_module()
    timeline = pd.date_range("2026-03-20T00:00:00Z", periods=5, freq="5min")

    decisions, executions = module._causal_execution_timelines(timeline, fill_delay_bars=1)

    assert list(decisions) == list(timeline[:-1])
    assert list(executions) == list(timeline[1:])
    assert all(execution > decision for decision, execution in zip(decisions, executions, strict=True))
    with pytest.raises(ValueError, match="at least 1"):
        module._causal_execution_timelines(timeline, fill_delay_bars=0)


def test_offline_contract_accepts_only_sealed_raw_and_research_manifest(tmp_path: Path) -> None:
    module = _load_module()
    raw_root, manifest_path = _write_offline_contract(tmp_path)
    args = Namespace(
        raw_root=str(raw_root),
        manifest_path=str(manifest_path),
        end_ts="2026-03-20T23:55:00Z",
    )

    actual_raw, actual_manifest, snapshot, manifest = module._load_offline_contract(args)

    assert actual_raw == raw_root.resolve()
    assert actual_manifest == manifest_path.resolve()
    assert snapshot["future_data_access"] == "forbidden"
    assert manifest["research_only"] is True
    assert manifest["runtime_store_updated"] is False


def test_offline_contract_refuses_missing_sentinel_and_activation_manifest(tmp_path: Path) -> None:
    module = _load_module()
    raw_root = tmp_path / "unsealed_raw"
    raw_root.mkdir()
    production_manifest = tmp_path / "production_models.json"
    production_payload = {
        "version": "fxstack_research_manifest_v1",
        "research_only": False,
        "runtime_store_updated": True,
    }
    production_payload["manifest_content_sha256"] = hashlib.sha256(
        json.dumps(production_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    production_manifest.write_text(json.dumps(production_payload), encoding="utf-8")
    args = Namespace(
        raw_root=str(raw_root),
        manifest_path=str(production_manifest),
        end_ts="2026-03-20T23:55:00Z",
    )

    with pytest.raises(FileNotFoundError, match="sealed point-in-time"):
        module._load_offline_contract(args)

    (raw_root / "point_in_time_raw_snapshot.json").write_text(
        json.dumps(
            {
                "version": "point_in_time_raw_snapshot_v1",
                "future_data_access": "forbidden",
                "cutoff_inclusive": "2026-03-21T00:00:00Z",
                "output_root": str(raw_root.resolve()),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="research_only"):
        module._load_offline_contract(args)


@pytest.mark.parametrize(
    "environment",
    [
        {"FXSTACK_DATABASE_URL": "postgresql://production"},
        {"MT4_BRIDGE_URL": "http://production:8000"},
        {"FXSTACK_BRIDGE_API_KEY": "secret"},
        {"FXSTACK_EXECUTION_PROVIDER": "mt4"},
        {"FXSTACK_MODEL_ACTIVATION_MANIFEST": "active_models.json"},
    ],
)
def test_offline_environment_refuses_live_or_activation_configuration(environment: dict[str, str]) -> None:
    module = _load_module()

    with pytest.raises(RuntimeError, match="offline research refuses"):
        module._assert_offline_environment(environment)


def test_research_import_chain_has_no_runtime_or_network_client() -> None:
    tool_source = TOOL_PATH.read_text(encoding="utf-8")
    base_source = (REPO_ROOT / "tools" / "fxstack_lifecycle_equity_backtest.py").read_text(encoding="utf-8")
    support_source = (
        REPO_ROOT / "fx-quant-stack" / "src" / "fxstack" / "backtest" / "research_support.py"
    ).read_text(encoding="utf-8")
    joined = "\n".join((tool_source, base_source, support_source))

    assert "fxstack.runtime.runner" not in joined
    assert "urllib.request" not in joined
    assert "requests" not in joined
    assert "httpx" not in joined
    assert "/v2/decision-snapshots" not in joined
    assert "artifact_lock" not in support_source
    assert "validate_artifact_contract_read_only" in support_source


def test_walk_forward_scrubs_live_child_configuration() -> None:
    module = _load_walk_forward_module()
    environment = {
        "PATH": "safe",
        "FXSTACK_DATABASE_URL": "postgresql://production",
        "MT4_BRIDGE_URL": "http://production:8000",
        "FXSTACK_BRIDGE_API_KEY": "secret",
        "FXSTACK_EXECUTION_PROVIDER": "mt4",
        "FXSTACK_MODEL_ACTIVATION_MANIFEST": "active_models.json",
    }

    sanitized = module._offline_child_env(environment)

    assert sanitized == {"PATH": "safe"}
    source = WALK_FORWARD_PATH.read_text(encoding="utf-8")
    assert "fxstack.training.activation" not in source
    assert "fxstack.training.research_manifest" in source
    assert "bundle_root=window_root" in source


def test_artifact_path_supports_dict_manifest_entries() -> None:
    path = _artifact_path(
        {
            "model_uri": "models:/fx.meta_filter.EURUSD.M5@champion",
            "evidence_refs": {
                "artifact_path": "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/meta_filter"
            },
        }
    )

    assert path == "fx-quant-stack/artifacts_shadow/full_20260323/eurusd/meta_filter"
