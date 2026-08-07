"""The live model set is grandfathered past the certificate gate -- say so.

Enforcement lives in ``fxstack.training.activation``, which is on
``FORBIDDEN_RUNTIME_MODULES`` and is absent from the installed runtime. That
boundary is correct: a runtime able to refuse its own deployed models mid-session
could strand open positions. But it means enforcement is PROSPECTIVE only, and
the currently-deployed EURUSD set was activated before the gate was switched on.

Measured on the real manifest at the time of writing: 8 artifacts, 0 certified.
These tests pin the reporting so that fact stays visible rather than silent, and
pin that the report can never itself break a startup preflight.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.models.artifact_contract import VALIDATION_CERTIFICATE_FILENAME
from fxstack.runtime.model_manifest_preflight import certificate_coverage


def _manifest(tmp_path: Path, *, components: dict[str, bool]) -> dict:
    """Build an active-model-set row; ``components`` maps name -> has_certificate."""

    artifacts = {}
    for name, certified in components.items():
        art = tmp_path / name
        art.mkdir(parents=True, exist_ok=True)
        art.joinpath("model.bin").write_bytes(b"payload")
        if certified:
            art.joinpath(VALIDATION_CERTIFICATE_FILENAME).write_text(
                json.dumps({"passed": True}), encoding="utf-8"
            )
        artifacts[name] = {"path": str(art)}
    return {"EURUSD": {"enabled": True, "artifacts": artifacts}}


def test_uncertified_artifacts_are_reported_not_hidden(tmp_path: Path):
    sets = _manifest(tmp_path, components={"regime": False, "meta": False})
    rep = certificate_coverage(
        active_model_sets=sets, target_pairs=["EURUSD"], project_root=tmp_path
    )
    assert rep["artifacts_certified"] == 0
    assert rep["all_certified"] is False
    assert rep["grandfathered"] is True
    assert set(rep["pairs"]["EURUSD"]["uncertified"]) == {"regime", "meta"}


def test_certified_artifacts_are_recognised(tmp_path: Path):
    sets = _manifest(tmp_path, components={"regime": True, "meta": True})
    rep = certificate_coverage(
        active_model_sets=sets, target_pairs=["EURUSD"], project_root=tmp_path
    )
    assert rep["all_certified"] is True
    assert rep["grandfathered"] is False
    assert rep["pairs"]["EURUSD"]["uncertified"] == []


def test_partial_coverage_still_counts_as_grandfathered(tmp_path: Path):
    """One missing certificate is enough -- the set is not warranted."""

    sets = _manifest(tmp_path, components={"regime": True, "meta": False})
    rep = certificate_coverage(
        active_model_sets=sets, target_pairs=["EURUSD"], project_root=tmp_path
    )
    assert rep["artifacts_certified"] == 1
    assert rep["all_certified"] is False
    assert rep["grandfathered"] is True


def test_empty_manifest_is_not_reported_as_certified(tmp_path: Path):
    """No artifacts must not read as 'everything is fine'."""

    rep = certificate_coverage(
        active_model_sets={}, target_pairs=["EURUSD"], project_root=tmp_path
    )
    assert rep["all_certified"] is False
    assert rep["grandfathered"] is False


@pytest.mark.parametrize(
    "sets",
    [
        {"EURUSD": {"artifacts": "not-a-dict"}},
        {"EURUSD": "not-a-dict"},
        {"EURUSD": {"artifacts": {"regime": {"path": "\x00bad"}}}},
    ],
)
def test_malformed_input_never_raises(tmp_path: Path, sets):
    """Advisory reporting must not be able to fail a startup preflight."""

    rep = certificate_coverage(
        active_model_sets=sets, target_pairs=["EURUSD"], project_root=tmp_path
    )
    assert isinstance(rep, dict)
    assert rep["all_certified"] is False


def test_preflight_result_carries_the_report(tmp_path: Path):
    """The field must actually reach the preflight output, not just exist."""

    import inspect

    from fxstack.runtime import model_manifest_preflight as mmp

    src = inspect.getsource(mmp)
    assert '"validation_certificates": _safe_certificate_coverage(' in src, (
        "the certificate coverage report is no longer surfaced in the preflight "
        "result -- grandfathered models would go back to being invisible"
    )
