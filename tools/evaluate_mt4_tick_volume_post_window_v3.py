"""Versioned isolated evaluator for the corrected MTVCLC gap-v3 capture.

This entrypoint reuses the established bounded-memory capture projection and
outcome engine, but replaces every selection boundary: the v3 handoff verifier,
the ledger-authenticated v3 screen, v2 ledger rows, and v3 report filenames are
all exact-source bound.  No live, issuer, signing, activation, runtime, broker,
or trade surface is introduced.
"""

from __future__ import annotations

# AGENT: ROLE: isolated post-window evaluator for gap-v3 evidence.
# AGENT: HANDSHAKE: v3 handoff -> exact v3 screen -> authenticated ledgers/report.
# AGENT: ISOLATION: local immutable files only; no live or authority surface.
import argparse
import json
import os
import stat
import sys
from collections.abc import Sequence
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
LEGACY_EVALUATOR_PATH = REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window.py"
SCREEN_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation_replacement_v3.py"
)
REPORT_SCHEMA = "fxstack.scalp.mtvclc_post_window_report.v3"
EVALUATOR_SCHEMA = "fxstack.scalp.mtvclc_post_window_evaluator.v2"
LEDGER_ROW_SCHEMA = "fxstack.scalp.mtvclc_research_ledger_row.v2"


def _bootstrap_exact_module(path: Path, *, module_name: str) -> ModuleType:
    """Load the exact source bytes for the first trusted downstream boundary."""

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
    identity = lambda value: (  # noqa: E731 - compact immutable stat projection
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
    module_name="_fxstack_mtvclc_gap_v3_evaluator_handoff",
)
EVALUATOR_SOURCE_IMAGE = handoff._read_exact_source(
    TOOL_PATH,
    reason="evaluator_source_import_invalid",
)
LEGACY_EVALUATOR_SOURCE_IMAGE = handoff._read_exact_source(
    LEGACY_EVALUATOR_PATH,
    reason="legacy_evaluator_source_import_invalid",
)


def _load_private_legacy_module() -> Any:
    name = "_fxstack_mtvclc_gap_v3_evaluator_core"
    return handoff._execute_exact_source(
        LEGACY_EVALUATOR_SOURCE_IMAGE,
        module_name=name,
    )


_core = _load_private_legacy_module()
EvaluationRefusal = _core.EvaluationRefusal
LEGACY_HANDOFF_SOURCE_IMAGE = _core.HANDOFF_SOURCE_IMAGE
BASE_SCREEN_SOURCE_IMAGE = _core.BASE_SCREEN_SOURCE_IMAGE

# Bind all private core globals before exposing an evaluation function.
_core.handoff = handoff
_core.HANDOFF_PATH = HANDOFF_PATH
_core.HANDOFF_SOURCE_IMAGE = _core.ExecutedSourceImage(
    path=handoff.HANDOFF_SOURCE_IMAGE.path,
    raw=handoff.HANDOFF_SOURCE_IMAGE.raw,
    sha256=handoff.HANDOFF_SOURCE_IMAGE.sha256,
    size_bytes=handoff.HANDOFF_SOURCE_IMAGE.size_bytes,
)
_core.SCREEN_PATH = _core.SCREEN_PATH
_core.REPLACEMENT_SCREEN_PATH = SCREEN_PATH
_core.REPLACEMENT_SCREEN_FILENAME = SCREEN_PATH.name
_core.REPLACEMENT_SCREEN_MODULE_NAME = "fxstack_isolated_mtvclc_gap_v3_screen"
_core.EVALUATOR_PATH = TOOL_PATH
_core.EVALUATOR_SOURCE_IMAGE = _core.ExecutedSourceImage(
    path=EVALUATOR_SOURCE_IMAGE.path,
    raw=EVALUATOR_SOURCE_IMAGE.raw,
    sha256=EVALUATOR_SOURCE_IMAGE.sha256,
    size_bytes=EVALUATOR_SOURCE_IMAGE.size_bytes,
)
_core.REPORT_SCHEMA = REPORT_SCHEMA
_core.EVALUATOR_SCHEMA = EVALUATOR_SCHEMA
_core.LEDGER_ROW_SCHEMA = LEDGER_ROW_SCHEMA
_core.HANDOFF_FIELDS = handoff.HANDOFF_FIELDS
_core.BASE_INVENTORY_FIELDS = handoff.INVENTORY_FIELDS
_core.REPLACEMENT_INVENTORY_FIELDS = handoff.INVENTORY_FIELDS
_core.FALSE_AUTHORITY = dict(handoff.FALSE_AUTHORITY)


