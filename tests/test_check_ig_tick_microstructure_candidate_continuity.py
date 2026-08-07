from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sealer = _module("continuity_sealer", ROOT / "tools" / "seal_ig_tick_microstructure_candidate_preregistration.py")
checker = _module("continuity_checker", ROOT / "tools" / "check_ig_tick_microstructure_candidate_continuity.py")


def _prereg() -> dict:
    payload = {
        "schema_version": sealer.SCHEMA_VERSION,
        "tool_revision": sealer.TOOL_REVISION,
        "fixed_candidates": [dict(item) for item in sealer.FIXED_CANDIDATES],
        "attempt_accounting": {"cumulative_trials_lower_bound": 2774},
        "prospective_window": {
            "t0_utc_inclusive": "2026-08-04T16:10:00Z",
            "end_utc_exclusive": "2026-09-03T16:10:00Z",
            "consecutive_days": 30,
        },
        "source_binding": {"market_source_id_sha256": hashlib.sha256(b"source-1").hexdigest()},
        "authority": {"runtime_authorized": False, "order_authorized": False},
    }
    payload["preregistration_body_sha256"] = sealer.canonical_sha256(payload)
    return payload


def _receipt(prereg: dict) -> dict:
    payload = {
        "schema_version": checker.START_RECEIPT_SCHEMA,
        "collection_continued_across_t0": True,
        "preregistration_body_sha256": prereg["preregistration_body_sha256"],
        "t0_utc_inclusive": prereg["prospective_window"]["t0_utc_inclusive"],
        "market_source_id_sha256": hashlib.sha256(b"source-1").hexdigest(),
        "ordered_symbols": list(sealer.SYMBOL_SCOPE),
        "authority": {"runtime_authorized": False, "order_authorized": False},
    }
    payload["receipt_body_sha256"] = sealer.canonical_sha256(payload)
    return payload


def _readiness(*, observed: float, count: int = 1000, source: str = "source-1") -> dict:
    payload = {
        "schema_version": sealer.READINESS_SCHEMA,
        "collection_metadata_only": True,
        "source_identity_authenticated": True,
        "database_read_only": True,
        "research_authorized": False,
        "selection_authorized": False,
        "activation_authorized": False,
        "order_authorized": False,
        "symbol_scope": list(sealer.SYMBOL_SCOPE),
        "current_market_source_id": source,
        "observed_at_epoch": observed,
        "symbols": {
            symbol: {
                "observations": count,
                "first_sequence": 1,
                "last_sequence": count,
                "last_quote_epoch": observed - 1,
            }
            for symbol in sealer.SYMBOL_SCOPE
        },
    }
    payload["payload_sha256"] = sealer.canonical_sha256(payload)
    return payload


def _build(readiness: dict, previous: dict | None = None) -> dict:
    prereg = _prereg()
    receipt = _receipt(prereg)
    return checker.build_checkpoint(
        preregistration=prereg,
        preregistration_file_sha256="1" * 64,
        start_receipt=receipt,
        start_receipt_file_sha256="2" * 64,
        readiness=readiness,
        readiness_file_sha256="3" * 64,
        checked_at=datetime.fromtimestamp(readiness["observed_at_epoch"] + 1, tz=UTC),
        previous=previous,
        previous_body_sha256=previous.get("checkpoint_body_sha256") if previous else None,
        previous_file_sha256="4" * 64 if previous else None,
    )


def test_checkpoint_is_monotonic_and_authority_free() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    first = _build(_readiness(observed=t0 + 60, count=1000))
    second = _build(_readiness(observed=t0 + 120, count=1100), previous=first)

    assert first["sequence"] == 1
    assert second["sequence"] == 2
    assert second["previous_checkpoint_body_sha256"] == first["checkpoint_body_sha256"]
    assert second["source_continuity_proven"] is True
    assert not any(second["authority"].values())


def test_checkpoint_refuses_source_rollover() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    with pytest.raises(checker.CandidateContinuityRefusal, match="source_rollover"):
        _build(_readiness(observed=t0 + 60, source="source-2"))


def test_checkpoint_refuses_symbol_count_regression() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    first = _build(_readiness(observed=t0 + 60, count=1000))
    regressed = _readiness(observed=t0 + 120, count=1100)
    regressed["symbols"]["NZDJPY"]["observations"] = 999
    regressed.pop("payload_sha256")
    regressed["payload_sha256"] = sealer.canonical_sha256(regressed)

    with pytest.raises(checker.CandidateContinuityRefusal, match="regressed:NZDJPY"):
        _build(regressed, previous=first)


def test_checkpoint_refuses_quote_before_t0() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    readiness = _readiness(observed=t0 + 60)
    readiness["symbols"]["AUDUSD"]["last_quote_epoch"] = t0 - 1
    readiness.pop("payload_sha256")
    readiness["payload_sha256"] = sealer.canonical_sha256(readiness)

    with pytest.raises(checker.CandidateContinuityRefusal, match="invalid:AUDUSD"):
        _build(readiness)


def test_latest_checkpoint_refuses_missing_chain_link(tmp_path: Path) -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    first = _build(_readiness(observed=t0 + 60, count=1000))
    second = _build(_readiness(observed=t0 + 120, count=1100), previous=first)
    second["previous_checkpoint_body_sha256"] = "f" * 64
    second.pop("checkpoint_body_sha256")
    second["checkpoint_body_sha256"] = sealer.canonical_sha256(second)
    for payload in (first, second):
        path = tmp_path / f"{checker.CHECKPOINT_PREFIX}{payload['checkpoint_body_sha256']}.json"
        path.write_text(__import__("json").dumps(payload), encoding="utf-8")

    with pytest.raises(checker.CandidateContinuityRefusal, match="link_mismatch"):
        checker._latest_checkpoint(tmp_path)
