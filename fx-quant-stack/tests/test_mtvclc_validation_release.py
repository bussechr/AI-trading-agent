from __future__ import annotations

import base64
import copy
from datetime import UTC, datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.runtime import mtvclc_validation_evidence as verifier  # noqa: E402


def _load(name: str, path: Path):  # type: ignore[no-untyped-def]
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


release = _load(
    "mtvclc_validation_release_test_target",
    REPO_ROOT / "tools" / "mtvclc_validation_release.py",
)
sealer = _load(
    "mtvclc_validation_release_sealer",
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient.py",
)
base_helpers = _load(
    "mtvclc_validation_release_base_helpers",
    Path(__file__).with_name("test_seal_mt4_tick_volume_preregistration.py"),
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identity(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"filename": path.name, "sha256": _sha(raw), "size_bytes": len(raw)}


def _write_canonical(path: Path, payload: Mapping[str, Any]) -> Path:
    path.write_bytes(verifier.canonical_json_bytes(payload) + b"\n")
    return path


def _write_preregistration(
    root: Path,
) -> tuple[Path, dict[str, Any], Path, Path, Path]:
    input_root = root / "cost-inputs"
    input_root.mkdir()
    capture, npz, fee = base_helpers._inputs(input_root)
    payload = sealer.build_preregistration(
        cost_capture_json=capture,
        cost_capture_npz=npz,
        fee_attestation=fee,
        sealed_at=base_helpers.SEALED_AT,
    )
    path = root / (
        f"mtvclc_v1_preregistration_{payload['preregistration_body_sha256']}.json"
    )
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path, payload, capture, npz, fee


def _write_handoff(
    root: Path,
    *,
    prereg_path: Path,
    prereg: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    symbols = tuple(release.IG_MT4_SCALP_SYMBOLS)
    inventory = {
        "preregistration_body_sha256": prereg["preregistration_body_sha256"],
        "preregistration_artifact_sha256": _sha(prereg_path.read_bytes()),
        "prospective_t0_utc_inclusive": prereg["prospective_window"][
            "t0_utc_inclusive"
        ],
        "prospective_end_utc_exclusive": prereg["prospective_window"][
            "end_utc_exclusive"
        ],
        "manifest_sha256": "1" * 64,
        "manifest_head_sha256": "2" * 64,
        "manifest_entries": 10,
        "market_source_id": "3" * 64,
        "segment_count": 1,
        "bar_rows": 100_000,
        "quote_rows": 100_000,
        "bar_rows_by_symbol": {symbol: 5_000 for symbol in symbols},
        "quote_rows_by_symbol": {symbol: 5_000 for symbol in symbols},
        "first_bar_epoch_by_symbol": {symbol: 1 for symbol in symbols},
        "first_quote_epoch_by_symbol": {symbol: 1 for symbol in symbols},
        "last_quote_epoch_by_symbol": {symbol: 2 for symbol in symbols},
        "last_bar_epoch_by_symbol": {symbol: 2 for symbol in symbols},
        "maximum_transport_gap_seconds_by_symbol": {
            symbol: 2.0 for symbol in symbols
        },
        "transport_gap_count_over_five_seconds_by_symbol": {
            symbol: 0 for symbol in symbols
        },
        "referenced_chunk_files": 10,
        "orphan_chunk_files": 0,
        "guard_identity_sha256": "4" * 64,
    }
    body = {
        "schema_version": release.handoff.HANDOFF_SCHEMA,
        "strategy_id": verifier.MTVCLC_STRATEGY_ID,
        "strategy_version": verifier.MTVCLC_STRATEGY_VERSION,
        "config_id": verifier.MTVCLC_CONFIG_ID,
        "venue_id": release.IG_MT4_VENUE_ID,
        "scope_version": release.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(symbols),
        "source_contract_id": verifier.MTVCLC_SOURCE_CONTRACT_ID,
        "activity_metric_id": verifier.MTVCLC_ACTIVITY_METRIC_ID,
        "capture_inventory": inventory,
        "capture_inventory_sha256": verifier.canonical_sha256(inventory),
        "window_closed": True,
        "manifest_and_chunks_verified": True,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "research_only": True,
        "authority": dict(release._FALSE_RESEARCH_AUTHORITY),
    }
    body["handoff_body_sha256"] = verifier.canonical_sha256(body)
    path = root / f"mtvclc_capture_handoff_{body['handoff_body_sha256']}.json"
    return _write_canonical(path, body), body


def _ledger(
    root: Path,
    *,
    kind: str,
    rows: list[dict[str, Any]],
    evidence_binding_sha256: str,
    handoff_body_sha256: str,
    capture_inventory_sha256: str,
) -> tuple[Path, dict[str, Any]]:
    lines = []
    for sequence, row in enumerate(rows, start=1):
        wrapper = {
            "schema_version": release.LEDGER_ROW_SCHEMA,
            "ledger_kind": kind,
            "sequence": sequence,
            "evidence_binding_sha256": evidence_binding_sha256,
            "handoff_body_sha256": handoff_body_sha256,
            "capture_inventory_sha256": capture_inventory_sha256,
            "record": row,
            "research_only": True,
            "authority": dict(release._FALSE_RESEARCH_AUTHORITY),
        }
        lines.append(verifier.canonical_json_bytes(wrapper) + b"\n")
    raw = b"".join(lines)
    digest = _sha(raw)
    path = root / f"mtvclc_{kind}_ledger_{digest}.jsonl"
    path.write_bytes(raw)
    return path, {
        "filename": path.name,
        "sha256": digest,
        "size_bytes": len(raw),
        "rows": len(rows),
    }


def _passing_rows(
    prereg: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    reservations: list[dict[str, Any]] = []
    outcomes: list[dict[str, Any]] = []
    cells: list[dict[str, Any]] = []
    start = datetime(2026, 8, 4, tzinfo=UTC)
    for symbol in release.IG_MT4_SCALP_SYMBOLS:
        cost = prereg["cost_policy"]["symbols"][symbol]
        break_even = cost["conversion_adjusted_break_even_win_probability"]
        for side in ("BUY", "SELL"):
            selected: list[dict[str, Any]] = []
            for index in range(60):
                day = (start + timedelta(days=index)).date().isoformat()
                reservations.append(
                    {
                        "config_id": verifier.MTVCLC_CONFIG_ID,
                        "symbol": symbol,
                        "side": side,
                        "entry_day": day,
                    }
                )
                outcome = {
                    "config_id": verifier.MTVCLC_CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "signal_epoch": 1_800_000_000 + index * 86_400,
                    "entry_day": day,
                    "entry_epoch": 1_800_000_060 + index * 86_400,
                    "exit_epoch": 1_800_000_120 + index * 86_400,
                    "entry_price": 1.1,
                    "exit_price": 1.2,
                    "exit_reason": "TAKE_PROFIT",
                    "full_target_hit_first": True,
                    "gross_quote_bps": 10.0,
                    "recorded_cost_bps": cost[
                        "pre_conversion_geometry_cost_bps"
                    ],
                    "currency_conversion_debit_bps": 0.0,
                    "net_bps": 1.0,
                }
                outcomes.append(outcome)
                selected.append(outcome)
            lower = verifier.wilson_one_sided_lower(wins=60, trials=60)
            assert lower > break_even
            cells.append(
                {
                    "config_id": verifier.MTVCLC_CONFIG_ID,
                    "symbol": symbol,
                    "side": side,
                    "source_ready": True,
                    "reservations": 60,
                    "wins": 60,
                    "independent_days": 60,
                    "full_target_rate": 1.0,
                    "win_probability_wilson_lower": lower,
                    "base_break_even_probability": break_even,
                    "mean_net_bps": 1.0,
                    "passes_fixed_cell_screen": True,
                }
            )
    return reservations, outcomes, cells


def _write_report_bundle(
    root: Path,
    *,
    prereg_path: Path,
    prereg: Mapping[str, Any],
    handoff_path: Path,
    handoff_payload: Mapping[str, Any],
) -> tuple[dict[str, Path], dict[str, Any]]:
    inventory = handoff_payload["capture_inventory"]
    evaluator_identity = _identity(release.EVALUATOR_PATH)
    handoff_verifier_identity = _identity(
        REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff.py"
    )
    binding = {
        "preregistration_body_sha256": prereg["preregistration_body_sha256"],
        "preregistration_artifact_sha256": _sha(prereg_path.read_bytes()),
        "handoff_body_sha256": handoff_payload["handoff_body_sha256"],
        "handoff_artifact_sha256": _sha(handoff_path.read_bytes()),
        "capture_inventory_sha256": handoff_payload["capture_inventory_sha256"],
        "manifest_sha256": inventory["manifest_sha256"],
        "manifest_head_sha256": inventory["manifest_head_sha256"],
        "market_source_id": inventory["market_source_id"],
        "screen_source_filename": prereg["source_identities"]["screen_source"][
            "filename"
        ],
        "frozen_screen_sha256": prereg["source_identities"]["screen_source"][
            "sha256"
        ],
        "frozen_screen_support_sha256": prereg["source_identities"][
            "screen_support_source"
        ]["sha256"],
        "handoff_verifier_sha256": handoff_verifier_identity["sha256"],
        "evaluator_source_sha256": evaluator_identity["sha256"],
        "cost_source_sha256": prereg["source_identities"]["cost_capture"][
            "capture_json"
        ]["sha256"],
        "source_sha256_by_symbol": {
            symbol: hashlib.sha256(symbol.encode("ascii")).hexdigest()
            for symbol in release.IG_MT4_SCALP_SYMBOLS
        },
        "guard_identity_sha256": inventory["guard_identity_sha256"],
    }
    binding_sha = verifier.canonical_sha256(binding)
    reservations, outcomes, cells = _passing_rows(prereg)
    reservation_path, reservation_identity = _ledger(
        root,
        kind="reservation",
        rows=reservations,
        evidence_binding_sha256=binding_sha,
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    outcome_path, outcome_identity = _ledger(
        root,
        kind="outcome",
        rows=outcomes,
        evidence_binding_sha256=binding_sha,
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    cell_path, cell_identity = _ledger(
        root,
        kind="cell",
        rows=cells,
        evidence_binding_sha256=binding_sha,
        handoff_body_sha256=handoff_payload["handoff_body_sha256"],
        capture_inventory_sha256=handoff_payload["capture_inventory_sha256"],
    )
    total_trades = len(outcomes)
    report: dict[str, Any] = {
        "schema_version": release.POST_WINDOW_REPORT_SCHEMA,
        "evaluator_schema_version": release.POST_WINDOW_EVALUATOR_SCHEMA,
        "strategy_id": verifier.MTVCLC_STRATEGY_ID,
        "strategy_version": verifier.MTVCLC_STRATEGY_VERSION,
        "config_id": verifier.MTVCLC_CONFIG_ID,
        "symbol_scope": list(release.IG_MT4_SCALP_SYMBOLS),
        "prospective_t0_utc_inclusive": prereg["prospective_window"][
            "t0_utc_inclusive"
        ],
        "prospective_end_utc_exclusive": prereg["prospective_window"][
            "end_utc_exclusive"
        ],
        "window_closed_before_capture_access": True,
        "capture_snapshot_required_stopped_and_read_only": True,
        "capture_snapshot_active_hour_journal_absent": True,
        "capture_snapshot_data_writer_lock_required": True,
        "capture_snapshot_data_writer_lock_proven_free": True,
        "capture_snapshot_reverified_after_materialization": True,
        "capture_snapshot_fence_rechecked_before_publication": True,
        "bootstrap_context_bars_retained": 240,
        "pre_t0_signal_bars_evaluated": 0,
        "pre_t0_quotes_evaluated": 0,
        "frozen_screen_semantics_executed": True,
        "frozen_screen_hash_is_exact_executed_bytes": True,
        "handoff_verifier_hash_is_exact_executed_bytes": True,
        "screen_result_valid": True,
        "screen_source_scope_ready": True,
        "screen_source_errors": [],
        "screen_all_cells_pass_fixed_screen": True,
        "global_success_gates": {
            "all_44_cells_pass": True,
            "source_scope_ready": True,
            "total_trades": total_trades,
            "minimum_total_trades": 300,
            "total_independent_utc_days": 60,
            "minimum_total_independent_utc_days": 60,
            "global_trade_gate_pass": True,
            "global_day_gate_pass": True,
            "all_preregistered_success_gates_pass": True,
        },
        "preregistered_success_criteria_observed": True,
        "evidence_binding": binding,
        "evidence_binding_sha256": binding_sha,
        "ledgers": {
            "reservation": reservation_identity,
            "outcome": outcome_identity,
            "cell": cell_identity,
        },
        "capture_materialization": {
            "schema_version": "fxstack.scalp.mtvclc_binary_materialization.v1",
            "bounded_memory": True,
            "disk_backed_per_symbol": True,
            "full_universe_loaded_in_memory": False,
            "manifest_entries": 10,
            "projected_size_bytes": 100,
            "bar_rows_by_symbol": inventory["bar_rows_by_symbol"],
            "quote_rows_by_symbol": inventory["quote_rows_by_symbol"],
            "snapshot_files_verified_read_only": True,
            "snapshot_directories_verified_read_only": True,
            "scratch_retained": False,
        },
        "pbo_dsr_lineage_evaluated": False,
        "issuer_adapter_present": False,
        "evaluation_performed": True,
        "performance_statistics_computed": True,
        "research_only": True,
        "authority": dict(release._FALSE_RESEARCH_AUTHORITY),
        "success_claim_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "issuer_authorized": False,
        "signature_authorized": False,
        "broker_access_authorized": False,
        "order_authorized": False,
    }
    report["report_body_sha256"] = verifier.canonical_sha256(report)
    report_raw = verifier.canonical_json_bytes(report) + b"\n"
    report_path = root / f"mtvclc_post_window_report_{_sha(report_raw)}.json"
    report_path.write_bytes(report_raw)
    return {
        "report_path": report_path,
        "reservation_ledger_path": reservation_path,
        "outcome_ledger_path": outcome_path,
        "cell_ledger_path": cell_path,
    }, report


def _fixture(tmp_path: Path) -> tuple[dict[str, Path], float]:
    prereg_path, prereg, capture, npz, fee = _write_preregistration(tmp_path)
    handoff_path, handoff_payload = _write_handoff(
        tmp_path, prereg_path=prereg_path, prereg=prereg
    )
    report_paths, _report = _write_report_bundle(
        tmp_path,
        prereg_path=prereg_path,
        prereg=prereg,
        handoff_path=handoff_path,
        handoff_payload=handoff_payload,
    )
    paths = {
        "preregistration_path": prereg_path,
        "handoff_path": handoff_path,
        **report_paths,
        "cost_capture_json_path": capture,
        "cost_capture_npz_path": npz,
        "fee_attestation_path": fee,
    }
    end = datetime.fromisoformat(
        prereg["prospective_window"]["end_utc_exclusive"].replace("Z", "+00:00")
    ).timestamp()
    return paths, end + 60.0


def _signed_bundle_from_request(
    request: Mapping[str, Any], signing_key: Ed25519PrivateKey
) -> dict[str, Any]:
    certificate = copy.deepcopy(dict(request["certificate_claims"]))
    certificate["signing_key_id"] = verifier.ed25519_public_key_id(
        signing_key.public_key()
    )
    certificate["evidence_sha256"] = verifier.canonical_sha256(
        certificate["evidence"]
    )
    certificate[verifier.CERTIFICATE_SHA256_FIELD] = (
        verifier.certificate_body_sha256(certificate)
    )
    certificate[verifier.CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(
        signing_key.sign(verifier.canonical_json_bytes(certificate))
    ).decode("ascii")
    bundle = {
        "schema_version": verifier.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
        "certificate": certificate,
    }
    bundle[verifier.BUNDLE_SHA256_FIELD] = verifier.bundle_body_sha256(bundle)
    return bundle


def _expectation(request: Mapping[str, Any]) -> verifier.MTVCLCValidationExpectation:
    claims = request["certificate_claims"]
    return verifier.MTVCLCValidationExpectation(
        generation_id=claims["generation_id"],
        strategy_id=claims["strategy_id"],
        strategy_version=claims["strategy_version"],
        config_id=claims["config_id"],
        config_sha256=claims["config_sha256"],
        evaluator_source_sha256=claims["evaluator_source_sha256"],
    )


def test_prepare_revalidates_exact_mtvclc_evidence_and_stays_authority_free(
    tmp_path: Path,
) -> None:
    paths, now = _fixture(tmp_path)
    output, request = release.prepare_issuance_request(
        public_input_paths=paths,
        generation_id="mtvclc-generation-1",
        output_path=tmp_path / "request.json",
        now_epoch=now,
    )

    evidence = request["certificate_claims"]["evidence"]
    assert output.is_file()
    assert request["authority"] == verifier.NO_RUNTIME_AUTHORITY
    assert not any(request["authority"].values())
    assert evidence["account_mode"] == "demo"
    assert evidence["scope"]["symbol_scope"] == list(
        release.IG_MT4_SCALP_SYMBOLS
    )
    assert evidence["execution_contract"] == verifier.EXPECTED_EXECUTION_CONTRACT
    assert evidence["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_742,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_786,
    }
    assert evidence["wilson_allocation"]["alpha_allocation"] == (
        "one_sided_0.05_over_4786"
    )
    assert set(evidence["sealed_gates"]) == set(verifier.EXPECTED_SEALED_GATES)
    assert "pbo" not in evidence and "dsr" not in evidence
    assert len(evidence["cells"]) == 44
    assert set(evidence["costs"]["rows"]) == set(
        release.IG_MT4_SCALP_SYMBOLS
    )


def test_public_verifier_accepts_synthetic_signature_and_rejects_gate_invention(
    tmp_path: Path,
) -> None:
    paths, now = _fixture(tmp_path)
    _output, request = release.prepare_issuance_request(
        public_input_paths=paths,
        generation_id="mtvclc-generation-2",
        output_path=tmp_path / "request.json",
        now_epoch=now,
    )
    signing_key = Ed25519PrivateKey.generate()
    bundle = _signed_bundle_from_request(request, signing_key)

    accepted = verifier.verify_mtvclc_validation_evidence(
        bundle=bundle,
        public_key=signing_key.public_key(),
        expectation=_expectation(request),
        now_epoch=now,
    )

    assert accepted.valid is True
    assert accepted.account_mode == "demo"
    assert accepted.symbol_scope == release.IG_MT4_SCALP_SYMBOLS

    invented_request = copy.deepcopy(request)
    invented_request["certificate_claims"]["evidence"]["sealed_gates"][
        "pbo"
    ] = 0.0
    invented_bundle = _signed_bundle_from_request(invented_request, signing_key)
    refused = verifier.verify_mtvclc_validation_evidence(
        bundle=invented_bundle,
        public_key=signing_key.public_key(),
        expectation=_expectation(request),
        now_epoch=now,
    )
    assert refused.valid is False
    assert refused.reason == "mtvclc_evidence_sealed_gates_invalid"


def test_issue_refuses_changed_public_input_before_private_key_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, now = _fixture(tmp_path)
    request_path, _request = release.prepare_issuance_request(
        public_input_paths=paths,
        generation_id="mtvclc-generation-3",
        output_path=tmp_path / "request.json",
        now_epoch=now,
    )
    report_path = paths["report_path"]
    report_path.write_bytes(report_path.read_bytes() + b" ")
    private_key_accessed = False

    def forbidden_key_load(_path: str | Path) -> Any:
        nonlocal private_key_accessed
        private_key_accessed = True
        raise AssertionError("private key must remain unopened")

    monkeypatch.setattr(release, "_load_private_key", forbidden_key_load)
    with pytest.raises(release.MTVCLCReleaseRefusal, match="report"):
        release.issue_validation_bundle(
            request_path=request_path,
            public_input_paths=paths,
            signing_key_path=tmp_path / "must-not-open.pem",
            verification_key_path=tmp_path / "also-not-reached.pub",
            output_path=tmp_path / "bundle.json",
            now_epoch=now,
        )
    assert private_key_accessed is False
    assert not (tmp_path / "bundle.json").exists()


def test_prepare_rejects_rehashed_44_cell_wilson_claim(
    tmp_path: Path,
) -> None:
    paths, now = _fixture(tmp_path)
    prereg_path = paths["preregistration_path"]
    prereg = json.loads(prereg_path.read_text(encoding="utf-8"))
    prereg["fixed_success_gates"]["cell_win_probability_interval"] = (
        "wilson_one_sided_bonferroni_95pct_44_cells_v1"
    )
    body = dict(prereg)
    body.pop("preregistration_body_sha256")
    prereg["preregistration_body_sha256"] = verifier.canonical_sha256(body)
    prereg_path.write_text(
        json.dumps(prereg, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    with pytest.raises(release.MTVCLCReleaseRefusal, match="preregistration"):
        release.prepare_issuance_request(
            public_input_paths=paths,
            generation_id="mtvclc-generation-4",
            output_path=tmp_path / "request.json",
            now_epoch=now,
        )


def test_release_tool_has_no_live_or_activation_surface() -> None:
    source = (REPO_ROOT / "tools" / "mtvclc_validation_release.py").read_text(
        encoding="utf-8"
    )
    lowered = source.lower()
    assert "requests." not in lowered
    assert "urllib.request" not in lowered
    assert "socket." not in lowered
    assert "runtime_authorized\": true" not in lowered
    assert "order_authorized\": true" not in lowered
    assert "ed25519privatekey.generate" not in lowered
    issue_source = source[source.index("def issue_validation_bundle(") :]
    assert issue_source.index("_revalidate_request(") < issue_source.index(
        "_load_private_key(signing_key_path)"
    )
