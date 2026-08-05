"""Append an outcome-free continuity checkpoint for the sealed tick candidate.

The tool reads local JSON metadata only.  It proves source identity and monotonic
exact-scope collection, but never reads quotes, outcomes, signals, credentials,
the runtime database, or any broker/order surface.
"""

from __future__ import annotations

# AGENT: ROLE: local append-only collection continuity auditor.
# AGENT: HANDSHAKE: preregistration + T0 receipt + readiness -> checkpoint chain.
# AGENT: ISOLATION: metadata-only local reads; no outcome or trading authority.

import argparse
from datetime import UTC, datetime
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SEALER_PATH = ROOT / "tools" / "seal_ig_tick_microstructure_candidate_preregistration.py"
_SPEC = importlib.util.spec_from_file_location("tick_candidate_sealer", SEALER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("candidate_sealer_import_invalid")
sealer = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sealer)

SCHEMA_VERSION = "fxstack.ig_tick_microstructure_candidate_continuity.v1"
START_RECEIPT_SCHEMA = "fxstack.ig_tick_microstructure_candidate_start_receipt.v1"
CHECKPOINT_PREFIX = "ig_tick_microstructure_candidate_continuity_"


class CandidateContinuityRefusal(RuntimeError):
    pass


def _read(path: str | Path, *, reason: str) -> tuple[dict[str, Any], str, Path]:
    candidate = Path(path).expanduser().absolute()
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise OSError(reason)
        resolved = candidate.resolve(strict=True)
        raw = resolved.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise CandidateContinuityRefusal(reason) from None
    if not isinstance(payload, dict):
        raise CandidateContinuityRefusal(reason)
    return payload, hashlib.sha256(raw).hexdigest(), resolved


def _body_hash(payload: Mapping[str, Any], field: str, reason: str) -> str:
    expected = str(payload.get(field) or "").lower()
    body = dict(payload)
    body.pop(field, None)
    if len(expected) != 64 or sealer.canonical_sha256(body) != expected:
        raise CandidateContinuityRefusal(reason)
    return expected


def _finite(value: Any, reason: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise CandidateContinuityRefusal(reason) from None
    if not math.isfinite(result):
        raise CandidateContinuityRefusal(reason)
    return result


def _verify_start_receipt(
    receipt: Mapping[str, Any], preregistration: Mapping[str, Any]
) -> str:
    receipt_hash = _body_hash(
        receipt, "receipt_body_sha256", "start_receipt_hash_invalid"
    )
    authority = receipt.get("authority")
    if (
        receipt.get("schema_version") != START_RECEIPT_SCHEMA
        or receipt.get("collection_continued_across_t0") is not True
        or receipt.get("preregistration_body_sha256")
        != preregistration.get("preregistration_body_sha256")
        or receipt.get("t0_utc_inclusive")
        != preregistration.get("prospective_window", {}).get("t0_utc_inclusive")
        or list(receipt.get("ordered_symbols") or []) != list(sealer.SYMBOL_SCOPE)
        or not isinstance(authority, Mapping)
        or not authority
        or any(authority.values())
    ):
        raise CandidateContinuityRefusal("start_receipt_contract_invalid")
    return receipt_hash


def _verify_readiness(payload: Mapping[str, Any]) -> str:
    payload_hash = _body_hash(payload, "payload_sha256", "readiness_hash_invalid")
    if (
        payload.get("schema_version") != sealer.READINESS_SCHEMA
        or payload.get("collection_metadata_only") is not True
        or payload.get("source_identity_authenticated") is not True
        or payload.get("database_read_only") is not True
        or payload.get("research_authorized") is not False
        or payload.get("selection_authorized") is not False
        or payload.get("activation_authorized") is not False
        or payload.get("order_authorized") is not False
        or list(payload.get("symbol_scope") or []) != list(sealer.SYMBOL_SCOPE)
    ):
        raise CandidateContinuityRefusal("readiness_contract_invalid")
    return payload_hash


def _verify_previous(payload: Mapping[str, Any]) -> str:
    checkpoint_hash = _body_hash(
        payload, "checkpoint_body_sha256", "previous_checkpoint_hash_invalid"
    )
    authority = payload.get("authority")
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("collection_metadata_only") is not True
        or payload.get("source_continuity_proven") is not True
        or list(payload.get("ordered_symbols") or []) != list(sealer.SYMBOL_SCOPE)
        or not isinstance(authority, Mapping)
        or not authority
        or any(authority.values())
    ):
        raise CandidateContinuityRefusal("previous_checkpoint_contract_invalid")
    return checkpoint_hash


