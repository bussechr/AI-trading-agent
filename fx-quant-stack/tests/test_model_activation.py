from __future__ import annotations

import json
from pathlib import Path

from fxstack.runtime.db_tools import migrate_database
from fxstack.runtime.service import RuntimeService
from fxstack.training.activation import activate_registry_file


def _make_artifact(root: Path, name: str) -> str:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "meta.json").write_text(json.dumps({"name": name}, indent=2), encoding="utf-8")
    return str(path)


def _make_directional_belief_v2_artifact(root: Path) -> str:
    path = root / "directional_belief_v2"
    path.mkdir(parents=True, exist_ok=True)
    for name in [
        "ranker_xgb",
        "ev_above_hurdle_xgb",
        "expected_net_ev_bps_xgb",
        "confirm_success_xgb",
        "fail_fast_xgb",
    ]:
        subdir = path / name
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / "meta.json").write_text(json.dumps({"name": name}, indent=2), encoding="utf-8")
    (path / "meta.json").write_text(
        json.dumps(
            {
                "model_version": "directional_belief_v2",
                "belief_contract": "directional_belief_v2",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return str(path)


def test_activate_registry_file_updates_db_and_manifest(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    reg = tmp_path / "registry" / "eurusd_run1.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run1",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    item = activate_registry_file(
        database_url=db_url,
        registry_file=reg,
        manifest_path=manifest,
    )
    assert str(item.get("pair")) == "EURUSD"

    svc = RuntimeService(database_url=db_url)
    active = svc.get_active_model_set("EURUSD")
    assert active is not None
    assert str(active.get("model_set_id")) == "run1"

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert str(payload["active_model_sets"]["EURUSD"]["model_set_id"]) == "run1"
    assert str(payload["active_model_sets"]["EURUSD"]["policies"]["swing"]) == "transformer_primary_xgb_fallback"


def test_activate_registry_file_allows_missing_optional_directional_belief(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    reg = tmp_path / "registry" / "eurusd_run_belief_optional.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run-belief-optional",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                    "directional_belief": {"path": str(artifacts_root / "directional_belief_missing")},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    item = activate_registry_file(
        database_url=db_url,
        registry_file=reg,
        manifest_path=manifest,
    )

    assert str(item.get("pair")) == "EURUSD"
    assert bool(item.get("capabilities", {}).get("has_directional_belief", False)) is False


def test_activate_registry_file_accepts_directional_belief_v2_artifact(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    artifacts_root = tmp_path / "artifacts"
    out = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    reg = tmp_path / "registry" / "eurusd_run_belief_v2.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run-belief-v2",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": _make_artifact(artifacts_root, "regime_hmm")},
                    "meta": {"path": _make_artifact(artifacts_root, "meta_filter")},
                    "swing_transformer": {"path": _make_artifact(artifacts_root, "swing_transformer")},
                    "swing_xgb": {"path": _make_artifact(artifacts_root, "swing_xgb")},
                    "intraday_tcn": {"path": _make_artifact(artifacts_root, "intraday_tcn")},
                    "intraday_xgb": {"path": _make_artifact(artifacts_root, "intraday_xgb")},
                    "exit_policy": {"path": _make_artifact(artifacts_root, "exit_policy")},
                    "reversal_failure": {"path": _make_artifact(artifacts_root, "reversal_failure")},
                    "reversal_opportunity": {"path": _make_artifact(artifacts_root, "reversal_opportunity")},
                    "directional_belief": {"path": _make_directional_belief_v2_artifact(artifacts_root)},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": {"intraday_contract": "hierarchical_v1", "belief_contract": "directional_belief_v2"},
            }
        ),
        encoding="utf-8",
    )
    manifest = tmp_path / "active_models.json"

    item = activate_registry_file(
        database_url=db_url,
        registry_file=reg,
        manifest_path=manifest,
    )

    assert str(item.get("pair")) == "EURUSD"
    assert bool(item.get("metadata", {}).get("capabilities", {}).get("has_directional_belief", False)) is True
