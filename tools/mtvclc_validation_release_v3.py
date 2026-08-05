"""Two-phase release ceremony for ledger-authenticated MTVCLC gap-v3 evidence.

Public inputs are revalidated before any explicitly supplied signing-key path
can be opened.  The release derives all cell claims from strict one-to-one
reservation/outcome ledgers through the frozen v3 screen, binds v3 filenames
and source identities, and emits only the v2 public evidence contract.
"""

from __future__ import annotations

# AGENT: ROLE: isolated prepare/issue ceremony for gap-v3 evidence.
# AGENT: HANDSHAKE: exact public artifacts -> unsigned request -> signed v2 bundle.
# AGENT: ISOLATION: public files first; explicit key access only after revalidation.
import argparse
import json
import os
import stat
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for _path in (REPO_ROOT, FXSTACK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TOOL_PATH = Path(__file__).resolve()
HANDOFF_PATH = REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"
LEGACY_RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release.py"
EVALUATOR_PATH = REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"
PUBLIC_VERIFIER_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "mtvclc_validation_evidence_v2.py"
)
ISSUANCE_REQUEST_SCHEMA = "fxstack.scalp.mtvclc_issuance_request.v2"
POST_WINDOW_REPORT_SCHEMA = "fxstack.scalp.mtvclc_post_window_report.v3"
POST_WINDOW_EVALUATOR_SCHEMA = "fxstack.scalp.mtvclc_post_window_evaluator.v2"
LEDGER_ROW_SCHEMA = "fxstack.scalp.mtvclc_research_ledger_row.v2"


def _bootstrap_exact_module(path: Path, *, module_name: str) -> ModuleType:
    candidate = path.absolute()
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        if (
            candidate.is_symlink()
            or int(getattr(before_path, "st_file_attributes", 0)) & marker
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > 8 * 1024 * 1024
        ):
            raise OSError("bootstrap_source_invalid")
        descriptor = os.open(candidate, flags)
        try:
            before_handle = os.fstat(descriptor)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(before_handle.st_size + 1)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(f"exact_source_import_invalid:{candidate.name}") from exc
    def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
        return (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )
    if (
        len(
            {
                identity(value)
                for value in (
                    before_path,
                    before_handle,
                    after_handle,
                    after_path,
                )
            }
        )
        != 1
        or len(raw) != before_handle.st_size
    ):
        raise RuntimeError(f"exact_source_import_invalid:{candidate.name}")
    module = ModuleType(module_name)
    module.__file__ = str(candidate)
    module.__package__ = ""
    module.__dict__["__fxstack_exact_source_path__"] = candidate
    module.__dict__["__fxstack_exact_source_raw__"] = raw
    module.__dict__["__fxstack_exact_source_stat_identity__"] = identity(
        before_handle
    )
    missing = object()
    previous = sys.modules.get(module_name, missing)
    sys.modules[module_name] = module
    try:
        exec(  # noqa: S102 - exact descriptor snapshot; workspace pyc forbidden
            compile(raw, str(candidate), "exec", dont_inherit=True),
            module.__dict__,
        )
    except Exception as exc:
        raise RuntimeError(f"exact_source_import_invalid:{candidate.name}") from exc
    finally:
        if previous is missing:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous  # type: ignore[assignment]
    return module


handoff = _bootstrap_exact_module(
    HANDOFF_PATH,
    module_name="_fxstack_mtvclc_gap_v3_release_handoff",
)
sealer = handoff.sealer
screen = sealer.screen
RELEASE_SOURCE_IMAGE = handoff._read_exact_source(
    TOOL_PATH,
    reason="release_source_import_invalid",
)
PUBLIC_VERIFIER_SOURCE_IMAGE = handoff._read_exact_source(
    PUBLIC_VERIFIER_PATH,
    reason="public_verifier_source_import_invalid",
)
public = handoff._execute_exact_source(
    PUBLIC_VERIFIER_SOURCE_IMAGE,
    module_name="_fxstack_mtvclc_gap_v3_public_verifier",
)
LEGACY_RELEASE_SOURCE_IMAGE = handoff._read_exact_source(
    LEGACY_RELEASE_PATH,
    reason="legacy_release_source_import_invalid",
)


def _load_private_core() -> Any:
    name = "_fxstack_mtvclc_gap_v3_release_core"
    return handoff._execute_exact_source(
        LEGACY_RELEASE_SOURCE_IMAGE,
        module_name=name,
        injected_modules={
            "fxstack.providers.ig_mt4_catalog": sealer._catalog,
            "fxstack.runtime.mtvclc_validation_evidence": public._v1,
            "tools.verify_mt4_tick_volume_capture_handoff": handoff._core,
        },
    )


