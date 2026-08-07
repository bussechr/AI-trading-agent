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


sealer = _module(
    "candidate_sealer_for_start_test",
    ROOT / "tools" / "seal_ig_tick_microstructure_candidate_preregistration.py",
)
verifier = _module(
    "candidate_start_verifier",
    ROOT / "tools" / "verify_ig_tick_microstructure_candidate_start.py",
)


def _readiness(*, observed: float, quote: float, source: str = "source-1") -> dict:
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
            symbol: {"last_quote_epoch": quote} for symbol in sealer.SYMBOL_SCOPE
        },
    }
    payload["payload_sha256"] = sealer.canonical_sha256(payload)
    return payload


def _prereg(pre: dict) -> dict:
    source_hash = hashlib.sha256(b"source-1").hexdigest()
    payload = {
        "schema_version": sealer.SCHEMA_VERSION,
        "tool_revision": sealer.TOOL_REVISION,
        "fixed_candidates": [dict(item) for item in sealer.FIXED_CANDIDATES],
        "attempt_accounting": {"cumulative_trials_lower_bound": 2774},
        "prospective_window": {
            "t0_utc_inclusive": "2026-08-04T16:10:00Z",
            "consecutive_days": 30,
        },
        "discovery_lineage": {
            "readiness_payload_sha256": pre["payload_sha256"],
            "readiness_file_sha256": "2" * 64,
        },
        "source_binding": {"market_source_id_sha256": source_hash},
        "authority": {"runtime_authorized": False, "order_authorized": False},
    }
    payload["preregistration_body_sha256"] = sealer.canonical_sha256(payload)
    return payload


def test_receipt_proves_all_symbol_source_continuity_across_t0() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    pre = _readiness(observed=t0 - 30, quote=t0 - 31)
    post = _readiness(observed=t0 + 15, quote=t0 + 14)

    receipt = verifier.build_receipt(
        preregistration=_prereg(pre),
        preregistration_file_sha256="1" * 64,
        pre_readiness=pre,
        pre_readiness_file_sha256="2" * 64,
        post_readiness=post,
        post_readiness_file_sha256="3" * 64,
        verified_at=datetime.fromtimestamp(t0 + 20, tz=UTC),
    )

    assert receipt["collection_continued_across_t0"] is True
    assert list(receipt["edge_brackets"]) == list(sealer.SYMBOL_SCOPE)
    assert not any(receipt["authority"].values())


def test_receipt_refuses_source_rollover() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    pre = _readiness(observed=t0 - 30, quote=t0 - 31)
    post = _readiness(observed=t0 + 15, quote=t0 + 14, source="source-2")

    with pytest.raises(verifier.CandidateStartRefusal, match="source_rollover"):
        verifier.build_receipt(
            preregistration=_prereg(pre),
            preregistration_file_sha256="1" * 64,
            pre_readiness=pre,
            pre_readiness_file_sha256="2" * 64,
            post_readiness=post,
            post_readiness_file_sha256="3" * 64,
            verified_at=datetime.fromtimestamp(t0 + 20, tz=UTC),
        )


def test_receipt_refuses_missing_post_t0_symbol_quote() -> None:
    t0 = datetime(2026, 8, 4, 16, 10, tzinfo=UTC).timestamp()
    pre = _readiness(observed=t0 - 30, quote=t0 - 31)
    post = _readiness(observed=t0 + 15, quote=t0 + 14)
    post["symbols"]["NZDJPY"]["last_quote_epoch"] = t0 - 1
    post.pop("payload_sha256")
    post["payload_sha256"] = sealer.canonical_sha256(post)

    with pytest.raises(
        verifier.CandidateStartRefusal,
        match="start_edge_not_bracketed:NZDJPY",
    ):
        verifier.build_receipt(
            preregistration=_prereg(pre),
            preregistration_file_sha256="1" * 64,
            pre_readiness=pre,
            pre_readiness_file_sha256="2" * 64,
            post_readiness=post,
            post_readiness_file_sha256="3" * 64,
            verified_at=datetime.fromtimestamp(t0 + 20, tz=UTC),
        )
