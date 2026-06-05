"""The self-improvement loop registers a draft the existing factory can act on."""

from __future__ import annotations

from fxstack.improve.factory_bridge import register_to_factory
from fxstack.improve.loop import run_improvement_loop
from fxstack.orchestration.contracts import ExperimentProposal
from fxstack.orchestration.experiments import review_experiment, trace_experiment


def test_register_writes_contract_valid_bundle(tmp_path):
    r = run_improvement_loop(
        iterations=6,
        seed=17,
        emit_experiment=True,
        register_experiment=True,
        experiment_base_dir=str(tmp_path),
        upsert_service=False,
    )
    assert r.registration is not None
    assert r.registration["ok"] is True
    proposal_path = r.registration["written"]["proposal"]
    assert proposal_path.endswith("proposal.json")
    # The written proposal validates against the strict contract.
    import json

    payload = json.loads(open(proposal_path, encoding="utf-8").read())
    ExperimentProposal.model_validate(payload)


def test_registered_draft_drives_factory_review_and_trace(tmp_path):
    r = run_improvement_loop(
        iterations=5,
        seed=23,
        emit_experiment=True,
        register_experiment=True,
        experiment_base_dir=str(tmp_path),
        upsert_service=False,
    )
    # The factory addresses bundles by the experiment_id *string* used at write time
    # (the loop's derived id, not the uuid). Recover it from the bundle path.
    bundle_root = r.registration["bundle_root"]
    factory_exp_id = bundle_root.replace("\\", "/").rstrip("/").split("/")[-1]

    review = review_experiment(experiment_id=factory_exp_id, out_dir=str(tmp_path), decision="approved")
    assert review["ok"] is True

    trace = trace_experiment(experiment_id=factory_exp_id, out_dir=str(tmp_path))
    assert trace["ok"] is True
    assert trace["proposal"].get("hypothesis")
    # Review flipped the draft to approved through the normal factory path.
    assert trace["proposal"].get("approval_status") == "approved"


def test_register_to_factory_rejects_malformed_proposal(tmp_path):
    import pytest

    with pytest.raises(Exception):
        register_to_factory(
            experiment_id="bad",
            proposal_payload={"not": "a valid proposal"},
            base_dir=str(tmp_path),
            upsert_service=False,
        )
