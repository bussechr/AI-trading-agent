from __future__ import annotations

import base64
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from fxstack.runtime.release_contract import (
    active_manifest_identity,
    artifact_set_sha256,
    canonical_model_identity_sha256,
    file_sha256,
    is_sha256,
    measure_package_tree,
    read_json_object,
    resolve_contained_evidence_ref,
    validate_phase5_gate_bundle,
)
from fxstack.runtime.release_trust import (
    current_runtime_principal_id,
    load_release_trust_policy,
    observe_physical_capabilities,
    physical_boundary_errors,
)


RELEASE_AUTHORITY_REQUEST_SCHEMA = "fxstack_live_release_authority_request_v1"
RELEASE_AUTHORITY_STATE_SCHEMA = "fxstack_live_release_authority_state_v1"
RELEASE_AUTHORITY_ACK_SCHEMA = "fxstack_live_release_authority_ack_v1"
RELEASE_SIGNING_REQUEST_SCHEMA = "fxstack_release_signing_request_v1"

# Explicit, reviewed projection of settings that can alter a live decision,
# its size, its protection, or whether it reaches the broker.  Endpoint,
# credential, filesystem, training, and observability settings are omitted by
# exact choice; names are never filtered by substrings ("portfolio" must not
# disappear merely because it contains "port").
_EXECUTION_SEMANTIC_FIELDS = (
    "data_provider",
    "history_provider",
    "market_data_provider",
    "execution_provider",
    "provider_shadow_only",
    "provider_symbol_allowlist",
    "crypto_exchange_id",
    "bridge_stale_heartbeat_secs",
    "bridge_stale_tick_secs",
    "command_ttl_secs",
    "pairs",
    "start_profile",
    "live_armed",
    "live_expected_account_mode",
    "allow_sqlite",
    "require_active_models",
    "intraday_timeframe",
    "swing_timeframe",
    "regime_timeframe",
    "max_pair_positions",
    "max_total_positions",
    "default_order_lots",
    "equity_lots_per_usd",
    "min_order_lots",
    "order_lot_step",
    "max_order_lots",
    "min_swing_prob",
    "min_entry_prob",
    "min_trade_prob",
    "max_allowed_spread_bps",
    "min_expected_edge_bps",
    "min_expected_edge_rescue_margin_bps",
    "policy_version",
    "frame_profile",
    "swing_primary_timeframe",
    "enable_lifecycle_actions",
    "enable_adjust_actions",
    "hard_time_stop_secs",
    "adjust_stop_buffer_pips",
    "entry_stop_atr_multiple",
    "entry_take_profit_atr_multiple",
    "managed_runner_tp_r_multiple",
    "entry_min_stop_pips",
    "partial_close_fraction",
    "partial_close_cooldown_secs",
    "max_partial_closes_per_position",
    "lifecycle_model_action_min_prob",
    "reversal_failure_min_prob",
    "reversal_opportunity_min_prob",
    "strict_activation",
    "require_lifecycle_artifacts",
    "require_hierarchical_intraday_contract",
    "allow_heuristic_meta_labels",
    "strict_command_validation",
    "runtime_allow_create_all",
    "runtime_state_prune_stale_keys",
    "require_cuda",
    "tier1_pairs",
    "tier2_pairs",
    "model_load_timeout_secs",
    "deep_model_stale_hours",
    "live_spread_reject_rate_trigger",
    "swing_model_policy",
    "intraday_model_policy",
    "intraday_tcn_fallback_live_allowed",
    "xgb_device",
    "xgb_tree_method",
    "xgb_allow_cpu_fallback",
    "uncertainty_threshold",
    "use_uncertainty_gate",
    "max_entry_uncertainty",
    "adaptive_playbook_threshold_slack",
    "blocked_entry_sessions",
    "use_portfolio_ranking",
    "strategy_engine_mode",
    "rl_supervised_fallback_required",
    "portfolio_corr_mode",
    "portfolio_realized_corr_window_bars",
    "portfolio_realized_corr_min_obs",
    "portfolio_realized_corr_max_age_secs",
    "enable_pair_quality_prior",
    "max_new_entries_per_cycle",
    "adaptive_history_bars",
    "adaptive_playbooks",
    "adaptive_execution_enabled",
    "belief_enabled",
    "belief_runtime_required",
    "belief_influence_mode",
    "belief_short_horizon_bars",
    "belief_trade_horizon_bars",
    "belief_structural_horizon_bars",
    "campaign_manager_enabled",
    "campaign_abandon_cooldown_bars",
    "campaign_press_protected_bars",
    "campaign_reattack_cooldown_scale",
    "structure_timing_enabled",
    "structure_timing_rescue_min_score",
    "structure_timing_entry_rescue_margin",
    "structure_timing_max_chase_risk",
    "entry_hysteresis_margin_bps",
    "reversal_hysteresis_margin_bps",
    "risk_max_drawdown_pct",
    "risk_max_gross_exposure",
    "risk_max_net_exposure",
    "capital_band_mode",
    "capital_entries_only",
    "capital_governance_enabled",
    "capital_max_drawdown_micro_live_pct",
    "capital_max_drawdown_low_risk_pct",
    "capital_max_drawdown_full_risk_pct",
    "capital_max_tail_loss_pct",
    "capital_max_latency_breach_count",
    "capital_max_stale_feature_count",
    "capital_max_calibration_drift",
    "capital_max_operational_fault_count",
    "capital_max_concentration_share",
    "capital_max_realized_corr_share",
    "capital_rollout_budget_scale_micro_live",
    "capital_rollout_budget_scale_low_risk",
    "capital_rollout_budget_scale_full_risk",
    "feast_enabled",
    "feast_online_latency_budget_ms",
    "feast_online_stale_secs",
    "feature_push_enabled",
    "feature_push_batch_size",
    "feature_push_max_retries",
    "feature_push_claim_timeout_secs",
    "feature_push_backlog_warn",
    "feature_parity_tolerance",
    "model_bundle_version",
    "agent_mode",
    "agent_runtime",
    "agent_durability",
    "agent_decision_timeout_ms",
    "agent_max_node_ms",
    "agent_max_parallel_proposals",
    "agent_live_pair_allowlist",
    "agent_live_sleeve_allowlist",
    "agent_live_intent_allowlist",
    "agent_allow_remote_llm",
    "agent_allow_external_tools",
    "agent_require_human_approval",
    "phase5_observation_window_minutes",
    "phase5_canary_budget_scale",
    "phase5_canary_latency_budget_ms",
    "phase5_canary_stale_feature_limit",
    "phase5_canary_drawdown_limit_pct",
    "phase5_canary_calibration_drift_limit",
    "phase5_auto_rollback",
    "phase6b_canary_p95_overhead_ms",
    "phase6b_canary_p99_overhead_ms",
    "phase6b_canary_ack_success_floor",
    "phase6b_canary_orphan_command_limit",
    "phase6b_canary_entry_ratio_floor",
    "phase6b_canary_slot_utilisation_floor",
    "phase6b_canary_drawdown_deterioration_pct",
    "phase6b_canary_ramp_steps_pct",
    "phase6b_canary_alert_window_minutes",
)

