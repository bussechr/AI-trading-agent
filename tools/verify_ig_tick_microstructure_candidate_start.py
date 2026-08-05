"""Create an authority-free receipt proving a prospective tick-window start."""

from __future__ import annotations

# AGENT: ROLE: offline start-edge verifier for the microstructure candidate.
# AGENT: HANDSHAKE: sealed preregistration + pre/post readiness -> immutable receipt.
# AGENT: ISOLATION: local JSON reads only; no database, network, runtime, or order path.

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SEALER_PATH = ROOT / "tools" / "seal_ig_tick_microstructure_candidate_preregistration.py"
_SPEC = importlib.util.spec_from_file_location("tick_candidate_sealer", SEALER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("candidate_sealer_import_invalid")
sealer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sealer)

SCHEMA_VERSION = "fxstack.ig_tick_microstructure_candidate_start_receipt.v1"
MAXIMUM_BRACKET_GAP_SECONDS = 600.0


class CandidateStartRefusal(RuntimeError):
    pass


def _read(path: str | Path, *, reason: str) -> tuple[dict[str, Any], str]:
    candidate = Path(path).expanduser().resolve()
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(reason)
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        file_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
    except (OSError, ValueError):
        raise CandidateStartRefusal(reason) from None
    if not isinstance(payload, dict):
        raise CandidateStartRefusal(reason)
    return payload, file_sha


def _verify_readiness(payload: Mapping[str, Any]) -> str:
    expected = str(payload.get("payload_sha256") or "").lower()
    body = dict(payload)
    body.pop("payload_sha256", None)
    if (
        len(expected) != 64
        or sealer.canonical_sha256(body) != expected
        or payload.get("schema_version") != sealer.READINESS_SCHEMA
        or payload.get("source_identity_authenticated") is not True
        or payload.get("database_read_only") is not True
        or payload.get("collection_metadata_only") is not True
        or payload.get("research_authorized") is not False
        or payload.get("activation_authorized") is not False
        or payload.get("order_authorized") is not False
        or list(payload.get("symbol_scope") or []) != list(sealer.SYMBOL_SCOPE)
    ):
        raise CandidateStartRefusal("readiness_contract_invalid")
    return expected


