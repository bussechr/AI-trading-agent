"""Tests for the activation-time validation gate.

The behaviour that matters: a missing certificate must block when enforcement is
on, and a certificate for a DIFFERENT model must never pass. Both are the ways a
validation layer silently becomes decorative.
"""

from __future__ import annotations

import json

import pytest

from fxstack.validation.activation_gate import (
    CERTIFICATE_FILENAME,
    certificate_path_for,
    gate_activation,
    read_certificate,
    write_certificate,
)
from fxstack.validation.certificate import AcceptanceThresholds, build_certificate

PAYLOAD_SHA = "a" * 64
DATASET_FP = "dukascopy:EURUSD:M15:2024-01-01..2026-07-24"


def _stats(**overrides) -> dict[str, float]:
    base = {
        "mcpt_p_value": 0.004,
        "bootstrap_sharpe_ci_lower": 0.35,
        "pbo": 0.18,
        "dsr": 0.97,
        "n_trades": 320.0,
        "max_drawdown": 0.09,
        "survives_2x_costs": 1.0,
    }
    base.update(overrides)
    return base


def _cert(**overrides):
    kwargs = {
        "strategy_id": "eurusd_range_reversion",
        "pair": "EURUSD",
        "model_payload_sha256": PAYLOAD_SHA,
        "dataset_fingerprint": DATASET_FP,
        "created_at": "2026-07-30T00:00:00Z",
        "n_trials": 40,
        "statistics": _stats(),
    }
    kwargs.update(overrides)
    return build_certificate(**kwargs)


def test_missing_certificate_blocks_when_enforced(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    result = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=True)
    assert not result.allowed
    assert "validation_certificate_absent" in result.reasons
    assert not result.validated


def test_missing_certificate_is_observable_but_permitted_when_not_enforced(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    result = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=False)
    assert result.allowed  # does not veto...
    assert not result.validated  # ...but never claims to be validated
    assert "validation_certificate_absent" in result.reasons
    assert result.to_dict()["validated"] is False


def test_valid_certificate_allows_activation(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    write_certificate(_cert(), artifact_path=artifact)
    result = gate_activation(
        artifact_path=artifact,
        expected_payload_sha256=PAYLOAD_SHA,
        expected_dataset_fingerprint=DATASET_FP,
        enforce=True,
    )
    assert result.allowed, result.reasons
    assert result.validated
    assert result.certificate is not None


def test_certificate_for_a_different_payload_is_rejected(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    write_certificate(_cert(), artifact_path=artifact)
    result = gate_activation(artifact_path=artifact, expected_payload_sha256="b" * 64, enforce=True)
    assert not result.allowed
    assert "model_payload_mismatch" in result.reasons


def test_certificate_for_a_different_dataset_is_rejected(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    write_certificate(_cert(), artifact_path=artifact)
    result = gate_activation(
        artifact_path=artifact,
        expected_payload_sha256=PAYLOAD_SHA,
        expected_dataset_fingerprint="some:other:window",
        enforce=True,
    )
    assert not result.allowed
    assert "dataset_fingerprint_mismatch" in result.reasons


def test_failing_certificate_blocks(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    write_certificate(_cert(statistics=_stats(pbo=0.9, dsr=0.1)), artifact_path=artifact)
    result = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=True)
    assert not result.allowed
    assert "certificate_not_passing" in result.reasons


def test_tampered_certificate_blocks(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    path = write_certificate(_cert(), artifact_path=artifact)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["statistics"]["pbo"] = 0.01  # improve the numbers post-hoc
    path.write_text(json.dumps(payload), encoding="utf-8")
    result = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=True)
    assert not result.allowed
    assert "certificate_hash_mismatch" in result.reasons


def test_stricter_current_thresholds_override_a_sealed_pass(tmp_path):
    """A certificate sealed under a laxer bar must not pass a stricter one."""

    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    write_certificate(_cert(statistics=_stats(dsr=0.96)), artifact_path=artifact)
    lax = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=True)
    assert lax.allowed
    strict = gate_activation(
        artifact_path=artifact,
        expected_payload_sha256=PAYLOAD_SHA,
        enforce=True,
        thresholds=AcceptanceThresholds(dsr_min=0.995),
    )
    assert not strict.allowed
    assert any(reason.startswith("recheck:") for reason in strict.reasons)


def test_unreadable_and_malformed_certificates_block(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    path = certificate_path_for(artifact)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert "validation_certificate_unreadable" in read_certificate(path)[1]
    path.write_text('["a list, not an object"]', encoding="utf-8")
    assert "validation_certificate_malformed" in read_certificate(path)[1]
    result = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=True)
    assert not result.allowed


def test_certificate_path_convention(tmp_path):
    assert certificate_path_for(tmp_path / "swing_xgb" / "model.json").name == CERTIFICATE_FILENAME
    # A directory (no suffix) resolves inside itself, not beside it.
    assert certificate_path_for(tmp_path / "swing_xgb") == tmp_path / "swing_xgb" / CERTIFICATE_FILENAME


@pytest.mark.parametrize("missing", ["mcpt_p_value", "pbo", "dsr", "survives_2x_costs"])
def test_incomplete_evidence_blocks(tmp_path, missing):
    artifact = tmp_path / "model.json"
    artifact.write_text("{}", encoding="utf-8")
    stats = _stats()
    stats.pop(missing)
    write_certificate(_cert(statistics=stats), artifact_path=artifact)
    result = gate_activation(artifact_path=artifact, expected_payload_sha256=PAYLOAD_SHA, enforce=True)
    assert not result.allowed