_NON_EXECUTION_PUBLIC_FIELDS = frozenset(
    {
        # Secrets, endpoints, and machine-local locations are separately bound
        # by account, manifest, evidence, and package attestations.
        "database_url",
        "mt4_bridge_url",
        "dukascopy_source_root",
        "feast_repo_root",
        "mlflow_cache_root",
        "mlflow_registry_uri",
        "mlflow_tracking_uri",
        "model_activation_manifest",
        "model_manifest_path",
        "phase5_release_root",
        "registry_root",
        "rl_artifact_root",
        "rl_stress_root",
        "rl_transition_dataset_root",
        "sequence_dataset_cache_root",
        # Offline training/research/promotion configuration cannot execute in
        # the production distribution and is bound through signed artifacts.
        "cv_embargo_pct",
        "cv_splits",
        "deep_batch_size",
        "deep_retrain_max_age_hours",
        "deep_retrain_min_new_rows",
        "deep_train_epochs",
        "drift_trigger_ece",
        "drift_trigger_throughput_drop",
        "force_weekly_retrain_day",
        "intraday_retrain_min_new_rows",
        "lifecycle_retrain_min_new_events",
        "meta_retrain_min_new_rows",
        "min_segment_samples",
        "patchtst_d_model",
        "patchtst_dropout",
        "patchtst_num_heads",
        "patchtst_num_layers",
        "patchtst_patch_length",
        "patchtst_stride",
        "promotion_max_calibration_error",
        "promotion_min_cv_score",
        "promotion_min_delta",
        "promotion_min_wf_score",
        "promotion_policy",
        "rl_online_worker_count",
        "tcn_window_size",
        "throughput_floor",
        "transformer_window_size",
        "weekly_auto_activate",
        "weekly_full_retrain_time",
        "wf_step_months",
        "wf_test_months",
        "wf_train_months",
        # Non-live postures, diagnostics, and operator-plane settings.
        "agent_enable_otel",
        "agent_otel_exporter",
        "agent_paper_intent_allowlist",
        "agent_paper_pair_allowlist",
        "agent_paper_sleeve_allowlist",
        "agent_shadow_pair_allowlist",
        "agent_trace_retention_days",
        "dukascopy_file_pattern",
        "feature_push_worker_id",
        "mcp_enabled",
        "mcp_transport",
        "mlflow_enabled",
        "openclaw_enabled",
        "openclaw_sandbox_required",
        "openclaw_scopes",
        "run_fast_gate",
        "run_shadow_24h",
        "runtime_startup_progress_stale_secs",
    }
)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def release_request_sha256(request: dict[str, Any]) -> str:
    material = dict(request or {})
    material.pop("request_sha256", None)
    return canonical_sha256(material)


