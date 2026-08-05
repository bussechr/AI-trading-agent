"""Validate the operational continuity boundary around the MTVCLC collector.

This helper is intentionally read-only except for the explicit creation of one
immutable guard-identity file.  It never reads the bridge credential contents,
contacts the bridge, evaluates a signal or outcome, or grants authority.  The
Windows supervisor uses it before starting the hash-bound collector and for
cheap operational health checks between restarts.

The collector remains the authoritative full manifest verifier.  This helper
checks the immutable preregistration with the collector's own validator and
checks only the first and last manifest records plus their referenced chunks;
that bounded tail check is monitoring evidence, not a replacement for the
collector's full restart audit.
"""

from __future__ import annotations

# AGENT: ROLE: bounded, collection-only continuity preflight for the sealed MTVCLC collector.
# AGENT: HANDSHAKE: immutable preregistration + output tail + guard identity -> Windows supervisor.
# AGENT: ISOLATION: no bridge access, credential read, strategy evaluation, issuer, runtime, or order path.
# AGENT: SIDE EFFECTS: optional exclusive publication of one read-only guard-identity file.

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
from typing import Any, Mapping, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import capture_ig_mt4_m1_activity as collector  # noqa: E402


GUARD_SCHEMA_VERSION = "fxstack.mtvclc_collector_guard_identity.v1"
INSPECTION_SCHEMA_VERSION = "fxstack.mtvclc_collector_continuity_inspection.v1"
GUARD_IDENTITY_FILENAME = "collector-guard.identity.v1.json"
MAXIMUM_MANIFEST_LINE_BYTES = 512 * 1024
_ZERO_SHA256 = "0" * 64


class ContinuityRefusal(RuntimeError):
    """Fail-closed refusal with a stable, non-secret reason."""


@dataclass(frozen=True, slots=True)
class GuardPolicy:
    tick_interval_secs: float
    bar_interval_secs: float
    bar_limit: int
    http_timeout_secs: float
    rollover_mode: str = "refuse"

    def validated(self) -> "GuardPolicy":
        if self.rollover_mode != "refuse":
            raise ContinuityRefusal("guard_rollover_mode_must_refuse")
        try:
            policy = collector.CollectionPolicy(
                bar_limit=self.bar_limit,
                tick_interval_secs=self.tick_interval_secs,
                bar_interval_secs=self.bar_interval_secs,
                rollover_mode="refuse",
            )
            policy.validate()
            timeout = collector._positive_float(
                self.http_timeout_secs,
                "http_timeout_invalid",
            )
        except collector.CollectionRefusal as exc:
            raise ContinuityRefusal(str(exc)) from None
        return GuardPolicy(
            tick_interval_secs=float(policy.tick_interval_secs),
            bar_interval_secs=float(policy.bar_interval_secs),
            bar_limit=int(policy.bar_limit),
            http_timeout_secs=float(timeout),
            rollover_mode="refuse",
        )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContinuityRefusal("guard_payload_not_canonical") from exc