_core = _load_private_core()
MTVCLCReleaseRefusal = _core.MTVCLCReleaseRefusal
ValidatedPublicInputs = _core.ValidatedPublicInputs
LoadedFile = _core.LoadedFile

_core.handoff = handoff
_core.EVALUATOR_PATH = EVALUATOR_PATH
_core.ISSUANCE_REQUEST_SCHEMA = ISSUANCE_REQUEST_SCHEMA
_core.POST_WINDOW_REPORT_SCHEMA = POST_WINDOW_REPORT_SCHEMA
_core.POST_WINDOW_EVALUATOR_SCHEMA = POST_WINDOW_EVALUATOR_SCHEMA
_core.LEDGER_ROW_SCHEMA = LEDGER_ROW_SCHEMA
_core.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA = public.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA
_core.MTVCLC_VALIDATION_EVIDENCE_SCHEMA = public.MTVCLC_VALIDATION_EVIDENCE_SCHEMA
_core.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA = public.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA
_core.EXPECTED_ATTEMPT_ACCOUNTING = dict(public.EXPECTED_ATTEMPT_ACCOUNTING)
_core.EXPECTED_EXECUTION_CONTRACT = dict(public.EXPECTED_EXECUTION_CONTRACT)
_core.EXPECTED_SEALED_GATES = dict(public.EXPECTED_SEALED_GATES)
_core.WILSON_FAMILY_ATTEMPTED_CELLS = public.WILSON_FAMILY_ATTEMPTED_CELLS
_core.WILSON_ALPHA_ALLOCATION = public.WILSON_ALPHA_ALLOCATION
_core.WILSON_INTERVAL_METHOD = public.WILSON_INTERVAL_METHOD
_core.NO_RUNTIME_AUTHORITY = dict(public.NO_RUNTIME_AUTHORITY)
_core.canonical_json_bytes = public.canonical_json_bytes
_core.canonical_sha256 = public.canonical_sha256
_core.certificate_body_sha256 = public.certificate_body_sha256
_core.bundle_body_sha256 = public.bundle_body_sha256
_core.ed25519_public_key_id = public.ed25519_public_key_id
_core.verify_mtvclc_validation_evidence = public.verify_mtvclc_validation_evidence
_core.MTVCLCValidationExpectation = public.MTVCLCValidationExpectation


def executed_source_identities() -> dict[str, dict[str, Any]]:
    """Return exact source identities executed by the release boundary."""

    return {
        **handoff.executed_source_identities(),
        **public.EXECUTED_DEPENDENCY_IDENTITIES,
        "release_source": RELEASE_SOURCE_IMAGE.identity(),
        "legacy_release_support_source": LEGACY_RELEASE_SOURCE_IMAGE.identity(),
        "public_verifier_source": PUBLIC_VERIFIER_SOURCE_IMAGE.identity(),
    }


def _assert_executed_sources_unchanged() -> None:
    paths = sealer._source_paths()
    for label, identity in executed_source_identities().items():
        path = paths.get(label)
        if path is None:
            raise MTVCLCReleaseRefusal(f"executed_source_path_missing:{label}")
        try:
            current = handoff._read_exact_source(
                path,
                reason=f"executed_source_changed:{label}",
            )
        except RuntimeError as exc:
            raise MTVCLCReleaseRefusal(f"executed_source_changed:{label}") from exc
        if current.identity() != identity:
            raise MTVCLCReleaseRefusal(f"executed_source_changed:{label}")


_original_write_new_json = _core._write_new_json


def _write_new_json_source_bound(path: Any, payload: Any) -> Any:
    _assert_executed_sources_unchanged()
    return _original_write_new_json(path, payload)


_core._write_new_json = _write_new_json_source_bound
_original_load_ledger = _core._load_ledger
_original_validate_public_inputs = _core.validate_public_inputs
_current_reservations: list[dict[str, Any]] | None = None
_current_screen_sha256 = ""
_strict_ledger_validation_complete = False
_validation_lock = threading.RLock()