def unsigned_release_request_sha256(request: dict[str, Any]) -> str:
    """Hash only portable claims; local import paths and signatures are excluded."""

    material = dict(request or {})
    material.pop("external_witness", None)
    material.pop("request_sha256", None)
    material.pop("unsigned_request_sha256", None)
    material.pop("local_bindings", None)
    return canonical_sha256(material)


def release_witness_claims(request: dict[str, Any]) -> dict[str, Any]:
    payload = dict(request or {})
    execution = dict(payload.get("authorized_execution") or {})
    return {
        "unsigned_request_sha256": unsigned_release_request_sha256(payload),
        "generation_id": str(payload.get("generation_id") or ""),
        "pair": str(payload.get("pair") or "").strip().upper(),
        "bundle_run_id": str(payload.get("bundle_run_id") or ""),
        "model_set_id": str(payload.get("model_set_id") or ""),
        "model_identity_sha256": str(payload.get("model_identity_sha256") or ""),
        "artifact_set_sha256": str(payload.get("artifact_set_sha256") or ""),
        "manifest_file_sha256": str(payload.get("manifest_file_sha256") or ""),
        "phase5_bundle_sha256": str(payload.get("phase5_bundle_sha256") or ""),
        "release_validation_bundle_sha256": str(
            payload.get("release_validation_bundle_sha256") or ""
        ),
        "evidence_merkle_sha256": str(payload.get("evidence_merkle_sha256") or ""),
        "source_sha256": str(payload.get("source_sha256") or ""),
        "package_merkle_sha256": str(payload.get("package_merkle_sha256") or ""),
        "git_commit": str(payload.get("git_commit") or ""),
        "config_sha256": str(payload.get("config_sha256") or ""),
        "authorized_execution_sha256": canonical_sha256(execution),
    }


def build_release_signing_request(request: dict[str, Any]) -> dict[str, Any]:
    """Build the exact unsigned package an external witness must sign."""

    unsigned = dict(request or {})
    unsigned.pop("external_witness", None)
    unsigned.pop("request_sha256", None)
    unsigned["unsigned_request_sha256"] = unsigned_release_request_sha256(unsigned)
    claims = release_witness_claims(unsigned)
    return {
        "schema_version": RELEASE_SIGNING_REQUEST_SCHEMA,
        "request": unsigned,
        "unsigned_request_sha256": str(unsigned["unsigned_request_sha256"]),
        "witness_claims": claims,
        "witness_claims_sha256": canonical_sha256(claims),
    }


