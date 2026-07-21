"""Side-effect-free Python gate for every production runtime entrypoint."""

# AGENT: ROLE: Authoritative Python startup gate before bridge, runtime service, database, or state access.
# AGENT: CALLED BY: `fxstack.runtime.runner.run_loop` for CLI and direct runner invocation.
# AGENT: SIDE EFFECTS: None; validates settings and reads local manifest/artifact evidence only.
# AGENT: HANDSHAKE: Settings posture + active manifest -> runtime startup admission.

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Callable

from fxstack.runtime.model_manifest_preflight import preflight_active_model_manifest
from fxstack.runtime.release_trust import physical_boundary_errors


class RuntimeStartupPreflightError(RuntimeError):
    """The runtime is not allowed to touch its bridge or state store."""


FORBIDDEN_RUNTIME_MODULES = (
    "fxstack.backtest",
    "fxstack.improve",
    "fxstack.labels",
    "fxstack.llm",
    "fxstack.research",
    "fxstack.security",
    "fxstack.tasks",
    "fxstack.api.__main__",
    "fxstack.belief.adapters",
    "fxstack.belief.dataset",
    "fxstack.belief.labels",
    "fxstack.belief.outcome_labels",
    "fxstack.data.provider_migration",
    "fxstack.feast.compaction",
    "fxstack.feast.offline_builder",
    "fxstack.feast.parquet_adapter",
    "fxstack.feast.repository",
    "fxstack.mlops.lineage",
    "fxstack.mlops.model_uri",
    "fxstack.mlops.registry",
    "fxstack.mlops.run_context",
    "fxstack.mlops.types",
    "fxstack.models.patchtst",
    "fxstack.orchestration.approvals",
    "fxstack.orchestration.experiments",
    "fxstack.orchestration.promotion",
    "fxstack.orchestration.replay",
    "fxstack.providers.execution.paper",
    "fxstack.providers.paper_execution",
    "fxstack.rl._common",
    "fxstack.rl.envs",
    "fxstack.rl.evaluate",
    "fxstack.rl.export_replay",
    "fxstack.rl.offline_dataset",
    "fxstack.rl.portfolio_env",
    "fxstack.rl.reward",
    "fxstack.rl.stress_harness",
    "fxstack.rl.trainer",
    "fxstack.rl.train_offline",
    "fxstack.rl.train_online",
    "fxstack.runtime.service_contract",
    "fxstack.schemas.bars",
    "fxstack.training.activation",
    "fxstack.training.belief",
    "fxstack.training.counterfactual_eval",
    "fxstack.training.datasets",
    "fxstack.training.fingerprint",
    "fxstack.training.lifecycle_validation",
    "fxstack.training.objectives",
    "fxstack.training.phase4_types",
    "fxstack.training.phase5_gates",
    "fxstack.training.promotion",
    "fxstack.training.registry",
    "fxstack.training.release_package",
    "fxstack.training.release_workflow",
    "fxstack.training.research_manifest",
    "fxstack.training.sequence_dataset",
    "fxstack.training.splits",
    "fxstack.training.uncertainty",
    "fxstack.utils.time",
    "mlflow",
)


def runtime_physical_isolation_errors(
    *,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
) -> list[str]:
    """Require offline research and write-side control modules to be absent."""

    errors: list[str] = []
    for module_name in FORBIDDEN_RUNTIME_MODULES:
        try:
            spec = find_spec(module_name)
        except Exception as exc:
            errors.append(
                f"runtime isolation probe failed for {module_name}:{type(exc).__name__}"
            )
            continue
        if spec is not None:
            errors.append(
                f"runtime distribution must not contain importable module {module_name}"
            )
    return errors