def _canonical_sha256(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _strict_json_object(raw: bytes, reason: str) -> dict[str, Any]:
    try:
        parsed = collector._strict_json_object(raw, reason=reason)
    except collector.CollectionRefusal as exc:
        raise ContinuityRefusal(reason) from exc
    return dict(parsed)


def _resolved_existing_file(path: str | Path, reason: str) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_file() or target.is_symlink() or _is_reparse_point(target):
        raise ContinuityRefusal(reason)
    return target


def _resolved_existing_directory(path: str | Path, reason: str) -> Path:
    target = Path(path).expanduser().resolve(strict=False)
    if not target.is_dir() or target.is_symlink() or _is_reparse_point(target):
        raise ContinuityRefusal(reason)
    return target


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return True
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validated_api_key_file(path: str | Path) -> Path:
    target = _resolved_existing_file(path, "bridge_api_key_file_invalid")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise ContinuityRefusal("bridge_api_key_file_unreadable") from exc
    if size <= 0 or size > collector.MAXIMUM_API_KEY_BYTES:
        raise ContinuityRefusal("bridge_api_key_file_size_invalid")
    # Deliberately do not read or hash the credential bytes.  The collector
    # opens the pinned file only inside its own process.
    return target


def _read_manifest_edge_lines(path: Path) -> tuple[bytes, bytes]:
    try:
        size = path.stat().st_size
        if size <= 0:
            raise ContinuityRefusal("manifest_empty")
        with path.open("rb") as handle:
            first = handle.readline(MAXIMUM_MANIFEST_LINE_BYTES + 1)
            if not first.endswith(b"\n") or len(first) > MAXIMUM_MANIFEST_LINE_BYTES:
                raise ContinuityRefusal("manifest_edge_line_invalid")
            scan_size = min(size, MAXIMUM_MANIFEST_LINE_BYTES + 1)
            handle.seek(size - scan_size)
            tail = handle.read(scan_size)
    except OSError as exc:
        raise ContinuityRefusal("manifest_unreadable") from exc
    if not tail.endswith(b"\n"):
        raise ContinuityRefusal("manifest_trailing_partial_line")
    stripped = tail[:-1]
    separator = stripped.rfind(b"\n")
    last = stripped[separator + 1 :] + b"\n"
    if not last.strip() or len(last) > MAXIMUM_MANIFEST_LINE_BYTES:
        raise ContinuityRefusal("manifest_edge_line_invalid")
    return first, last


def _validated_binding_tuple(
    value: Mapping[str, Any],
    *,
    binding: collector.ProspectiveBinding,
    reason: str,
) -> None:
    expected = (
        binding.preregistration_body_sha256,
        binding.preregistration_artifact_sha256,
        binding.t0_utc,
        binding.end_utc_exclusive,
    )
    try:
        actual = collector._validated_binding_tuple(value, reason=reason)
    except collector.CollectionRefusal as exc:
        raise ContinuityRefusal(reason) from exc
    if actual != expected:
        raise ContinuityRefusal(reason)


def _validate_manifest_entry(
    raw: bytes,
    *,
    binding: collector.ProspectiveBinding,
    output_root: Path,
    require_first: bool,
) -> dict[str, Any]:
    parsed = _strict_json_object(raw, "manifest_edge_json_invalid")
    body = dict(parsed)
    claimed = str(body.pop("manifest_entry_sha256", "")).strip().lower()
    if not collector._is_sha256(claimed) or _canonical_sha256(body) != claimed:
        raise ContinuityRefusal("manifest_edge_hash_invalid")
    if body.get("schema_version") != collector.MANIFEST_SCHEMA_VERSION:
        raise ContinuityRefusal("manifest_edge_schema_invalid")
    sequence = body.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ContinuityRefusal("manifest_edge_sequence_invalid")
    previous = str(body.get("previous_entry_sha256") or "").lower()
    if not collector._is_sha256(previous):
        raise ContinuityRefusal("manifest_edge_chain_invalid")
    if require_first and (sequence != 1 or previous != _ZERO_SHA256):
        raise ContinuityRefusal("manifest_first_entry_invalid")
    _validated_binding_tuple(
        body,
        binding=binding,
        reason="manifest_preregistration_binding_invalid",
    )
    relative = str(body.get("chunk_path") or "")
    pure = PurePosixPath(relative)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or not relative.startswith(f"{collector.CHUNK_DIRECTORY}/")
    ):
        raise ContinuityRefusal("manifest_chunk_path_invalid")
    chunk_path = output_root.joinpath(*pure.parts)
    try:
        chunk_bytes = chunk_path.read_bytes()
    except OSError as exc:
        raise ContinuityRefusal("manifest_chunk_missing") from exc
    if len(chunk_bytes) != body.get("chunk_size_bytes"):
        raise ContinuityRefusal("manifest_chunk_size_mismatch")
    if _sha256_bytes(chunk_bytes) != body.get("chunk_sha256"):
        raise ContinuityRefusal("manifest_chunk_hash_mismatch")
    chunk = _strict_json_object(chunk_bytes, "manifest_chunk_invalid")
    _validated_binding_tuple(
        chunk,
        binding=binding,
        reason="manifest_chunk_preregistration_binding_invalid",
    )
    if (
        chunk.get("schema_version") != collector.CHUNK_SCHEMA_VERSION
        or chunk.get("collection_only") is not True
        or chunk.get("evaluation_performed") is not False
        or chunk.get("success_claim_authorized") is not False
        or chunk.get("authority_granted") is not False
        or chunk.get("activation_authorized") is not False
        or chunk.get("order_authorized") is not False
    ):
        raise ContinuityRefusal("manifest_chunk_collection_boundary_invalid")
    source = chunk.get("source")
    if not isinstance(source, Mapping):
        raise ContinuityRefusal("manifest_chunk_source_invalid")
    source_id = str(source.get("market_source_id") or "").strip().lower()
    if not collector._is_sha256(source_id):
        raise ContinuityRefusal("manifest_chunk_source_invalid")
    if source_id != str(body.get("market_source_id") or "").strip().lower():
        raise ContinuityRefusal("manifest_entry_chunk_source_mismatch")
    return {
        **body,
        "manifest_entry_sha256": claimed,
        "market_source_id": source_id,
    }