def import_external_release_witness(
    signing_request: dict[str, Any],
    witness: dict[str, Any],
    *,
    trust_policy: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Attach an external signature only after exact package/hash verification."""

    package = dict(signing_request or {})
    errors: list[str] = []
    if str(package.get("schema_version") or "") != RELEASE_SIGNING_REQUEST_SCHEMA:
        errors.append("release_signing_request_schema_invalid")
    request = dict(package.get("request") or {})
    unsigned_sha = unsigned_release_request_sha256(request)
    if (
        not is_sha256(unsigned_sha)
        or str(request.get("unsigned_request_sha256") or "") != unsigned_sha
        or str(package.get("unsigned_request_sha256") or "") != unsigned_sha
    ):
        errors.append("release_signing_request_hash_mismatch")
    claims = release_witness_claims(request)
    if dict(package.get("witness_claims") or {}) != claims:
        errors.append("release_signing_request_claims_mismatch")
    if str(package.get("witness_claims_sha256") or "") != canonical_sha256(claims):
        errors.append("release_signing_request_claims_hash_mismatch")
    errors.extend(
        external_witness_errors(
            dict(witness or {}),
            expected_claims=claims,
            trust_policy=trust_policy,
        )
    )
    final_request = {**request, "external_witness": dict(witness or {})}
    final_request["request_sha256"] = release_request_sha256(final_request)
    return final_request, list(dict.fromkeys(errors))


def runtime_config_sha256(settings: Any) -> str:
    public = (
        dict(settings.to_public_dict())
        if hasattr(settings, "to_public_dict")
        else {
            key: value
            for key, value in vars(settings).items()
            if not str(key).startswith("_")
        }
    )
    if hasattr(settings, "to_public_dict"):
        unclassified = sorted(
            set(public)
            - set(_EXECUTION_SEMANTIC_FIELDS)
            - set(_NON_EXECUTION_PUBLIC_FIELDS)
        )
        if unclassified:
            raise ValueError(
                "execution_config_projection_unclassified:"
                + ",".join(unclassified)
            )
    semantic = {
        field: public[field]
        for field in _EXECUTION_SEMANTIC_FIELDS
        if field in public
    }
    semantic["schema_version"] = "fxstack_execution_config_projection_v2"
    semantic["normalized_execution_provider"] = str(
        getattr(settings, "normalized_execution_provider", semantic.get("execution_provider", ""))
        or ""
    )
    semantic["normalized_data_provider"] = str(
        getattr(settings, "normalized_data_provider", semantic.get("data_provider", ""))
        or ""
    )
    return canonical_sha256(semantic)


def _verify_ed25519_payload(
    *,
    payload: dict[str, Any],
    signature_text: str,
    public_key_path: Path,
    expected_key_sha256: str,
) -> bool:
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        if (
            not public_key_path.is_file()
            or public_key_path.is_symlink()
            or not is_sha256(expected_key_sha256)
            or file_sha256(public_key_path) != expected_key_sha256
        ):
            return False
        key_bytes = public_key_path.read_bytes()
        try:
            public_key = serialization.load_pem_public_key(key_bytes)
        except ValueError:
            public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(key_bytes))
        if not isinstance(public_key, Ed25519PublicKey):
            return False
        public_key.verify(
            base64.b64decode(signature_text),
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8"),
        )
    except Exception:
        return False
    return True


def load_signed_build_provenance(
    *,
    trust_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load externally signed build identity; no production-side signing exists."""

    trust = dict(trust_policy or load_release_trust_policy())
    source_text = str(trust.get("build_provenance_path") or "").strip()
    expected_sha = str(trust.get("build_provenance_sha256") or "").strip().lower()
    source = Path(source_text) if source_text else Path()
    if (
        not source_text
        or not source.is_file()
        or source.is_symlink()
        or not is_sha256(expected_sha)
        or file_sha256(source) != expected_sha
    ):
        return {
            "valid": False,
            "production_authority_allowed": False,
            "errors": list(
                dict.fromkeys(
                    [
                        *list(trust.get("errors") or []),
                        "build_provenance_unavailable",
                    ]
                )
            ),
        }
    payload = read_json_object(source)
    errors: list[str] = []
    if str(payload.get("schema_version") or "") != "fxstack_signed_build_provenance_v1":
        errors.append("build_provenance_schema_invalid")
    if not is_sha256(payload.get("source_sha256")) or not str(
        payload.get("git_commit") or ""
    ).strip():
        errors.append("build_provenance_identity_invalid")
    if payload.get("source_clean") is not True:
        errors.append("build_provenance_source_dirty")
    signed_payload = dict(payload)
    signature = str(signed_payload.pop("signature", "") or "").strip()
    public_key_path_text = str(
        trust.get("build_provenance_public_key_path") or ""
    ).strip()
    expected_key_sha = str(
        trust.get("build_provenance_public_key_sha256") or ""
    ).strip().lower()
    public_key_path = Path(public_key_path_text) if public_key_path_text else Path()
    if not public_key_path_text or not _verify_ed25519_payload(
        payload=signed_payload,
        signature_text=signature,
        public_key_path=public_key_path,
        expected_key_sha256=expected_key_sha,
    ):
        errors.append("build_provenance_signature_invalid")
    installed_root_text = str(trust.get("installed_package_root") or "").strip()
    installed_root = Path(installed_root_text) if installed_root_text else Path()
    executing_root = Path(__file__).resolve().parents[1]
    if (
        not installed_root_text
        or not installed_root.is_absolute()
        or installed_root.resolve() != executing_root
    ):
        errors.append("build_provenance_executing_package_root_mismatch")
    measured_inventory, measured_digest, measure_errors = measure_package_tree(
        installed_root
    )
    errors.extend(measure_errors)
    signed_inventory = {
        str(key): str(value or "").strip().lower()
        for key, value in dict(signed_payload.get("package_inventory") or {}).items()
    }
    signed_package_digest = str(
        signed_payload.get("package_merkle_sha256") or ""
    ).strip().lower()
    if signed_inventory != measured_inventory:
        errors.append("build_provenance_package_inventory_mismatch")
    if (
        not is_sha256(signed_package_digest)
        or signed_package_digest != measured_digest
    ):
        errors.append("build_provenance_package_merkle_mismatch")
    for required_module in ("runtime/release_authority.py", "runtime/runner.py"):
        if required_module not in measured_inventory:
            errors.append(f"build_provenance_executing_module_missing:{required_module}")
    if trust.get("production_authority_allowed") is not True:
        errors.extend(list(trust.get("errors") or []))
        errors.append("build_provenance_trust_policy_not_authoritative")
    return {
        **signed_payload,
        "path": str(source.resolve()),
        "file_sha256": expected_sha,
        "measured_package_merkle_sha256": measured_digest,
        "executing_package_root": str(executing_root),
        "production_authority_allowed": bool(
            trust.get("production_authority_allowed") is True and not errors
        ),
        "valid": not errors,
        "errors": list(dict.fromkeys(errors)),
    }


def external_witness_errors(
    witness: dict[str, Any],
    *,
    expected_claims: dict[str, Any],
    now_ts: float | None = None,
    trust_policy: dict[str, Any] | None = None,
) -> list[str]:
    payload = dict(witness or {})
    errors: list[str] = []
    trust = dict(trust_policy or load_release_trust_policy())
    if trust.get("production_authority_allowed") is not True:
        errors.extend(list(trust.get("errors") or []))
        errors.append("release_witness_trust_policy_not_authoritative")
    if str(payload.get("schema_version") or "") != "fxstack_external_release_witness_v1":
        errors.append("release_witness_schema_invalid")
    configured_issuer = str(trust.get("issuer") or "").strip()
    configured_trust_domain = str(trust.get("witness_trust_domain") or "").strip()
    runtime_trust_domain = str(trust.get("runtime_trust_domain") or "").strip()
    runtime_host_id = str(trust.get("runtime_host_id") or "").strip()
    if not configured_issuer or str(payload.get("issuer") or "").strip() != configured_issuer:
        errors.append("release_witness_issuer_invalid")
    witness_domain = str(payload.get("trust_domain") or "").strip()
    witness_host = str(payload.get("witness_host_id") or "").strip()
    if not configured_trust_domain or witness_domain != configured_trust_domain:
        errors.append("release_witness_trust_domain_invalid")
    if not runtime_trust_domain or witness_domain == runtime_trust_domain:
        errors.append("release_witness_not_physically_independent")
    if not runtime_host_id or not witness_host or witness_host == runtime_host_id:
        errors.append("release_witness_host_not_independent")
    witness_principal = str(payload.get("witness_principal_id") or "").strip()
    expected_witness_principal = str(
        trust.get("witness_principal_id") or ""
    ).strip()
    runtime_principal = current_runtime_principal_id()
    if (
        not witness_principal
        or witness_principal.upper() != expected_witness_principal.upper()
    ):
        errors.append("release_witness_principal_invalid")
    if not runtime_principal or runtime_principal.upper() != str(
        trust.get("runtime_principal_id") or ""
    ).strip().upper():
        errors.append("release_runtime_principal_invalid")
    if witness_principal.upper() == runtime_principal.upper():
        errors.append("release_witness_principal_not_independent")
    if str(payload.get("workload_id") or "").strip() != str(
        trust.get("witness_workload_id") or ""
    ).strip():
        errors.append("release_witness_workload_invalid")
    if str(payload.get("purpose") or "").strip() != str(
        trust.get("witness_purpose") or ""
    ).strip():
        errors.append("release_witness_purpose_invalid")
    nonce = str(payload.get("nonce") or "").strip()
    if len(nonce) < 32:
        errors.append("release_witness_nonce_invalid")
    issued_at = _float(payload.get("issued_at"))
    expires_at = _float(payload.get("expires_at"))
    now = float(time.time() if now_ts is None else now_ts)
    if (
        not math.isfinite(issued_at)
        or not math.isfinite(expires_at)
        or issued_at <= 0.0
        or expires_at <= issued_at
        or issued_at > now + 5.0
        or expires_at <= now
        or expires_at - issued_at > 24.0 * 60.0 * 60.0
    ):
        errors.append("release_witness_time_window_invalid")
    claims = dict(payload.get("claims") or {})
    if claims != dict(expected_claims or {}):
        errors.append("release_witness_claims_mismatch")

    public_key_path_text = str(trust.get("witness_public_key_path") or "").strip()
    expected_key_sha = str(trust.get("witness_public_key_sha256") or "").strip().lower()
    public_key_path = Path(public_key_path_text) if public_key_path_text else Path()
    if (
        not public_key_path_text
        or not public_key_path.is_file()
        or public_key_path.is_symlink()
        or not is_sha256(expected_key_sha)
        or (public_key_path.is_file() and file_sha256(public_key_path) != expected_key_sha)
    ):
        errors.append("release_witness_public_key_unavailable")
    else:
        signature_text = str(payload.pop("signature", "") or "").strip()
        if not _verify_ed25519_payload(
            payload=payload,
            signature_text=signature_text,
            public_key_path=public_key_path,
            expected_key_sha256=expected_key_sha,
        ):
            errors.append("release_witness_signature_invalid")
    return list(dict.fromkeys(errors))


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return float("nan")


def db_model_identity(*, row: dict[str, Any] | None, pair: str) -> dict[str, str]:
    payload = dict(row or {})
    pair_key = str(pair).strip().upper()
    model_set_id = str(payload.get("model_set_id") or "").strip()
    metadata = dict(payload.get("metadata_json") or payload.get("metadata") or {})
    bundle_run_id = str(metadata.get("bundle_run_id") or model_set_id).strip()
    artifacts = dict(payload.get("artifacts_json") or payload.get("artifacts") or {})
    return {
        "pair": pair_key,
        "bundle_run_id": bundle_run_id,
        "model_set_id": model_set_id,
        "model_identity_sha256": canonical_model_identity_sha256(
            pair=pair_key,
            bundle_run_id=bundle_run_id,
            model_set_id=model_set_id,
            artifacts=artifacts,
        ),
        "artifact_set_sha256": artifact_set_sha256(
            artifacts,
            expected_pair=pair_key,
        ),
    }


def manifest_model_identity(*, manifest_path: str | Path, pair: str) -> dict[str, str]:
    path = Path(manifest_path).resolve()
    semantic = active_manifest_identity(manifest_path=path, pair=pair)
    return {
        "pair": semantic.pair,
        "bundle_run_id": semantic.bundle_run_id,
        "model_set_id": semantic.model_set_id,
        "model_identity_sha256": semantic.model_manifest_sha256,
        "artifact_set_sha256": semantic.artifact_set_sha256,
        "manifest_path": str(path),
        "manifest_file_sha256": file_sha256(path) if path.is_file() else "",
    }


def authority_request_errors(
    request: dict[str, Any],
    *,
    active_db_row: dict[str, Any] | None = None,
    validate_evidence: bool = True,
) -> list[str]:
    from fxstack.settings import get_settings

    payload = dict(request or {})
    errors: list[str] = []
    trust = load_release_trust_policy()
    errors.extend(
        # Never accept request-supplied booleans as physical observation.
        # A future DB/bridge probe must pass independently collected evidence
        # at a trusted call boundary; until then authority is non-operable.
        physical_boundary_errors(
            trust,
            observed_capabilities=observe_physical_capabilities(
                get_settings(),
                policy=trust,
            ),
        )
    )
    if str(payload.get("schema_version") or "") != RELEASE_AUTHORITY_REQUEST_SCHEMA:
        errors.append("release_authority_request_schema_invalid")
    pair = str(payload.get("pair") or "").strip().upper()
    bundle_run_id = str(payload.get("bundle_run_id") or "").strip()
    model_set_id = str(payload.get("model_set_id") or "").strip()
    generation_id = str(payload.get("generation_id") or "").strip()
    if not pair or str(payload.get("scope_key") or "").strip().upper() != pair:
        errors.append("release_authority_scope_invalid")
    if not generation_id:
        errors.append("release_authority_generation_missing")
    if not bundle_run_id or not model_set_id:
        errors.append("release_authority_model_identity_incomplete")
    for field in (
        "model_identity_sha256",
        "artifact_set_sha256",
        "manifest_file_sha256",
        "phase5_bundle_sha256",
        "release_validation_bundle_sha256",
        "evidence_merkle_sha256",
        "source_sha256",
        "package_merkle_sha256",
        "config_sha256",
    ):
        if not is_sha256(payload.get(field)):
            errors.append(f"release_authority_{field}_invalid")
    if str(payload.get("request_sha256") or "").strip().lower() != release_request_sha256(payload):
        errors.append("release_authority_request_hash_mismatch")
    if str(payload.get("unsigned_request_sha256") or "").strip().lower() != (
        unsigned_release_request_sha256(payload)
    ):
        errors.append("release_authority_unsigned_request_hash_mismatch")
    if payload.get("source_clean") is not True or not str(payload.get("git_commit") or "").strip():
        errors.append("release_authority_source_unattested")
    execution = dict(payload.get("authorized_execution") or {})
    if str(execution.get("agent_mode") or "").strip().lower() != "live":
        errors.append("release_authority_mode_invalid")
    if str(execution.get("execution_provider") or "").strip().lower() != "mt4":
        errors.append("release_authority_provider_invalid")
    if str(execution.get("account_mode") or "").strip().lower() not in {"demo", "real"}:
        errors.append("release_authority_account_mode_invalid")
    if not str(execution.get("account_scope") or "").strip():
        errors.append("release_authority_account_scope_invalid")
    if [str(item).strip().upper() for item in list(execution.get("pair_scope") or [])] != [pair]:
        errors.append("release_authority_pair_scope_invalid")
    if not list(execution.get("sleeve_scope") or []) or "enter" not in {
        str(item).strip().lower() for item in list(execution.get("intent_scope") or [])
    }:
        errors.append("release_authority_strategy_scope_invalid")
    protective_intents = {
        str(item).strip().lower()
        for item in list(execution.get("protective_intent_scope") or [])
        if str(item).strip()
    }
    if protective_intents != {"exit", "adjust"}:
        errors.append("release_authority_protective_scope_invalid")
    if not isinstance(execution.get("emergency_flatten_all"), bool):
        errors.append("release_authority_emergency_flatten_scope_invalid")
    witness_claims = release_witness_claims(payload)
    errors.extend(
        external_witness_errors(
            dict(payload.get("external_witness") or {}),
            expected_claims=witness_claims,
            trust_policy=trust,
        )
    )

    local_bindings = dict(payload.get("local_bindings") or {})
    evidence_root = Path(
        str(local_bindings.get("evidence_root_path") or "").strip()
    )
    evidence_refs = dict(payload.get("evidence_refs") or {})
    evidence_hashes = {
        str(key): str(value or "").strip().lower()
        for key, value in dict(payload.get("evidence_hashes") or {}).items()
    }
    if str(payload.get("evidence_merkle_sha256") or "") != canonical_sha256(
        evidence_hashes
    ):
        errors.append("release_authority_evidence_merkle_mismatch")
    manifest_path = resolve_contained_evidence_ref(
        evidence_root=evidence_root,
        reference=str(evidence_refs.get("model_manifest") or ""),
    )
    phase5_path = resolve_contained_evidence_ref(
        evidence_root=evidence_root,
        reference=str(evidence_refs.get("phase5_gate_bundle") or ""),
    )
    release_path = resolve_contained_evidence_ref(
        evidence_root=evidence_root,
        reference=str(evidence_refs.get("release_validation_bundle") or ""),
    )
    for legacy_path_field in (
        "manifest_path",
        "phase5_bundle_path",
        "release_validation_bundle_path",
    ):
        if str(payload.get(legacy_path_field) or "").strip():
            errors.append(f"release_authority_legacy_absolute_ref_forbidden:{legacy_path_field}")
    if manifest_path is None or not manifest_path.is_file():
        errors.append("release_authority_manifest_missing")
    else:
        manifest_identity = manifest_model_identity(manifest_path=manifest_path, pair=pair)
        for field in (
            "bundle_run_id",
            "model_set_id",
            "model_identity_sha256",
            "artifact_set_sha256",
            "manifest_file_sha256",
        ):
            if str(manifest_identity.get(field) or "") != str(payload.get(field) or ""):
                errors.append(f"release_authority_manifest_{field}_mismatch")
    if active_db_row is None:
        errors.append("release_authority_active_db_row_missing")
    else:
        db_identity = db_model_identity(row=active_db_row, pair=pair)
        for field in (
            "bundle_run_id",
            "model_set_id",
            "model_identity_sha256",
            "artifact_set_sha256",
        ):
            if str(db_identity.get(field) or "") != str(payload.get(field) or ""):
                errors.append(f"release_authority_db_{field}_mismatch")

    if phase5_path is None or not phase5_path.is_file() or file_sha256(phase5_path) != str(
        payload.get("phase5_bundle_sha256") or ""
    ):
        errors.append("release_authority_phase5_bundle_hash_mismatch")
    if release_path is None or not release_path.is_file() or file_sha256(release_path) != str(
        payload.get("release_validation_bundle_sha256") or ""
    ):
        errors.append("release_authority_release_bundle_hash_mismatch")
    for key, expected_field in (
        ("model_manifest", "manifest_file_sha256"),
        ("phase5_gate_bundle", "phase5_bundle_sha256"),
        ("release_validation_bundle", "release_validation_bundle_sha256"),
    ):
        if str(evidence_hashes.get(key) or "") != str(payload.get(expected_field) or ""):
            errors.append(f"release_authority_evidence_hash_mismatch:{key}")
    if validate_evidence and phase5_path is not None and phase5_path.is_file():
        phase5_payload = read_json_object(phase5_path)
        validation = validate_phase5_gate_bundle(
            phase5_payload,
            expected_pair=pair,
            expected_bundle_run_id=bundle_run_id,
            evidence_root=evidence_root,
        )
        if not validation.valid:
            errors.extend(f"release_authority_phase5:{item}" for item in validation.errors)
            for gate_name, gate_items in sorted(validation.gate_errors.items()):
                errors.extend(
                    f"release_authority_phase5:{gate_name}:{item}"
                    for item in gate_items
                )
        required_gate_passes = {
            "research_gate",
            "economic_gate",
            "operational_gate",
            "shadow_gate",
            "canary_gate",
        }
        for gate_name in sorted(required_gate_passes):
            if validation.gate_passes.get(gate_name) is not True:
                errors.append(f"release_authority_phase5:{gate_name}:not_passed")
        refs = dict(phase5_payload.get("evidence_refs") or {})
        hashes = dict(phase5_payload.get("evidence_hashes") or {})
        phase5_release_path = resolve_contained_evidence_ref(
            evidence_root=evidence_root,
            reference=str(refs.get("release_validation_bundle") or ""),
        )
        if (
            phase5_release_path is None
            or release_path is None
            or file_sha256(phase5_release_path) != file_sha256(release_path)
        ):
            errors.append("release_authority_release_bundle_content_mismatch")
        if str(hashes.get("release_validation_bundle") or "") != str(
            payload.get("release_validation_bundle_sha256") or ""
        ):
            errors.append("release_authority_release_bundle_phase5_hash_mismatch")
    return list(dict.fromkeys(errors))


def active_authority_errors(
    authority: dict[str, Any],
    *,
    active_db_row: dict[str, Any] | None,
    runtime_boot_id: str,
    runtime_attestation: dict[str, Any],
    expected_generation_id: str = "",
    expected_request_sha256: str = "",
    validate_evidence: bool = True,
) -> list[str]:
    state = dict(authority or {})
    errors: list[str] = []
    if str(state.get("schema_version") or "") != RELEASE_AUTHORITY_STATE_SCHEMA:
        errors.append("release_authority_state_schema_invalid")
    if str(state.get("status") or "").strip().lower() != "active":
        errors.append("release_authority_not_active")
    request = dict(state.get("request") or {})
    errors.extend(
        authority_request_errors(
            request,
            active_db_row=active_db_row,
            validate_evidence=validate_evidence,
        )
    )
    generation_id = str(request.get("generation_id") or "")
    request_sha = str(request.get("request_sha256") or "")
    if expected_generation_id and generation_id != str(expected_generation_id):
        errors.append("release_authority_generation_changed")
    if expected_request_sha256 and request_sha != str(expected_request_sha256):
        errors.append("release_authority_request_changed")
    ack = dict(state.get("ack") or {})
    if str(ack.get("schema_version") or "") != RELEASE_AUTHORITY_ACK_SCHEMA:
        errors.append("release_authority_ack_schema_invalid")
    if str(ack.get("generation_id") or "") != generation_id:
        errors.append("release_authority_ack_generation_mismatch")
    if str(ack.get("request_sha256") or "") != request_sha:
        errors.append("release_authority_ack_request_mismatch")
    if not str(runtime_boot_id or "").strip() or str(ack.get("runtime_boot_id") or "") != str(
        runtime_boot_id
    ):
        errors.append("release_authority_ack_boot_mismatch")
    for field in (
        "source_sha256",
        "package_merkle_sha256",
        "config_sha256",
        "manifest_file_sha256",
        "model_identity_sha256",
        "artifact_set_sha256",
        "model_set_id",
    ):
        observed = str(dict(runtime_attestation or {}).get(field) or "")
        if not observed or str(ack.get(field) or "") != observed or str(request.get(field) or "") != observed:
            errors.append(f"release_authority_ack_{field}_mismatch")
    if dict(runtime_attestation or {}).get("source_clean") is not True:
        errors.append("release_authority_runtime_source_dirty")
    return list(dict.fromkeys(errors))