def executed_source_identities() -> dict[str, dict[str, Any]]:
    """Return exact source identities executed by the evaluator boundary."""

    return {
        **handoff.executed_source_identities(),
        "evaluator_source": EVALUATOR_SOURCE_IMAGE.identity(),
        "legacy_evaluator_support_source": LEGACY_EVALUATOR_SOURCE_IMAGE.identity(),
        "legacy_handoff_support_source": LEGACY_HANDOFF_SOURCE_IMAGE.identity(),
        "screen_support_source": BASE_SCREEN_SOURCE_IMAGE.identity(),
    }


def _assert_executed_sources_unchanged() -> None:
    paths = handoff.sealer._source_paths()
    for label, identity in executed_source_identities().items():
        path = paths.get(label)
        if path is None:
            raise EvaluationRefusal(f"executed_source_path_missing:{label}")
        try:
            current = handoff._read_exact_source(
                path,
                reason=f"executed_source_changed:{label}",
            )
        except RuntimeError as exc:
            raise EvaluationRefusal(f"executed_source_changed:{label}") from exc
        if current.identity() != identity:
            raise EvaluationRefusal(f"executed_source_changed:{label}")


_original_publish_content_addressed = _core._publish_content_addressed
_original_select_executed_screen = _core._select_executed_screen


def _select_executed_screen_v3(preregistration: Any) -> Any:
    identities = preregistration.get("source_identities")
    if not isinstance(identities, dict) or any(
        dict(identities.get(label) or {}) != identity
        for label, identity in executed_source_identities().items()
    ):
        raise EvaluationRefusal("executed_source_identity_mismatch")
    return _original_select_executed_screen(preregistration)


def _publish_content_addressed_v3(**kwargs: Any) -> Any:
    _assert_executed_sources_unchanged()
    prefix = str(kwargs.get("prefix") or "")
    mapped = {
        "mtvclc_reservation_ledger": "mtvclc_reservation_ledger_v2",
        "mtvclc_outcome_ledger": "mtvclc_outcome_ledger_v2",
        "mtvclc_cell_ledger": "mtvclc_cell_ledger_v2",
        "mtvclc_post_window_report": "mtvclc_post_window_report_v3",
    }.get(prefix, prefix)
    kwargs["prefix"] = mapped
    return _original_publish_content_addressed(**kwargs)


def _load_handoff_v3(
    path: str | Path,
    *,
    binding: Any,
    replacement_profile: bool = True,
) -> tuple[dict[str, Any], str]:
    del replacement_profile
    try:
        payload, artifact_sha256 = handoff.load_handoff_artifact(
            path,
            binding=binding,
        )
    except handoff.HandoffRefusal as exc:
        raise EvaluationRefusal("capture_handoff_invalid") from exc
    return payload, artifact_sha256


