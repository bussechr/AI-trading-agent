"""Content identity for the production-owned IG MT4 scalp engine.

The external validation host and the installed runtime both compute this
identity from the same small, reviewed set of production modules.  Newline
normalization keeps the digest portable across Windows and Linux checkouts;
any other source change invalidates the evidence by design.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from functools import lru_cache
import hashlib
from itertools import chain
import json
from pathlib import Path
import stat
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
    "live/entry_protection.py",
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

# Two workers overlap Windows filesystem latency without turning the one-second
# identity refresh into a wide fan-out.  The executor starts its threads lazily
# and reuses them; component bytes and digests are deliberately never cached.
_ENGINE_HASH_WORKERS = 2
_ENGINE_HASH_EXECUTOR = ThreadPoolExecutor(
    max_workers=_ENGINE_HASH_WORKERS,
    thread_name_prefix="fxstack-scalp-engine-hash",
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
        payload = path.read_bytes()
        if not payload.isascii():
            payload.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"production_scalp_engine_component_unreadable:{path.name}"
        ) from exc
    if b"\r" in payload:
        payload = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return payload


def _hash_component(
    item: tuple[str, Path, Path, bool],
) -> tuple[str, str]:
    relative, component_root, path, inside_root = item
    try:
        component_mode = path.lstat().st_mode
    except OSError as exc:
        raise RuntimeError(
            f"production_scalp_engine_component_missing:{relative}"
        ) from exc
    if stat.S_ISLNK(component_mode):
        path = path.resolve()
        inside_root = inside_root and path.is_relative_to(component_root)
        try:
            component_mode = path.stat().st_mode
        except OSError as exc:
            raise RuntimeError(
                f"production_scalp_engine_component_missing:{relative}"
            ) from exc
    if not inside_root or not stat.S_ISREG(component_mode):
        raise RuntimeError(f"production_scalp_engine_component_missing:{relative}")
    return relative, hashlib.sha256(_normalized_source_bytes(path)).hexdigest()


@lru_cache(maxsize=4)
def _component_plan(
    root: Path,
    bridge_root: Path,
) -> tuple[
    tuple[tuple[Path, Path], ...],
    tuple[tuple[str, Path, Path, str], ...],
]:
    """Cache lexical paths only; filesystem identity is rechecked every call."""

    parent_roots: dict[Path, Path] = {}
    specs: list[tuple[str, Path, Path, str]] = []
    for relative in SCALP_ENGINE_COMPONENTS:
        component_root = (
            bridge_root
            if relative in SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS
            else root
        )
        relative_path = Path(relative)
        declared_parent = component_root / relative_path.parent
        parent_roots.setdefault(declared_parent, component_root)
        specs.append(
            (relative, component_root, declared_parent, relative_path.name)
        )
    return tuple(parent_roots.items()), tuple(specs)


def _resolve_component_parent(
    item: tuple[Path, Path],
) -> tuple[Path, Path, bool]:
    declared_parent, component_root = item
    resolved_parent = declared_parent.resolve()
    return (
        declared_parent,
        resolved_parent,
        resolved_parent.is_relative_to(component_root),
    )


def _resolve_component_parent_batch(
    items: tuple[tuple[Path, Path], ...],
) -> tuple[tuple[Path, Path, bool], ...]:
    return tuple(_resolve_component_parent(item) for item in items)


def _hash_component_batch(
    items: tuple[tuple[str, Path, Path, bool], ...],
) -> tuple[tuple[str, str], ...]:
    return tuple(_hash_component(item) for item in items)


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
    parent_inputs, component_specs = _component_plan(root, bridge_root)
    parent_batches = tuple(
        tuple(parent_inputs[offset::_ENGINE_HASH_WORKERS])
        for offset in range(_ENGINE_HASH_WORKERS)
    )
    resolved_parents = {
        declared_parent: (resolved_parent, inside_root)
        for declared_parent, resolved_parent, inside_root in chain.from_iterable(
            _ENGINE_HASH_EXECUTOR.map(
                _resolve_component_parent_batch,
                parent_batches,
            )
        )
    }
    component_inputs: list[tuple[str, Path, Path, bool]] = []
    for relative, component_root, declared_parent, name in component_specs:
        component_parent, inside_root = resolved_parents[declared_parent]
        path = component_parent / name
        component_inputs.append((relative, component_root, path, inside_root))

    batches = tuple(
        tuple(component_inputs[offset::_ENGINE_HASH_WORKERS])
        for offset in range(_ENGINE_HASH_WORKERS)
    )
    hashes_by_path = dict(
        chain.from_iterable(
            _ENGINE_HASH_EXECUTOR.map(_hash_component_batch, batches)
        )
    )
    component_hashes = [
        (relative, hashes_by_path[relative]) for relative in SCALP_ENGINE_COMPONENTS
    ]

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