def _load_preregistration_v3(
    path: str | Path,
) -> tuple[LoadedFile, dict[str, Any]]:
    global _current_screen_sha256
    loaded = _core._read_regular(
        path,
        label="preregistration",
        limit=_core.MAX_JSON_BYTES,
    )
    payload = _core._json_object(loaded, label="preregistration")
    if not sealer.validate_preregistration(payload):
        raise MTVCLCReleaseRefusal("preregistration_public_contract_invalid")
    try:
        binding = handoff.load_preregistration(loaded.path)
    except handoff.HandoffRefusal as exc:
        raise MTVCLCReleaseRefusal("preregistration_contract_invalid") from exc
    identities = payload.get("source_identities")
    if not isinstance(identities, Mapping) or any(
        dict(identities.get(label) or {}) != identity
        for label, identity in executed_source_identities().items()
    ):
        raise MTVCLCReleaseRefusal("executed_source_identity_invalid")
    digest = str(payload.get("preregistration_body_sha256") or "").lower()
    if (
        loaded.path.name != f"mtvclc_gap_v3_preregistration_{digest}.json"
        or binding.preregistration_body_sha256 != digest
        or binding.preregistration_artifact_sha256 != loaded.sha256
    ):
        raise MTVCLCReleaseRefusal("preregistration_identity_invalid")
    identity = payload.get("source_identities", {}).get("screen_source", {})
    _current_screen_sha256 = str(identity.get("sha256") or "").lower()
    if _current_screen_sha256 != sealer.stable_snapshot(
        sealer.SCREEN_PATH, reason="screen_source_changed"
    ).sha256:
        raise MTVCLCReleaseRefusal("screen_source_identity_invalid")
    return loaded, payload


def _load_handoff_v3(
    path: str | Path,
    *,
    preregistration: Mapping[str, Any],
    preregistration_file: LoadedFile,
) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _core._read_regular(path, label="handoff", limit=_core.MAX_JSON_BYTES)
    try:
        binding = handoff.load_preregistration(preregistration_file.path)
        payload, artifact_sha = handoff.load_handoff_artifact(
            loaded.path,
            binding=binding,
        )
    except handoff.HandoffRefusal as exc:
        raise MTVCLCReleaseRefusal("handoff_contract_invalid") from exc
    if artifact_sha != loaded.sha256:
        raise MTVCLCReleaseRefusal("handoff_identity_invalid")
    inventory = payload.get("capture_inventory")
    if (
        not isinstance(inventory, Mapping)
        or inventory.get("preregistration_body_sha256")
        != preregistration.get("preregistration_body_sha256")
        or inventory.get("preregistration_artifact_sha256")
        != preregistration_file.sha256
    ):
        raise MTVCLCReleaseRefusal("handoff_contract_invalid")
    return loaded, payload


def _load_report_v3(path: str | Path) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _core._read_regular(path, label="report", limit=_core.MAX_JSON_BYTES)
    report = _core._json_object(loaded, label="report")
    body = dict(report)
    claimed = str(body.pop("report_body_sha256", "")).lower()
    if (
        set(report) != _core._REPORT_FIELDS
        or not _core._is_sha256(claimed)
        or claimed != public.canonical_sha256(body)
        or loaded.path.name != f"mtvclc_post_window_report_v3_{loaded.sha256}.json"
        or loaded.raw != public.canonical_json_bytes(report) + b"\n"
    ):
        raise MTVCLCReleaseRefusal("report_identity_invalid")
    return loaded, report


def _load_ledger_v3(*args: Any, **kwargs: Any) -> Any:
    global _current_reservations
    loaded, records = _original_load_ledger(*args, **kwargs)
    if kwargs.get("kind") == "reservation":
        _current_reservations = records
    return loaded, records


