"""Content identity for the production-owned IG MT4 scalp engine.

The external validation host and the installed runtime both compute this
identity from the same small, reviewed set of production modules.  Newline
normalization keeps the digest portable across Windows and Linux checkouts;
any other source change invalidates the evidence by design.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


SCALP_ENGINE_IDENTITY_SCHEMA = "fxstack.production_scalp_engine_identity.v3"
SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS: tuple[str, ...] = (
    "runtime/scalp_engine_identity.py",
    "runtime/mtvclc_validation_evidence_v2.py",
    "runtime/mtvclc_validation_evidence_v3.py",
    "runtime/mtvclc_runtime_release.py",
    "runtime/scalp_runtime_admission.py",
    "runtime/live_launch_authority_preflight.py",
    "runtime/scalp_execution_authority.py",
    "runtime/execution_ack_attestation.py",
)
SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS: tuple[str, ...] = (
    "MQL4/Experts/BridgeEA.mq4",
    "MQL4/Include/BridgeHttp.mqh",
    "MQL4/Include/BridgeUtils.mqh",
)
SCALP_ENGINE_COMPONENTS: tuple[str, ...] = (
    "api/app.py",
    "api/auth.py",
    "api/schemas.py",
    "api/wire.py",
    "data/live_quotes.py",
    "live/policy.py",
    "portfolio/__init__.py",
    "portfolio/allocator.py",
    "portfolio/book.py",
    "portfolio/budgeting.py",
    "portfolio/concentration.py",
    "portfolio/correlation.py",
    "portfolio/stress.py",
    "portfolio/telemetry.py",
    "providers/catalog.py",
    "providers/contracts.py",
    "providers/execution/mt4.py",
    "providers/ig_mt4_catalog.py",
    "providers/market/mt4_bridge.py",
    "providers/registry.py",
    "risk/kernel.py",
    "risk/contracts.py",
    "risk/envelope.py",
    "risk/sizing.py",
    "runtime/dto.py",
    "runtime/governance.py",
    "runtime/orchestration_bridge.py",
    "runtime/market_source_identity.py",
    "runtime/postgres_store.py",
    "runtime/protocol.py",
    "runtime/release_authority.py",
    "runtime/runner.py",
    "runtime/service.py",
    "runtime/service_contract.py",
    "runtime/startup.py",
    "runtime/startup_preflight.py",
    "schemas/entry.py",
    "settings.py",
    "strategy/mtvclc.py",
    "runtime/broker_contract_state.py",
    "runtime/mtvclc_proposal_batch.py",
    "runtime/scalp_cost_snapshot.py",
    "runtime/mtvclc_cycle_capacity.py",
    "runtime/scalp_position_lifecycle.py",
    "runtime/mtvclc_entry_qualification.py",
    "runtime/mtvclc_entry_quote.py",
    "runtime/scalp_execution_boundary.py",
    "runtime/scalp_daily_budget.py",
    "runtime/scalp_restart_reconciliation.py",
    "runtime/scalp_rollover_guard.py",
    *SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS,
    "runtime/scalp_runtime_control.py",
    "runtime/scalp_live_loop.py",
    *SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS,
)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _normalized_source_bytes(path: Path) -> bytes:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"production_scalp_engine_component_unreadable:{path.name}"
        ) from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class ProductionScalpEngineIdentity:
    engine_sha256: str
    component_sha256: tuple[tuple[str, str], ...]
    schema_version: str = SCALP_ENGINE_IDENTITY_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def production_scalp_engine_identity(
    *,
    package_root: str | Path | None = None,
    repository_root: str | Path | None = None,
) -> ProductionScalpEngineIdentity:
    """Hash every production-scalper execution-semantic source component."""

    explicit_package_root = package_root is not None
    root = (
        Path(package_root)
        if explicit_package_root
        else Path(__file__).resolve().parents[1]
    ).resolve()
    bridge_root = (
        Path(repository_root).resolve()
        if repository_root is not None
        else (root if explicit_package_root else root.parents[2])
    )
    component_hashes: list[tuple[str, str]] = []
    for relative in SCALP_ENGINE_COMPONENTS:
        component_root = (
            bridge_root if relative in SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS else root
        )
        path = (component_root / Path(relative)).resolve()
        try:
            inside_root = path.is_relative_to(component_root)
        except AttributeError:  # pragma: no cover - Python >=3.11 in production
            inside_root = str(path).startswith(str(component_root) + str(Path("/")))
        if not inside_root or not path.is_file():
            raise RuntimeError(f"production_scalp_engine_component_missing:{relative}")
        digest = hashlib.sha256(_normalized_source_bytes(path)).hexdigest()
        component_hashes.append((relative, digest))

    payload = {
        "schema_version": SCALP_ENGINE_IDENTITY_SCHEMA,
        "components": [
            {"path": relative, "sha256": digest}
            for relative, digest in component_hashes
        ],
    }
    engine_sha256 = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return ProductionScalpEngineIdentity(
        engine_sha256=engine_sha256,
        component_sha256=tuple(component_hashes),
    )


__all__ = [
    "SCALP_ENGINE_COMPONENTS",
    "SCALP_ENGINE_IDENTITY_SCHEMA",
    "SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS",
    "SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS",
    "ProductionScalpEngineIdentity",
    "production_scalp_engine_identity",
]