def runtime_launch_posture_errors(settings: Any) -> list[str]:
    """Return deterministic profile/mode/arming/scope errors for a runtime."""

    profile = str(getattr(settings, "start_profile", "") or "").strip().lower()
    mode = str(getattr(settings, "agent_mode", "") or "").strip().lower()
    errors: list[str] = []
    if profile == "paper":
        return [
            "FXSTACK_START_PROFILE=paper is unavailable in the production runtime distribution"
        ]
    required_mode = {
        "staged_safe": "shadow",
        "live": "live",
    }.get(profile)
    if required_mode is None:
        errors.append("FXSTACK_START_PROFILE must be staged_safe or live")
    elif mode != required_mode:
        errors.append(
            f"FXSTACK_START_PROFILE={profile} requires FXSTACK_AGENT_MODE={required_mode}"
        )

    if profile != "live":
        return errors

    # Live is a physical capability, not a settings posture.  Until the fixed
    # OS trust policy proves least-privilege DB roles, a single terminal-wide
    # EA lease/consumer token, credential rotation, and research separation,
    # startup names the exact blocker and remains fail closed.  Staged-safe
    # operation intentionally does not require these live-only capabilities.
    errors.extend(physical_boundary_errors())

    if not bool(getattr(settings, "live_armed", False)):
        errors.append("live startup requires explicit FXSTACK_LIVE_ARMED=1")
    expected_account_mode = str(
        getattr(settings, "live_expected_account_mode", "") or ""
    ).strip().lower()
    if expected_account_mode not in {"demo", "real"}:
        errors.append(
            "live startup requires explicit FXSTACK_LIVE_EXPECTED_ACCOUNT_MODE=demo or real"
        )
    if not bool(getattr(settings, "structure_timing_enabled", False)):
        errors.append(
            "live startup requires FXSTACK_STRUCTURE_TIMING_ENABLED=true so structure-timing and chase hard gates are binding"
        )
    if not bool(getattr(settings, "use_uncertainty_gate", False)):
        errors.append(
            "live startup requires FXSTACK_USE_UNCERTAINTY_GATE=true so uncertainty hard gates are binding"
        )
    if not bool(getattr(settings, "belief_enabled", False)):
        errors.append(
            "live startup requires FXSTACK_BELIEF_ENABLED=true so the activated belief model is computed"
        )
    if not bool(getattr(settings, "belief_runtime_required", False)):
        errors.append(
            "live startup requires FXSTACK_BELIEF_RUNTIME_REQUIRED=true so a missing belief model fails closed"
        )
    belief_influence_mode = str(
        getattr(settings, "belief_influence_mode", "off") or "off"
    ).strip().lower()
    if belief_influence_mode != "hard_gate":
        errors.append(
            "live startup requires FXSTACK_BELIEF_INFLUENCE_MODE=hard_gate so belief verdicts bind entries"
        )
    if not bool(getattr(settings, "campaign_manager_enabled", False)):
        errors.append(
            "live startup requires FXSTACK_CAMPAIGN_MANAGER_ENABLED=true so campaign governance is active"
        )
    if not bool(getattr(settings, "capital_governance_enabled", False)):
        errors.append(
            "live startup requires FXSTACK_CAPITAL_GOVERNANCE_ENABLED=true so drawdown and operational governance bind"
        )

    configured_pairs = {
        str(item).strip().upper()
        for item in list(getattr(settings, "pairs", []) or [])
        if str(item).strip()
    }
    pair_scope = {
        str(item).strip().upper()
        for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        if str(item).strip()
    }
    sleeve_scope = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
        if str(item).strip()
    }
    intent_scope = {
        str(item).strip().lower()
        for item in list(getattr(settings, "agent_live_intent_allowlist", []) or [])
        if str(item).strip()
    }
    for env_name, scope in (
        ("FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST", pair_scope),
        ("FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST", sleeve_scope),
        ("FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST", intent_scope),
    ):
        if not scope:
            errors.append(f"live startup requires explicit non-empty {env_name}")
    unknown_pairs = sorted(pair_scope - configured_pairs)
    if unknown_pairs:
        errors.append(
            "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST contains pairs outside FXSTACK_PAIRS:"
            + ",".join(unknown_pairs)
        )
    return errors


def _repository_root(settings: Any) -> Path:
    project_root = Path(getattr(settings, "project_root", Path.cwd())).expanduser().resolve()
    if project_root.name.lower() == "fx-quant-stack":
        return project_root.parent
    return project_root


def validate_runtime_startup(
    settings: Any,
    *,
    find_spec: Callable[[str], Any] = importlib.util.find_spec,
) -> dict[str, Any]:
    """Fail before any bridge/service access and return read-only model evidence."""

    errors = [str(item) for item in list(settings.validate_for_startup() or []) if str(item)]
    errors.extend(runtime_launch_posture_errors(settings))
    errors.extend(runtime_physical_isolation_errors(find_spec=find_spec))
    if errors:
        raise RuntimeStartupPreflightError(
            "runtime_startup_config_invalid:" + " | ".join(errors)
        )

    pairs = [str(item).strip().upper() for item in list(settings.pairs) if str(item).strip()]
    result = preflight_active_model_manifest(
        manifest_path=Path(str(settings.model_activation_manifest)),
        project_root=_repository_root(settings),
        required_pairs=pairs,
    )
    return {
        **dict(result),
        "settings_validated": True,
        "profile": str(settings.start_profile).strip().lower(),
        "agent_mode": str(settings.agent_mode).strip().lower(),
    }


__all__ = [
    "FORBIDDEN_RUNTIME_MODULES",
    "RuntimeStartupPreflightError",
    "runtime_launch_posture_errors",
    "runtime_physical_isolation_errors",
    "validate_runtime_startup",
]
