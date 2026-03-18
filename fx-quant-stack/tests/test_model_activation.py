from __future__ import annotations

import json
from pathlib import Path

from fxstack.runtime.service import RuntimeService
from fxstack.training.activation import activate_registry_file


def test_activate_registry_file_updates_db_and_manifest(tmp_path: Path):
    db_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"

    reg = tmp_path / "registry" / "eurusd_run1.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(
        json.dumps(
            {
                "run_id": "run1",
                "pair": "EURUSD",
                "artifacts": {
                    "regime": {"path": "fx-quant-stack/artifacts/eurusd/regime_hmm"},
                    "meta": {"path": "fx-quant-stack/artifacts/eurusd/meta_filter"},
                    "swing_transformer": {"path": "fx-quant-stack/artifacts/eurusd/swing_transformer"},
                    "swing_xgb": {"path": "fx-quant-stack/artifacts/eurusd/swing_xgb"},
                    "intraday_tcn": {"path": "fx-quant-stack/artifacts/eurusd/intraday_tcn"},
                    "intraday_xgb": {"path": "fx-quant-stack/artifacts/eurusd/intraday_xgb"},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
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