def inspect_manifest_tail(
    output_root: Path,
    *,
    binding: collector.ProspectiveBinding,
) -> dict[str, Any]:
    manifest = output_root / collector.MANIFEST_FILENAME
    if not manifest.exists():
        return {
            "manifest_present": False,
            "manifest_sequence": 0,
            "manifest_last_entry_sha256": _ZERO_SHA256,
            "manifest_last_market_source_id": "",
            "manifest_last_write_epoch": None,
            "manifest_tail_check_only": True,
        }
    if manifest.is_symlink() or _is_reparse_point(manifest):
        raise ContinuityRefusal("manifest_symlink_forbidden")
    if not manifest.is_file():
        raise ContinuityRefusal("manifest_invalid")
    first_raw, last_raw = _read_manifest_edge_lines(manifest)
    first = _validate_manifest_entry(
        first_raw,
        binding=binding,
        output_root=output_root,
        require_first=True,
    )
    last = (
        first
        if first_raw == last_raw
        else _validate_manifest_entry(
            last_raw,
            binding=binding,
            output_root=output_root,
            require_first=False,
        )
    )
    if int(last["sequence"]) < int(first["sequence"]):
        raise ContinuityRefusal("manifest_edge_sequence_invalid")
    if last["market_source_id"] != first["market_source_id"]:
        raise ContinuityRefusal("manifest_market_source_changed")
    try:
        modified = manifest.stat().st_mtime
    except OSError as exc:
        raise ContinuityRefusal("manifest_unreadable") from exc
    return {
        "manifest_present": True,
        "manifest_sequence": int(last["sequence"]),
        "manifest_last_entry_sha256": str(last["manifest_entry_sha256"]),
        "manifest_last_market_source_id": str(last["market_source_id"]),
        "manifest_last_write_epoch": float(modified),
        "manifest_tail_check_only": True,
    }


def _guard_identity(
    *,
    collector_path: Path,
    preregistration_path: Path,
    output_root: Path,
    api_key_file: Path,
    base_url: str,
    binding: collector.ProspectiveBinding,
    policy: GuardPolicy,
) -> dict[str, Any]:
    collector_sha256 = _sha256_bytes(collector_path.read_bytes())
    return {
        "schema_version": GUARD_SCHEMA_VERSION,
        "collection_only": True,
        "evaluation_performed": False,
        "signal_computation_authorized": False,
        "performance_computation_authorized": False,
        "success_claim_authorized": False,
        "authority_granted": False,
        "activation_authorized": False,
        "order_authorized": False,
        "collector_source_path": str(collector_path),
        "collector_source_sha256": collector_sha256,
        "preregistration_path": str(preregistration_path),
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": (
            binding.preregistration_artifact_sha256
        ),
        "output_root": str(output_root),
        "api_key_file_path": str(api_key_file),
        "bridge_base_url": base_url,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "policy": asdict(policy),
        "resume_contract": {
            "same_preregistration_required": True,
            "same_output_root_required": True,
            "same_collector_source_required": True,
            "market_source_rollover_refused": True,
            "t0_reset_forbidden": True,
            "observed_gaps_preserved": True,
        },
    }


def _publish_guard_identity(path: Path, payload: Mapping[str, Any]) -> None:
    expected = _canonical_json_bytes(payload) + b"\n"
    path.parent.mkdir(parents=False, exist_ok=True)
    temporary: Path | None = None
    try:
        descriptor, raw_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary = Path(raw_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(expected)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, path)
            temporary.unlink()
            temporary = None
            if path.read_bytes() != expected:
                raise ContinuityRefusal("guard_identity_publish_verify_failed")
            path.chmod(stat.S_IREAD)
        except FileExistsError:
            raise ContinuityRefusal("guard_identity_already_exists") from None
    except OSError as exc:
        raise ContinuityRefusal("guard_identity_publish_failed") from exc
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.chmod(stat.S_IWRITE | stat.S_IREAD)
                temporary.unlink()
            except OSError:
                pass