def build_receipt(
    *,
    preregistration: Mapping[str, Any],
    preregistration_file_sha256: str,
    pre_readiness: Mapping[str, Any],
    pre_readiness_file_sha256: str,
    post_readiness: Mapping[str, Any],
    post_readiness_file_sha256: str,
    verified_at: datetime,
) -> dict[str, Any]:
    if not sealer.validate_preregistration(preregistration):
        raise CandidateStartRefusal("preregistration_invalid")
    if verified_at.tzinfo is None:
        raise CandidateStartRefusal("verified_at_timezone_missing")
    verified = verified_at.astimezone(UTC)
    t0 = datetime.fromisoformat(
        str(preregistration["prospective_window"]["t0_utc_inclusive"])
        .replace("Z", "+00:00")
    ).astimezone(UTC)
    if verified < t0:
        raise CandidateStartRefusal("prospective_t0_not_reached")
    pre_sha = _verify_readiness(pre_readiness)
    post_sha = _verify_readiness(post_readiness)
    lineage = preregistration.get("discovery_lineage")
    source_binding = preregistration.get("source_binding")
    if (
        not isinstance(lineage, Mapping)
        or not isinstance(source_binding, Mapping)
        or pre_sha != lineage.get("readiness_payload_sha256")
        or pre_readiness_file_sha256 != lineage.get("readiness_file_sha256")
    ):
        raise CandidateStartRefusal("pre_t0_readiness_binding_mismatch")
    pre_source = str(pre_readiness.get("current_market_source_id") or "")
    post_source = str(post_readiness.get("current_market_source_id") or "")
    source_hash = hashlib.sha256(pre_source.encode("utf-8")).hexdigest()
    if (
        not pre_source
        or post_source != pre_source
        or source_hash != source_binding.get("market_source_id_sha256")
    ):
        raise CandidateStartRefusal("prospective_source_rollover")
    pre_observed = float(pre_readiness.get("observed_at_epoch") or 0.0)
    post_observed = float(post_readiness.get("observed_at_epoch") or 0.0)
    if not (pre_observed < t0.timestamp() <= post_observed <= verified.timestamp()):
        raise CandidateStartRefusal("prospective_observation_bracket_invalid")

    edge_rows: dict[str, Any] = {}
    pre_symbols = pre_readiness.get("symbols")
    post_symbols = post_readiness.get("symbols")
    if not isinstance(pre_symbols, Mapping) or not isinstance(post_symbols, Mapping):
        raise CandidateStartRefusal("readiness_symbol_details_missing")
    for symbol in sealer.SYMBOL_SCOPE:
        before = pre_symbols.get(symbol)
        after = post_symbols.get(symbol)
        if not isinstance(before, Mapping) or not isinstance(after, Mapping):
            raise CandidateStartRefusal("readiness_symbol_details_missing")
        before_last = float(before.get("last_quote_epoch") or 0.0)
        after_last = float(after.get("last_quote_epoch") or 0.0)
        if (
            before_last <= 0.0
            or before_last >= t0.timestamp()
            or after_last < t0.timestamp()
            or after_last < before_last
            or after_last - before_last > MAXIMUM_BRACKET_GAP_SECONDS
        ):
            raise CandidateStartRefusal(f"start_edge_not_bracketed:{symbol}")
        edge_rows[symbol] = {
            "pre_t0_last_quote_epoch": before_last,
            "post_t0_last_quote_epoch": after_last,
            "bracket_gap_seconds": after_last - before_last,
        }

    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "verified_at_utc": verified.isoformat().replace("+00:00", "Z"),
        "preregistration_body_sha256": preregistration[
            "preregistration_body_sha256"
        ],
        "preregistration_file_sha256": preregistration_file_sha256,
        "t0_utc_inclusive": t0.isoformat().replace("+00:00", "Z"),
        "pre_readiness_payload_sha256": pre_sha,
        "pre_readiness_file_sha256": pre_readiness_file_sha256,
        "post_readiness_payload_sha256": post_sha,
        "post_readiness_file_sha256": post_readiness_file_sha256,
        "market_source_id_sha256": source_hash,
        "ordered_symbols": list(sealer.SYMBOL_SCOPE),
        "edge_brackets": edge_rows,
        "collection_continued_across_t0": True,
        "authority": {
            "outcome_access_authorized": False,
            "research_authorized": False,
            "selection_authorized": False,
            "success_claim_authorized": False,
            "activation_authorized": False,
            "runtime_authorized": False,
            "broker_access_authorized": False,
            "order_authorized": False,
        },
    }
    receipt["receipt_body_sha256"] = sealer.canonical_sha256(receipt)
    return receipt


def atomic_publish(*, output_root: str | Path, receipt: Mapping[str, Any]) -> Path:
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    output = root / (
        "ig_tick_microstructure_candidate_start_"
        + str(receipt["receipt_body_sha256"])
        + ".json"
    )
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(receipt, handle, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError:
        raise CandidateStartRefusal("start_receipt_output_exists") from None
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--pre-readiness", required=True)
    parser.add_argument("--post-readiness", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        prereg, prereg_file_sha = _read(
            args.preregistration, reason="preregistration_unreadable"
        )
        pre, pre_file_sha = _read(args.pre_readiness, reason="pre_readiness_unreadable")
        post, post_file_sha = _read(
            args.post_readiness, reason="post_readiness_unreadable"
        )
        receipt = build_receipt(
            preregistration=prereg,
            preregistration_file_sha256=prereg_file_sha,
            pre_readiness=pre,
            pre_readiness_file_sha256=pre_file_sha,
            post_readiness=post,
            post_readiness_file_sha256=post_file_sha,
            verified_at=datetime.now(UTC),
        )
        output = atomic_publish(output_root=args.output_root, receipt=receipt)
    except (CandidateStartRefusal, OSError) as exc:
        print(f"microstructure candidate start refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "receipt_body_sha256": receipt["receipt_body_sha256"],
                "collection_continued_across_t0": True,
                "authority_granted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