def _screen_costs(
    cost_rows: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for symbol in handoff.SYMBOLS:
        row = cost_rows[symbol]
        calibration = screen.MT4CostCalibration(
            symbol=symbol,
            p90_spread_bps=float(row["p90_ig_spread_bps"]),
            commission_bps_per_round_trip=float(
                row["commission_bps_per_round_trip"]
            ),
            financing_bps_per_trade=float(row["financing_bps_per_trade"]),
            account_currency=str(row["account_currency"]),
            pnl_currency=str(row["profit_loss_currency"]),
            convert_on_close_charge_fraction=float(
                row["convert_on_close_charge_fraction_for_screen"]
            ),
            source_sha256="0" * 64,
            adverse_execution_debit_bps=float(
                row["fixed_adverse_execution_debit_bps"]
            ),
        )
        if not screen.validate_cost_calibration(calibration, expected_symbol=symbol):
            raise MTVCLCReleaseRefusal(f"sealed_cost_screen_parity_failed:{symbol}")
        result[symbol] = asdict(calibration)
    return result


def _derive_cell_evidence_v3(
    *,
    cell_records: Sequence[Mapping[str, Any]],
    outcome_records: Sequence[Mapping[str, Any]],
    cost_rows: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int, int]:
    global _strict_ledger_validation_complete
    if _current_reservations is None:
        raise MTVCLCReleaseRefusal("reservation_ledger_missing")
    costs = _screen_costs(cost_rows)
    ready = {symbol: True for symbol in handoff.SYMBOLS}
    cells = screen.recompute_cells_from_ledgers(
        reservation_ledger=_current_reservations,
        outcome_ledger=list(outcome_records),
        costs=costs,
        source_ready_by_symbol=ready,
    )
    if cells is None:
        raise MTVCLCReleaseRefusal("reservation_outcome_ledgers_invalid")
    if list(cell_records) != cells:
        raise MTVCLCReleaseRefusal("cell_ledger_not_exclusively_recomputed")
    screen_bundle = {
        "schema_version": screen.SCREEN_RESULT_SCHEMA,
        "strategy_id": screen.STRATEGY_ID,
        "strategy_version": screen.STRATEGY_VERSION,
        "config_ids": [screen.CONFIG_ID],
        "symbol_scope": list(screen.MTVCLC_SYMBOLS),
        "source_contract_id": screen.SOURCE_CONTRACT_ID,
        "activity_metric_id": screen.ACTIVITY_METRIC_ID,
        "attempt_accounting": dict(public.EXPECTED_ATTEMPT_ACCOUNTING),
        "source_scope_ready": True,
        "source_ready_by_symbol": ready,
        "source_sha256_by_symbol": {
            symbol: str(costs[symbol]["source_sha256"])
            for symbol in handoff.SYMBOLS
        },
        "source_errors": [],
        "costs": costs,
        "cells": cells,
        "reservation_ledger": list(_current_reservations),
        "outcome_ledger": list(outcome_records),
        "all_cells_pass_fixed_screen": all(
            cell["passes_fixed_cell_screen"] for cell in cells
        ),
        "attempt_manifest": screen.attempt_manifest(),
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "order_authorized": False,
    }
    if not screen.validate_result_bundle(screen_bundle):
        raise MTVCLCReleaseRefusal("screen_result_bundle_invalid")
    if any(cell["passes_fixed_cell_screen"] is not True for cell in cells):
        failed = next(
            cell for cell in cells if cell["passes_fixed_cell_screen"] is not True
        )
        raise MTVCLCReleaseRefusal(
            f"sealed_cell_gate_failed:{failed['symbol']}:{failed['side']}"
        )
    days = {
        str(row.get("entry_day") or "")
        for row in outcome_records
        if str(row.get("entry_day") or "")
    }
    _strict_ledger_validation_complete = True
    return cells, len(outcome_records), len(days)


def _ledger_authentication(evidence: Mapping[str, Any]) -> dict[str, Any]:
    artifacts = evidence.get("artifacts")
    overall = evidence.get("overall")
    if not isinstance(artifacts, Mapping) or not isinstance(overall, Mapping):
        raise MTVCLCReleaseRefusal("public_evidence_contract_invalid")
    total = int(overall.get("total_trades") or 0)
    return {
        "schema_version": public.LEDGER_AUTHENTICATION_SCHEMA,
        "reservation_rows": total,
        "outcome_rows": total,
        "cell_rows": 44,
        "duplicate_reservation_keys": 0,
        "duplicate_outcome_keys": 0,
        "missing_outcomes": 0,
        "orphan_outcomes": 0,
        "inconsistent_pairs": 0,
        "empty_ledgers_rejected": True,
        "cell_summaries_recomputed_exclusively_from_ledgers": True,
        "screen_result_bundle_validated": True,
        "reservation_ledger_sha256": artifacts["reservation_ledger_sha256"],
        "outcome_ledger_sha256": artifacts["outcome_ledger_sha256"],
        "cell_ledger_sha256": artifacts["cell_ledger_sha256"],
        "screen_source_sha256": _current_screen_sha256,
    }


def _pre_return_evidence_error(value: Any) -> str:
    if not isinstance(value, Mapping) or not _strict_ledger_validation_complete:
        return "mtvclc_evidence_ledger_authentication_invalid"
    candidate = deepcopy(dict(value))
    candidate["ledger_authentication"] = _ledger_authentication(candidate)
    return public.mtvclc_evidence_error(candidate)


_core._load_preregistration = _load_preregistration_v3
_core._load_handoff = _load_handoff_v3
_core._load_report = _load_report_v3
_core._load_ledger = _load_ledger_v3
_core._derive_cell_evidence = _derive_cell_evidence_v3
_core.mtvclc_evidence_error = _pre_return_evidence_error


def _validate_public_inputs_serialized(**kwargs: Any) -> ValidatedPublicInputs:
    global _current_reservations, _strict_ledger_validation_complete
    _current_reservations = None
    _strict_ledger_validation_complete = False
    validated = _original_validate_public_inputs(**kwargs)
    evidence = deepcopy(validated.evidence)
    evidence["ledger_authentication"] = _ledger_authentication(evidence)
    error = public.mtvclc_evidence_error(evidence)
    if error:
        raise MTVCLCReleaseRefusal(f"public_evidence_contract_failed:{error}")
    return ValidatedPublicInputs(
        evidence=evidence,
        input_manifest=validated.input_manifest,
        prospective_end_epoch=validated.prospective_end_epoch,
    )


def validate_public_inputs(**kwargs: Any) -> ValidatedPublicInputs:
    """Serialize the legacy callback adapter so ledger state cannot interleave."""

    with _validation_lock:
        return _validate_public_inputs_serialized(**kwargs)


_core.validate_public_inputs = validate_public_inputs
canonical_json_bytes = public.canonical_json_bytes
canonical_sha256 = public.canonical_sha256


def prepare_issuance_request(
    *,
    public_input_paths: Mapping[str, str | Path],
    generation_id: str,
    validity_secs: float,
    output_path: str | Path,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    return _core.prepare_issuance_request(
        public_input_paths=public_input_paths,
        generation_id=generation_id,
        validity_secs=validity_secs,
        output_path=output_path,
        now_epoch=now_epoch,
    )


def issue_validation_bundle(
    *,
    request_path: str | Path,
    public_input_paths: Mapping[str, str | Path],
    signing_key_path: str | Path,
    verification_key_path: str | Path,
    output_path: str | Path,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    return _core.issue_validation_bundle(
        request_path=request_path,
        public_input_paths=public_input_paths,
        signing_key_path=signing_key_path,
        verification_key_path=verification_key_path,
        output_path=output_path,
        now_epoch=now_epoch,
    )


def _public_args(args: argparse.Namespace) -> dict[str, str]:
    return {
        "preregistration_path": args.preregistration,
        "handoff_path": args.handoff,
        "report_path": args.report,
        "reservation_ledger_path": args.reservation_ledger,
        "outcome_ledger_path": args.outcome_ledger,
        "cell_ledger_path": args.cell_ledger,
        "cost_capture_json_path": args.cost_capture_json,
        "cost_capture_npz_path": args.cost_capture_npz,
        "fee_attestation_path": args.fee_attestation,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare or issue ledger-authenticated MTVCLC evidence offline."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    def public_inputs(target: argparse.ArgumentParser) -> None:
        target.add_argument("--preregistration", required=True)
        target.add_argument("--handoff", required=True)
        target.add_argument("--report", required=True)
        target.add_argument("--reservation-ledger", required=True)
        target.add_argument("--outcome-ledger", required=True)
        target.add_argument("--cell-ledger", required=True)
        target.add_argument("--cost-capture-json", required=True)
        target.add_argument("--cost-capture-npz", required=True)
        target.add_argument("--fee-attestation", required=True)

    prepare = commands.add_parser("prepare")
    public_inputs(prepare)
    prepare.add_argument("--generation-id", required=True)
    prepare.add_argument("--validity-secs", type=float, default=86_400.0)
    prepare.add_argument("--output", required=True)
    issue = commands.add_parser("issue")
    public_inputs(issue)
    issue.add_argument("--request", required=True)
    issue.add_argument("--signing-key-file", required=True)
    issue.add_argument("--verification-key-file", required=True)
    issue.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            output, request = prepare_issuance_request(
                public_input_paths=_public_args(args),
                generation_id=args.generation_id,
                validity_secs=args.validity_secs,
                output_path=args.output,
                now_epoch=time.time(),
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "output": str(output),
                        "request_body_sha256": request[
                            _core.ISSUANCE_REQUEST_SHA256_FIELD
                        ],
                        "authority_granted": False,
                    },
                    sort_keys=True,
                )
            )
            return 0
        output, bundle = issue_validation_bundle(
            request_path=args.request,
            public_input_paths=_public_args(args),
            signing_key_path=args.signing_key_file,
            verification_key_path=args.verification_key_file,
            output_path=args.output,
            now_epoch=time.time(),
        )
        print(
            json.dumps(
                {
                    "status": "issued",
                    "output": str(output),
                    "bundle_body_sha256": bundle[public.BUNDLE_SHA256_FIELD],
                    "immediate_market_buy_sell_only": True,
                    "pending_trades_forbidden": True,
                    "runtime_authority_granted": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except MTVCLCReleaseRefusal as exc:
        print(f"gap-v3 validation release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
