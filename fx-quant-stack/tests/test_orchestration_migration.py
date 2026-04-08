from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect

from fxstack.runtime.db_tools import migrate_database, verify_database


def test_phase1_orchestration_migration_creates_tables_and_columns(tmp_path: Path) -> None:
    db_url = f"sqlite+pysqlite:///{tmp_path / 'phase1_orchestration.db'}"
    result = migrate_database(database_url=db_url, root=Path(__file__).resolve().parents[1])
    assert bool(result.get("ok")), result

    verification = verify_database(database_url=db_url)
    assert bool(verification.get("ok")), verification

    engine = create_engine(db_url, future=True)
    try:
        inspector = inspect(engine)
        present = set(inspector.get_table_names())
        for required in {
            "orchestration_runs",
            "agent_proposals",
            "governed_decisions",
            "agent_traces",
            "approval_events",
            "experiment_proposals",
            "experiment_promotions",
            "experiment_lineage",
        }:
            assert required in present
        governed_columns = {col["name"] for col in inspector.get_columns("governed_decisions")}
        for required in {
            "runtime_mode",
            "version_bundle_json",
        }:
            assert required in governed_columns
        proposal_columns = {col["name"] for col in inspector.get_columns("experiment_proposals")}
        for required in {
            "prompt_hash",
            "tool_trace_hash",
            "model_id",
            "decision_seed",
            "input_artefact_refs_json",
            "config_diff_json",
            "replay_window",
            "artifact_root",
            "latest_stage",
            "latest_promotion_id",
        }:
            assert required in proposal_columns
        lineage_columns = {col["name"] for col in inspector.get_columns("experiment_lineage")}
        for required in {
            "replay_refs_json",
            "latest_stage",
            "approval_status",
            "promotion_ids_json",
            "approval_event_ids_json",
        }:
            assert required in lineage_columns
        command_columns = {col["name"] for col in inspector.get_columns("commands")}
        for required in {
            "correlation_id",
            "thread_id",
            "idempotency_key",
            "schema_version",
            "orchestration_meta_json",
        }:
            assert required in command_columns
        governed_columns = {col["name"] for col in inspector.get_columns("governed_decisions")}
        assert "runtime_mode" in governed_columns
    finally:
        engine.dispose()