def _latest_checkpoint(root: Path) -> tuple[dict[str, Any], str, str] | None:
    records: list[tuple[int, dict[str, Any], str, str]] = []
    for path in sorted(root.glob(f"{CHECKPOINT_PREFIX}*.json")):
        payload, file_hash, _ = _read(path, reason="checkpoint_chain_unreadable")
        body_hash = _verify_previous(payload)
        sequence = int(payload.get("sequence") or 0)
        if sequence <= 0:
            raise CandidateContinuityRefusal("checkpoint_sequence_invalid")
        records.append((sequence, payload, body_hash, file_hash))
    if not records:
        return None
    records.sort(key=lambda item: item[0])
    binding: tuple[Any, Any, Any] | None = None
    prior_body: str | None = None
    prior_file: str | None = None
    for expected_sequence, (sequence, payload, body_hash, file_hash) in enumerate(
        records, start=1
    ):
        if sequence != expected_sequence:
            raise CandidateContinuityRefusal("checkpoint_chain_sequence_gap_or_fork")
        current_binding = (
            payload.get("preregistration_body_sha256"),
            payload.get("start_receipt_body_sha256"),
            payload.get("market_source_id_sha256"),
        )
        if binding is None:
            binding = current_binding
        elif current_binding != binding:
            raise CandidateContinuityRefusal("checkpoint_chain_binding_mismatch")
        if (
            payload.get("previous_checkpoint_body_sha256") != prior_body
            or payload.get("previous_checkpoint_file_sha256") != prior_file
        ):
            raise CandidateContinuityRefusal("checkpoint_chain_link_mismatch")
        prior_body = body_hash
        prior_file = file_hash
    _, payload, body_hash, file_hash = records[-1]
    return payload, body_hash, file_hash


def build_checkpoint(
    *,
    preregistration: Mapping[str, Any],
    preregistration_file_sha256: str,
    start_receipt: Mapping[str, Any],
    start_receipt_file_sha256: str,
    readiness: Mapping[str, Any],
    readiness_file_sha256: str,
    checked_at: datetime,
    previous: Mapping[str, Any] | None = None,
    previous_body_sha256: str | None = None,
    previous_file_sha256: str | None = None,
) -> dict[str, Any]:
    if not sealer.validate_preregistration(preregistration):
        raise CandidateContinuityRefusal("preregistration_invalid")
    if checked_at.tzinfo is None:
        raise CandidateContinuityRefusal("checked_at_timezone_missing")
    checked = checked_at.astimezone(UTC)
    receipt_hash = _verify_start_receipt(start_receipt, preregistration)
    readiness_hash = _verify_readiness(readiness)
    window = preregistration["prospective_window"]
    t0 = datetime.fromisoformat(str(window["t0_utc_inclusive"]).replace("Z", "+00:00"))
    end = datetime.fromisoformat(str(window["end_utc_exclusive"]).replace("Z", "+00:00"))
    observed = _finite(readiness.get("observed_at_epoch"), "readiness_observed_at_invalid")
    if checked < t0 or checked >= end or not (t0.timestamp() <= observed <= checked.timestamp()):
        raise CandidateContinuityRefusal("checkpoint_outside_prospective_window")
    source = str(readiness.get("current_market_source_id") or "")
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    expected_source_hash = preregistration.get("source_binding", {}).get(
        "market_source_id_sha256"
    )
    if (
        not source
        or source_hash != expected_source_hash
        or source_hash != start_receipt.get("market_source_id_sha256")
    ):
        raise CandidateContinuityRefusal("prospective_source_rollover")

    previous_symbols: Mapping[str, Any] = {}
    previous_sequence = 0
    previous_checked = 0.0
    if previous is not None:
        verified_previous_hash = _verify_previous(previous)
        if verified_previous_hash != previous_body_sha256:
            raise CandidateContinuityRefusal("previous_checkpoint_binding_invalid")
        if (
            previous.get("preregistration_body_sha256")
            != preregistration.get("preregistration_body_sha256")
            or previous.get("start_receipt_body_sha256") != receipt_hash
            or previous.get("market_source_id_sha256") != source_hash
        ):
            raise CandidateContinuityRefusal("checkpoint_chain_binding_mismatch")
        previous_sequence = int(previous.get("sequence") or 0)
        previous_checked = datetime.fromisoformat(
            str(previous.get("checked_at_utc") or "").replace("Z", "+00:00")
        ).timestamp()
        previous_symbols = previous.get("symbols") or {}
        if not isinstance(previous_symbols, Mapping):
            raise CandidateContinuityRefusal("previous_symbol_details_missing")
    if observed < previous_checked:
        raise CandidateContinuityRefusal("readiness_observation_regressed")

    current_symbols = readiness.get("symbols")
    if not isinstance(current_symbols, Mapping):
        raise CandidateContinuityRefusal("readiness_symbol_details_missing")
    symbols: dict[str, Any] = {}
    for symbol in sealer.SYMBOL_SCOPE:
        row = current_symbols.get(symbol)
        prior = previous_symbols.get(symbol) if previous is not None else None
        if not isinstance(row, Mapping):
            raise CandidateContinuityRefusal(f"readiness_symbol_missing:{symbol}")
        observations = int(row.get("observations") or 0)
        first_sequence = int(row.get("first_sequence") or 0)
        last_sequence = int(row.get("last_sequence") or 0)
        last_quote = _finite(
            row.get("last_quote_epoch"), f"last_quote_epoch_invalid:{symbol}"
        )
        if (
            observations <= 0
            or first_sequence <= 0
            or last_sequence < first_sequence
            or last_quote < t0.timestamp()
            or last_quote > observed
        ):
            raise CandidateContinuityRefusal(f"symbol_continuity_invalid:{symbol}")
        if prior is not None:
            if not isinstance(prior, Mapping) or (
                observations < int(prior.get("observations") or 0)
                or first_sequence != int(prior.get("first_sequence") or 0)
                or last_sequence < int(prior.get("last_sequence") or 0)
                or last_quote < float(prior.get("last_quote_epoch") or 0.0)
            ):
                raise CandidateContinuityRefusal(f"symbol_collection_regressed:{symbol}")
        symbols[symbol] = {
            "observations": observations,
            "first_sequence": first_sequence,
            "last_sequence": last_sequence,
            "last_quote_epoch": last_quote,
        }

    checkpoint: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sequence": previous_sequence + 1,
        "checked_at_utc": checked.isoformat().replace("+00:00", "Z"),
        "collection_metadata_only": True,
        "source_continuity_proven": True,
        "preregistration_body_sha256": preregistration["preregistration_body_sha256"],
        "preregistration_file_sha256": preregistration_file_sha256,
        "start_receipt_body_sha256": receipt_hash,
        "start_receipt_file_sha256": start_receipt_file_sha256,
        "readiness_payload_sha256": readiness_hash,
        "readiness_file_sha256": readiness_file_sha256,
        "previous_checkpoint_body_sha256": previous_body_sha256,
        "previous_checkpoint_file_sha256": previous_file_sha256,
        "market_source_id_sha256": source_hash,
        "t0_utc_inclusive": window["t0_utc_inclusive"],
        "end_utc_exclusive": window["end_utc_exclusive"],
        "readiness_observed_at_epoch": observed,
        "ordered_symbols": list(sealer.SYMBOL_SCOPE),
        "symbols": symbols,
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
    checkpoint["checkpoint_body_sha256"] = sealer.canonical_sha256(checkpoint)
    return checkpoint