def _screen_result_from_materialization_v3(
    *,
    sealed: Any,
    materialized: Any,
) -> dict[str, Any]:
    selected_screen = sealed.screen_module
    source_ready = _core.verified_handoff_proves_source_ready(
        sealed=sealed,
        materialized=materialized,
    )
    reservations: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    for symbol in handoff.SYMBOLS:
        symbol_reservations, symbol_outcomes = _core.evaluate_materialized_symbol(
            symbol=symbol,
            bar_path=materialized.bar_paths[symbol],
            quote_path=materialized.quote_paths[symbol],
            cost=sealed.costs_by_symbol[symbol],
            t0_epoch=sealed.binding.t0_epoch,
            end_epoch_exclusive=sealed.binding.end_epoch_exclusive,
            screen_module=selected_screen,
        )
        reservations.extend(symbol_reservations)
        outcomes.extend(symbol_outcomes)
    ready_by_symbol = {symbol: bool(source_ready) for symbol in handoff.SYMBOLS}
    cost_rows = {
        symbol: asdict(sealed.costs_by_symbol[symbol]) for symbol in handoff.SYMBOLS
    }
    cells = selected_screen.recompute_cells_from_ledgers(
        reservation_ledger=reservations,
        outcome_ledger=outcomes,
        costs=cost_rows,
        source_ready_by_symbol=ready_by_symbol,
    )
    if cells is None:
        raise EvaluationRefusal("frozen_screen_ledgers_invalid")
    result = {
        "schema_version": selected_screen.SCREEN_RESULT_SCHEMA,
        "strategy_id": selected_screen.STRATEGY_ID,
        "strategy_version": selected_screen.STRATEGY_VERSION,
        "config_ids": [selected_screen.CONFIG_ID],
        "symbol_scope": list(selected_screen.MTVCLC_SYMBOLS),
        "source_contract_id": selected_screen.SOURCE_CONTRACT_ID,
        "activity_metric_id": selected_screen.ACTIVITY_METRIC_ID,
        "attempt_accounting": dict(sealed.preregistration["attempt_accounting"]),
        "source_scope_ready": bool(source_ready),
        "source_ready_by_symbol": ready_by_symbol,
        "source_sha256_by_symbol": dict(materialized.source_sha256_by_symbol),
        "source_errors": [],
        "costs": cost_rows,
        "cells": cells,
        "reservation_ledger": reservations,
        "outcome_ledger": outcomes,
        "all_cells_pass_fixed_screen": all(
            cell["passes_fixed_cell_screen"] for cell in cells
        ),
        "attempt_manifest": dict(
            sealed.preregistration["strategy"]["attempt_manifest"]
        ),
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "order_authorized": False,
    }
    if not selected_screen.validate_result_bundle(result):
        raise EvaluationRefusal("frozen_screen_result_invalid")
    return result


_core._publish_content_addressed = _publish_content_addressed_v3
_core._load_handoff = _load_handoff_v3
_core._select_executed_screen = _select_executed_screen_v3
_core._screen_result_from_materialization = _screen_result_from_materialization_v3

canonical_json_bytes = _core.canonical_json_bytes
canonical_sha256 = _core.canonical_sha256
enforce_global_gates = _core.enforce_global_gates
evaluate_materialized_symbol = _core.evaluate_materialized_symbol
publish_research_artifacts = _core.publish_research_artifacts


def evaluate_post_window(
    *,
    preregistration_path: str | Path,
    handoff_path: str | Path,
    capture_root: str | Path,
    staging_root: str | Path,
    output_root: str | Path,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Run one complete offline evaluation with all v3 selectors pinned."""

    return _core.evaluate_post_window(
        preregistration_path=preregistration_path,
        handoff_path=handoff_path,
        capture_root=capture_root,
        staging_root=staging_root,
        output_root=output_root,
        now_epoch=now_epoch,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Evaluate a completed gap-v3 MTVCLC capture offline."
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--handoff", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--staging-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report_path, report = evaluate_post_window(
            preregistration_path=args.preregistration,
            handoff_path=args.handoff,
            capture_root=args.capture_root,
            staging_root=args.staging_root,
            output_root=args.output_root,
        )
    except EvaluationRefusal as exc:
        print(f"gap-v3 evaluation refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "report": str(report_path),
                "report_body_sha256": report["report_body_sha256"],
                "research_only": True,
                "authority_granted": False,
                "immediate_market_buy_sell_only": True,
                "pending_trades_forbidden": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