def _validate_or_initialize_guard(
    path: Path,
    *,
    expected: Mapping[str, Any],
    initialize: bool,
    require: bool,
) -> bool:
    expected_bytes = _canonical_json_bytes(expected) + b"\n"
    if not path.exists():
        if initialize:
            _publish_guard_identity(path, expected)
            return True
        if require:
            raise ContinuityRefusal("guard_identity_missing")
        return False
    if not path.is_file() or path.is_symlink() or _is_reparse_point(path):
        raise ContinuityRefusal("guard_identity_invalid")
    try:
        actual = path.read_bytes()
    except OSError as exc:
        raise ContinuityRefusal("guard_identity_unreadable") from exc
    if actual != expected_bytes:
        raise ContinuityRefusal("guard_identity_mismatch")
    return True


def inspect_continuity(
    *,
    preregistration: str | Path,
    output_dir: str | Path,
    api_key_file: str | Path,
    base_url: str,
    policy: GuardPolicy,
    initialize_guard: bool = False,
    require_guard: bool = False,
) -> dict[str, Any]:
    if initialize_guard and require_guard:
        raise ContinuityRefusal("guard_mode_conflict")
    collector_path = collector.TOOL_PATH.resolve(strict=True)
    if collector_path != (REPOSITORY_ROOT / "tools" / collector_path.name).resolve():
        raise ContinuityRefusal("collector_source_path_invalid")
    preregistration_path = _resolved_existing_file(
        preregistration,
        "preregistration_file_invalid",
    )
    output_root = _resolved_existing_directory(output_dir, "output_root_invalid")
    if output_root == Path(output_root.anchor) or _is_within(
        output_root,
        REPOSITORY_ROOT,
    ):
        raise ContinuityRefusal("output_root_must_be_external")
    api_key_path = _validated_api_key_file(api_key_file)
    try:
        binding = collector.load_preregistration(preregistration_path)
        normalized_url = collector._validated_loopback_base_url(base_url)
    except collector.CollectionRefusal as exc:
        raise ContinuityRefusal(str(exc)) from None
    validated_policy = policy.validated()
    manifest = inspect_manifest_tail(output_root, binding=binding)
    if (
        manifest["manifest_present"]
        and validated_policy.rollover_mode == "refuse"
        and not manifest["manifest_last_market_source_id"]
    ):
        raise ContinuityRefusal("manifest_market_source_missing")
    identity = _guard_identity(
        collector_path=collector_path,
        preregistration_path=preregistration_path,
        output_root=output_root,
        api_key_file=api_key_path,
        base_url=normalized_url,
        binding=binding,
        policy=validated_policy,
    )
    identity_path = output_root / GUARD_IDENTITY_FILENAME
    present = _validate_or_initialize_guard(
        identity_path,
        expected=identity,
        initialize=initialize_guard,
        require=require_guard,
    )
    return {
        "schema_version": INSPECTION_SCHEMA_VERSION,
        "status": "continuity_preflight_passed",
        "collection_only": True,
        "evaluation_performed": False,
        "signal_computation_authorized": False,
        "performance_computation_authorized": False,
        "authority_granted": False,
        "activation_authorized": False,
        "order_authorized": False,
        "collector_source_path": str(collector_path),
        "collector_source_sha256": identity["collector_source_sha256"],
        "preregistration_path": str(preregistration_path),
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": (
            binding.preregistration_artifact_sha256
        ),
        "output_root": str(output_root),
        "api_key_file_path": str(api_key_path),
        "bridge_base_url": normalized_url,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_t0_epoch": binding.t0_epoch,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "prospective_end_epoch_exclusive": binding.end_epoch_exclusive,
        "policy": asdict(validated_policy),
        "guard_identity_path": str(identity_path),
        "guard_identity_present": present,
        "guard_identity_sha256": _canonical_sha256(identity),
        **manifest,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check the sealed MTVCLC collector continuity boundary."
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tick-interval-secs", type=float, default=2.0)
    parser.add_argument("--bar-interval-secs", type=float, default=60.0)
    parser.add_argument("--bar-limit", type=int, default=400)
    parser.add_argument("--http-timeout-secs", type=float, default=5.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--initialize-guard", action="store_true")
    mode.add_argument("--require-guard", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = inspect_continuity(
            preregistration=args.preregistration,
            output_dir=args.output_dir,
            api_key_file=args.api_key_file,
            base_url=args.base_url,
            policy=GuardPolicy(
                tick_interval_secs=args.tick_interval_secs,
                bar_interval_secs=args.bar_interval_secs,
                bar_limit=args.bar_limit,
                http_timeout_secs=args.http_timeout_secs,
            ),
            initialize_guard=bool(args.initialize_guard),
            require_guard=bool(args.require_guard),
        )
    except ContinuityRefusal as exc:
        print(f"continuity refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, allow_nan=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