def _publish(root: Path, checkpoint: Mapping[str, Any]) -> Path:
    output = root / f"{CHECKPOINT_PREFIX}{checkpoint['checkpoint_body_sha256']}.json"
    try:
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(checkpoint, handle, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
            handle.write("\n")
    except FileExistsError:
        raise CandidateContinuityRefusal("continuity_checkpoint_output_exists") from None
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--start-receipt", required=True)
    parser.add_argument("--readiness", required=True)
    parser.add_argument("--checkpoint-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    lock_path: Path | None = None
    lock_fd: int | None = None
    try:
        root = Path(args.checkpoint_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / ".continuity.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        prereg, prereg_file_hash, _ = _read(args.preregistration, reason="preregistration_unreadable")
        receipt, receipt_file_hash, _ = _read(args.start_receipt, reason="start_receipt_unreadable")
        readiness, readiness_file_hash, _ = _read(args.readiness, reason="readiness_unreadable")
        latest = _latest_checkpoint(root)
        checkpoint = build_checkpoint(
            preregistration=prereg,
            preregistration_file_sha256=prereg_file_hash,
            start_receipt=receipt,
            start_receipt_file_sha256=receipt_file_hash,
            readiness=readiness,
            readiness_file_sha256=readiness_file_hash,
            checked_at=datetime.now(UTC),
            previous=latest[0] if latest else None,
            previous_body_sha256=latest[1] if latest else None,
            previous_file_sha256=latest[2] if latest else None,
        )
        output = _publish(root, checkpoint)
    except (CandidateContinuityRefusal, OSError, ValueError) as exc:
        print(f"microstructure candidate continuity refused: {exc}")
        return 2
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_path is not None:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
    print(json.dumps({
        "output": str(output),
        "sequence": checkpoint["sequence"],
        "checkpoint_body_sha256": checkpoint["checkpoint_body_sha256"],
        "source_continuity_proven": True,
        "authority_granted": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
