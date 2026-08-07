from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime import scalp_engine_identity as engine_identity_module
from fxstack.runtime import scalp_runtime_admission as admission_module
from fxstack.runtime.scalp_engine_identity import (
    SCALP_ENGINE_COMPONENTS,
    SCALP_ENGINE_IDENTITY_SCHEMA,
    SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS,
    SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS,
    production_scalp_engine_identity,
)
from fxstack.runtime.scalp_runtime_admission import (
    SCALP_ADMISSION_MODE_SIGNED,
    verify_configured_scalp_runtime_admission,
)
from fxstack.strategy.mtvclc import MTVCLC_CONFIG_SHA256


NOW = 1_800_000_002.0


def _write_engine_tree(root: Path, *, newline: str = "\n") -> None:
    for index, relative in enumerate(SCALP_ENGINE_COMPONENTS, start=1):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# component {index}{newline}VALUE = {index}{newline}",
            encoding="utf-8",
            newline="",
        )


def _settings(root: Path, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "project_root": root,
        "live_expected_account_mode": "demo",
        "production_scalp_generation_id": "mtvclc-runtime-generation-test",
        "production_scalp_mtvclc_release_bundle": "release.json",
        "production_scalp_mtvclc_evidence_verify_key_file": "evidence-public.pem",
        "production_scalp_mtvclc_release_verify_key_file": "release-public.pem",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _load_release_test_support() -> ModuleType:
    """Reuse the issuer's complete signed-evidence fixture without real keys."""

    module_name = "admission_runtime_release_test_support"
    loaded = sys.modules.get(module_name)
    if isinstance(loaded, ModuleType):
        return loaded
    path = (
        Path(__file__).resolve().parents[2] / "tests" / "test_mtvclc_runtime_release.py"
    )
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def signed_release(tmp_path: Path) -> SimpleNamespace:
    support = _load_release_test_support()
    evidence_private = Ed25519PrivateKey.generate()
    release_private = Ed25519PrivateKey.generate()
    _evidence_private_path, evidence_public_path = support._write_keys(
        tmp_path,
        prefix="evidence",
        private_key=evidence_private,
    )
    release_private_path, release_public_path = support._write_keys(
        tmp_path,
        prefix="release",
        private_key=release_private,
    )
    package_root, repository_root = support._engine_tree(tmp_path / "engine")
    preregistration_path, preregistration = support._v5_preregistration(
        tmp_path / "prereg",
        package_root=package_root,
        repository_root=repository_root,
    )
    evidence_bundle_path = support._write_canonical(
        tmp_path / "evidence-v3.json",
        support._signed_evidence_bundle(
            evidence_private_key=evidence_private,
            preregistration=preregistration,
        ),
    )
    ceremony = support.CeremonyFixture(
        preregistration_path=preregistration_path,
        evidence_bundle_path=evidence_bundle_path,
        evidence_public_key_path=evidence_public_path,
        release_public_key_path=release_public_path,
        release_private_key_path=release_private_path,
        package_root=package_root,
        repository_root=repository_root,
    )
    request_path, _request, _engine = support._prepare(
        ceremony,
        output=tmp_path / "release-request.json",
    )
    release_path, bundle = support._issue(
        ceremony,
        request=request_path,
        output=tmp_path / "release.json",
    )
    settings = _settings(
        repository_root,
        production_scalp_mtvclc_release_bundle=release_path,
        production_scalp_mtvclc_evidence_verify_key_file=evidence_public_path,
        production_scalp_mtvclc_release_verify_key_file=release_public_path,
    )
    return SimpleNamespace(
        bundle=bundle,
        bundle_path=release_path,
        evidence_public_key_path=evidence_public_path,
        release_public_key_path=release_public_path,
        package_root=package_root,
        repository_root=repository_root,
        settings=settings,
    )


def _admit(case: SimpleNamespace, **kwargs: Any):
    return verify_configured_scalp_runtime_admission(
        case.settings,
        now_epoch=NOW,
        package_root=case.package_root,
        **kwargs,
    )


def test_engine_identity_is_deterministic_and_newline_portable(tmp_path: Path) -> None:
    lf_root = tmp_path / "lf"
    crlf_root = tmp_path / "crlf"
    _write_engine_tree(lf_root, newline="\n")
    _write_engine_tree(crlf_root, newline="\r\n")

    first = production_scalp_engine_identity(package_root=lf_root)
    second = production_scalp_engine_identity(package_root=lf_root)
    portable = production_scalp_engine_identity(package_root=crlf_root)

    assert first == second == portable
    assert first.schema_version == "fxstack.production_scalp_engine_identity.v3"
    assert first.schema_version == SCALP_ENGINE_IDENTITY_SCHEMA
    assert len(first.engine_sha256) == 64
    assert tuple(path for path, _ in first.component_sha256) == SCALP_ENGINE_COMPONENTS


def test_engine_identity_requires_release_and_authority_security_components() -> None:
    assert SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS == (
        "runtime/scalp_engine_identity.py",
        "runtime/mtvclc_validation_evidence_v2.py",
        "runtime/mtvclc_validation_evidence_v3.py",
        "runtime/mtvclc_runtime_release.py",
        "runtime/scalp_runtime_admission.py",
        "runtime/live_launch_authority_preflight.py",
        "runtime/scalp_execution_authority.py",
        "runtime/execution_ack_attestation.py",
    )
    assert set(SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS).issubset(
        SCALP_ENGINE_COMPONENTS
    )


def test_engine_identity_requires_last_mile_bridge_components() -> None:
    assert SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS == (
        "MQL4/Experts/BridgeEA.mq4",
        "MQL4/Include/BridgeHttp.mqh",
        "MQL4/Include/BridgeUtils.mqh",
    )
    assert set(SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS).issubset(
        SCALP_ENGINE_COMPONENTS
    )


def test_engine_identity_binds_risk_and_portfolio_execution_semantics() -> None:
    required = {
        "risk/contracts.py",
        "risk/envelope.py",
        "risk/kernel.py",
        "risk/sizing.py",
        "portfolio/allocator.py",
        "portfolio/book.py",
        "portfolio/budgeting.py",
        "portfolio/concentration.py",
        "portfolio/correlation.py",
        "portfolio/stress.py",
        "portfolio/telemetry.py",
    }
    assert required.issubset(SCALP_ENGINE_COMPONENTS)


def test_engine_identity_binds_only_the_active_mtvclc_proposal_path() -> None:
    assert {
        "strategy/mtvclc.py",
        "runtime/mtvclc_proposal_batch.py",
        "runtime/mtvclc_cycle_capacity.py",
        "runtime/mtvclc_entry_qualification.py",
        "runtime/mtvclc_entry_quote.py",
    }.issubset(SCALP_ENGINE_COMPONENTS)
    assert "strategy/scalp_dislocation.py" not in SCALP_ENGINE_COMPONENTS
    assert "runtime/scalp_proposal_batch.py" not in SCALP_ENGINE_COMPONENTS


def test_engine_identity_supports_separate_installed_package_and_bridge_roots(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "runtime" / "site-packages" / "fxstack"
    repository_root = tmp_path / "app"
    for index, relative in enumerate(SCALP_ENGINE_COMPONENTS, start=1):
        root = (
            repository_root
            if relative in SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS
            else package_root
        )
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# component {index}\n", encoding="utf-8")

    identity = production_scalp_engine_identity(
        package_root=package_root,
        repository_root=repository_root,
    )

    assert (
        tuple(path for path, _ in identity.component_sha256) == SCALP_ENGINE_COMPONENTS
    )


def test_engine_identity_changes_when_any_component_changes(tmp_path: Path) -> None:
    _write_engine_tree(tmp_path)
    before = production_scalp_engine_identity(package_root=tmp_path)
    changed = tmp_path / SCALP_ENGINE_COMPONENTS[-1]
    changed.write_text("SEMANTICS = 'changed'\n", encoding="utf-8")

    after = production_scalp_engine_identity(package_root=tmp_path)

    assert before.engine_sha256 != after.engine_sha256


def test_engine_identity_rereads_every_component_on_each_measurement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_engine_tree(tmp_path)
    real_read = engine_identity_module._normalized_source_bytes
    reads: list[Path] = []

    def counted_read(path: Path) -> bytes:
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(
        engine_identity_module,
        "_normalized_source_bytes",
        counted_read,
    )

    production_scalp_engine_identity(package_root=tmp_path)
    production_scalp_engine_identity(package_root=tmp_path)

    assert len(reads) == 2 * len(SCALP_ENGINE_COMPONENTS)
    assert {path.relative_to(tmp_path).as_posix() for path in reads} == set(
        SCALP_ENGINE_COMPONENTS
    )


def test_engine_identity_batches_parent_checks_and_hash_work(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_engine_tree(tmp_path)
    real_parent_batch = engine_identity_module._resolve_component_parent_batch
    real_hash_batch = engine_identity_module._hash_component_batch
    parent_batches: list[int] = []
    hash_batches: list[tuple[str, ...]] = []

    def counted_parent_batch(items: Any) -> Any:
        parent_batches.append(len(items))
        return real_parent_batch(items)

    def counted_hash_batch(items: Any) -> Any:
        hash_batches.append(tuple(item[0] for item in items))
        return real_hash_batch(items)

    monkeypatch.setattr(
        engine_identity_module,
        "_resolve_component_parent_batch",
        counted_parent_batch,
    )
    monkeypatch.setattr(
        engine_identity_module,
        "_hash_component_batch",
        counted_hash_batch,
    )

    production_scalp_engine_identity(package_root=tmp_path)

    assert len(parent_batches) == 2
    assert all(parent_batches)
    assert len(hash_batches) == 2
    assert all(hash_batches)
    assert {item for batch in hash_batches for item in batch} == set(
        SCALP_ENGINE_COMPONENTS
    )


def test_engine_identity_resolves_each_component_directory_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _write_engine_tree(tmp_path)
    resolved: list[Path] = []
    resolve = Path.resolve

    def counted_resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        resolved.append(path)
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", counted_resolve)

    production_scalp_engine_identity(package_root=tmp_path)

    component_paths = {tmp_path / relative for relative in SCALP_ENGINE_COMPONENTS}
    unique_parents = {path.parent for path in component_paths}
    assert component_paths.isdisjoint(resolved)
    assert len(resolved) <= len(unique_parents) + 1


def test_engine_identity_refuses_component_symlink_escape(tmp_path: Path) -> None:
    _write_engine_tree(tmp_path)
    component = tmp_path / SCALP_ENGINE_COMPONENTS[0]
    outside = tmp_path.parent / f"{tmp_path.name}-outside.py"
    outside.write_text("VALUE = 'outside'\n", encoding="utf-8")
    component.unlink()
    try:
        os.symlink(outside, component)
    except OSError as exc:  # pragma: no cover - host policy may forbid symlinks
        pytest.skip(f"host forbids symlink creation: {exc}")

    with pytest.raises(RuntimeError, match="component_missing"):
        production_scalp_engine_identity(package_root=tmp_path)


def test_engine_identity_refuses_missing_component(tmp_path: Path) -> None:
    _write_engine_tree(tmp_path)
    (tmp_path / SCALP_ENGINE_COMPONENTS[0]).unlink()

    with pytest.raises(RuntimeError, match="component_missing"):
        production_scalp_engine_identity(package_root=tmp_path)


def test_runtime_admission_authenticates_complete_mtvclc_release(
    monkeypatch: pytest.MonkeyPatch,
    signed_release: SimpleNamespace,
) -> None:
    real_verify = admission_module.verify_mtvclc_runtime_release
    captured: dict[str, Any] = {}

    def capture(**kwargs: Any):
        captured.update(kwargs)
        return real_verify(**kwargs)

    monkeypatch.setattr(admission_module, "verify_mtvclc_runtime_release", capture)

    result = _admit(signed_release)

    expected_engine = production_scalp_engine_identity(
        package_root=signed_release.package_root,
        repository_root=signed_release.repository_root,
    )
    assert result.valid
    assert result.reason == ""
    assert result.engine_identity == expected_engine
    assert result.verification.authenticated
    assert result.verification.revocation_verified
    assert result.verification.admission_mode == SCALP_ADMISSION_MODE_SIGNED
    assert result.verification.account_mode == "demo"
    assert result.verification.venue_id == "ig_mt4"
    assert result.verification.engine_sha256 == expected_engine.engine_sha256
    assert result.verification.config_sha256 == MTVCLC_CONFIG_SHA256
    assert result.verification.symbol_scope == IG_MT4_SCALP_SYMBOLS
    assert len(result.verification.win_probability_lower_bounds) == 22
    assert (
        sum(
            len(sides)
            for sides in result.verification.win_probability_lower_bounds.values()
        )
        == 44
    )
    assert len(result.cost_calibrations) == 22
    assert tuple(row.symbol for row in result.cost_calibrations) == (
        IG_MT4_SCALP_SYMBOLS
    )
    eurusd = result.cost_calibration_for("eurusd")
    assert eurusd is not None
    assert (
        eurusd.row_sha256()
        == (result.verification.cost_calibration_row_sha256["EURUSD"])
    )
    assert result.cost_calibration_for("XRPUSD") is None
    assert isinstance(captured["release_public_key"], Ed25519PublicKey)
    assert isinstance(captured["evidence_public_key"], Ed25519PublicKey)
    assert captured["release_public_key"] != captured["evidence_public_key"]
    assert captured["expectation"].engine_sha256 == expected_engine.engine_sha256
    assert captured["expectation"].engine_component_sha256 == (
        expected_engine.component_sha256
    )
    assert (
        result.bundle_file_sha256
        == hashlib.sha256(signed_release.bundle_path.read_bytes()).hexdigest()
    )
    assert (
        result.evidence_public_key_file_sha256
        == hashlib.sha256(
            signed_release.evidence_public_key_path.read_bytes()
        ).hexdigest()
    )
    assert (
        result.release_public_key_file_sha256
        == hashlib.sha256(
            signed_release.release_public_key_path.read_bytes()
        ).hexdigest()
    )
    assert result.public_key_file_sha256 == result.release_public_key_file_sha256
    assert result.public_key_path == result.release_public_key_path

    compact = result.to_dict()
    assert compact["verification"]["win_probability_lower_bounds"] == {}
    assert compact["verification"]["cost_calibrations"] == {}
    expected_compact = asdict(result.verification)
    for field_name in (
        "qualification_surface",
        "win_probability_lower_bounds",
        "base_break_even_probabilities",
        "evidence_cell_sha256",
        "cost_calibrations",
    ):
        expected_compact[field_name] = {}
    assert compact["verification"] == expected_compact
    full = result.to_dict(
        include_win_probability_bounds=True,
        include_cost_calibrations=True,
    )
    assert full["verification"] == asdict(result.verification)
    assert len(full["verification"]["win_probability_lower_bounds"]) == 22
    assert len(full["verification"]["cost_calibrations"]) == 22

    cycle_diagnostics = result.to_cycle_diagnostics()
    assert cycle_diagnostics["schema_version"] == (
        admission_module.SCALP_RUNTIME_ADMISSION_DIAGNOSTIC_SCHEMA
    )
    assert (
        cycle_diagnostics["verification"]["runtime_release_certificate_sha256"]
        == result.verification.runtime_release_certificate_sha256
    )
    assert cycle_diagnostics["verification"]["symbol_scope"] == list(
        IG_MT4_SCALP_SYMBOLS
    )
    assert "cost_calibration_row_sha256" not in cycle_diagnostics["verification"]
    assert "evidence_cost_row_sha256" not in cycle_diagnostics["verification"]
    assert "cost_calibrations" not in cycle_diagnostics["verification"]
    assert len(json.dumps(cycle_diagnostics, separators=(",", ":"))) < 4_096


@pytest.mark.parametrize(
    ("mutator", "expected_reason"),
    [
        (lambda _bundle: {}, "mtvclc_runtime_release_bundle_scope_invalid"),
        (
            lambda bundle: {**bundle, "schema_version": "wrong"},
            "mtvclc_runtime_release_bundle_schema_invalid",
        ),
    ],
)
def test_runtime_admission_refuses_ambiguous_bundle_envelopes(
    tmp_path: Path,
    signed_release: SimpleNamespace,
    mutator: Any,
    expected_reason: str,
) -> None:
    payload = mutator(deepcopy(signed_release.bundle))
    tampered_path = tmp_path / "tampered-release.json"
    tampered_path.write_text(json.dumps(payload), encoding="utf-8")
    signed_release.settings.production_scalp_mtvclc_release_bundle = tampered_path

    result = _admit(signed_release)

    assert not result.valid
    assert result.reason == expected_reason
    assert result.verification.win_probability_lower_bounds == {}
    assert result.cost_calibrations == ()


def test_runtime_admission_refuses_outer_release_tampering(
    tmp_path: Path,
    signed_release: SimpleNamespace,
) -> None:
    bundle = deepcopy(signed_release.bundle)
    bundle["certificate"]["execution_contract"]["pending_trades_forbidden"] = False
    tampered_path = tmp_path / "tampered-release.json"
    tampered_path.write_text(json.dumps(bundle), encoding="utf-8")
    signed_release.settings.production_scalp_mtvclc_release_bundle = tampered_path

    result = _admit(signed_release)

    assert not result.valid
    assert result.reason == "mtvclc_runtime_release_bundle_hash_invalid"
    assert not result.verification.authenticated
    assert result.cost_calibrations == ()


@pytest.mark.parametrize(
    ("changes", "expected_reason"),
    [
        (
            {"admission_mode": "direct_demo"},
            "mtvclc_runtime_release_signed_admission_required",
        ),
        (
            {"authenticated": False},
            "mtvclc_runtime_release_unauthenticated",
        ),
        (
            {"revocation_verified": False},
            "mtvclc_runtime_release_registry_unverified",
        ),
    ],
)
def test_runtime_admission_refuses_verifier_security_downgrades(
    monkeypatch: pytest.MonkeyPatch,
    signed_release: SimpleNamespace,
    changes: dict[str, Any],
    expected_reason: str,
) -> None:
    real_verify = admission_module.verify_mtvclc_runtime_release

    def downgraded(**kwargs: Any):
        verified = real_verify(**kwargs)
        assert verified.valid
        return replace(verified, **changes)

    monkeypatch.setattr(
        admission_module,
        "verify_mtvclc_runtime_release",
        downgraded,
    )

    result = _admit(signed_release)

    assert not result.valid
    assert result.reason == expected_reason
    assert result.verification.reason == expected_reason
    assert result.cost_calibrations == ()


def test_runtime_admission_refuses_cost_row_binding_drift(
    monkeypatch: pytest.MonkeyPatch,
    signed_release: SimpleNamespace,
) -> None:
    real_verify = admission_module.verify_mtvclc_runtime_release

    def drifted(**kwargs: Any):
        verified = real_verify(**kwargs)
        assert verified.valid
        row_hashes = dict(verified.cost_calibration_row_sha256)
        row_hashes["EURUSD"] = "0" * 64
        return replace(verified, cost_calibration_row_sha256=row_hashes)

    monkeypatch.setattr(
        admission_module,
        "verify_mtvclc_runtime_release",
        drifted,
    )

    result = _admit(signed_release)

    assert not result.valid
    assert result.reason == "mtvclc_runtime_release_cost_row_binding_invalid"
    assert result.cost_calibrations == ()


def test_runtime_admission_refuses_missing_generation_before_file_access(
    monkeypatch: pytest.MonkeyPatch,
    signed_release: SimpleNamespace,
) -> None:
    settings = _settings(
        signed_release.repository_root,
        production_scalp_generation_id="",
        production_scalp_mtvclc_release_bundle="does-not-exist.json",
        production_scalp_mtvclc_evidence_verify_key_file="does-not-exist-evidence.pub",
        production_scalp_mtvclc_release_verify_key_file="does-not-exist-release.pub",
    )

    def forbidden_read(*_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("release files must not be read")

    monkeypatch.setattr(admission_module, "_read_bounded", forbidden_read)

    result = verify_configured_scalp_runtime_admission(
        settings,
        now_epoch=NOW,
        package_root=signed_release.package_root,
    )

    assert not result.valid
    assert result.reason == "mtvclc_runtime_release_expected_generation_id_missing"


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        (
            "production_scalp_mtvclc_release_bundle",
            "mtvclc_runtime_release_bundle_path_missing",
        ),
        (
            "production_scalp_mtvclc_evidence_verify_key_file",
            "mtvclc_runtime_release_evidence_public_key_path_missing",
        ),
        (
            "production_scalp_mtvclc_release_verify_key_file",
            "mtvclc_runtime_release_public_key_path_missing",
        ),
    ],
)
def test_runtime_admission_requires_all_three_public_release_paths(
    signed_release: SimpleNamespace,
    field: str,
    expected_reason: str,
) -> None:
    setattr(signed_release.settings, field, "")

    result = _admit(signed_release)

    assert not result.valid
    assert result.reason == expected_reason
    assert result.verification.valid is False
    assert result.cost_calibrations == ()


@pytest.mark.parametrize("account_mode", ("demo", "real"))
def test_runtime_native_admission_needs_no_external_release(
    account_mode: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    capture = (
        repository_root
        / "artifacts"
        / "scalp_research"
        / "staging"
        / "ig_tick_cost_snapshot_20260804_post_reload"
        / "ig_mt4_bid_ask_capture.json"
    )
    settings = _settings(
        repository_root,
        live_expected_account_mode=account_mode,
        production_scalp_generation_id="",
        production_scalp_mtvclc_release_bundle="",
        production_scalp_mtvclc_evidence_verify_key_file="",
        production_scalp_mtvclc_release_verify_key_file="",
        production_scalp_cost_capture_file=str(capture),
        production_scalp_cost_capture_sha256=(
            "2c8b1239113dc76b9c1bd1f55da2010a44ebb1f6020cf358ac2110afcecd6190"
        ),
    )

    result = verify_configured_scalp_runtime_admission(
        settings,
        now_epoch=NOW,
    )

    assert result.valid is True
    assert result.reason == ""
    assert result.bundle_path == "runtime-native"
    assert result.verification.generation_id == "mtvclc-runtime-native-v1"
    assert result.verification.account_mode == account_mode
    assert result.verification.expires_at_epoch == 2_147_483_647.0
    assert result.verification.expires_at_epoch > NOW
    assert len(result.verification.deployment_sha256) == 64
    assert result.verification.authority["real_account_authorized"] is (
        account_mode == "real"
    )
    assert len(result.cost_calibrations) == len(IG_MT4_SCALP_SYMBOLS)
    assert set(result.verification.win_probability_lower_bounds) == set(
        IG_MT4_SCALP_SYMBOLS
    )


def test_runtime_native_admission_rereads_capture_and_isolates_cached_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    capture = (
        repository_root
        / "artifacts"
        / "scalp_research"
        / "staging"
        / "ig_tick_cost_snapshot_20260804_post_reload"
        / "ig_mt4_bid_ask_capture.json"
    )
    settings = _settings(
        repository_root,
        live_expected_account_mode="demo",
        production_scalp_generation_id="",
        production_scalp_cost_capture_file=str(capture),
        production_scalp_cost_capture_sha256=(
            "2c8b1239113dc76b9c1bd1f55da2010a44ebb1f6020cf358ac2110afcecd6190"
        ),
    )
    real_load = admission_module.load_runtime_cost_snapshot
    loads = 0

    def counted_load(**kwargs: Any):
        nonlocal loads
        loads += 1
        return real_load(**kwargs)

    monkeypatch.setattr(admission_module, "load_runtime_cost_snapshot", counted_load)

    first = verify_configured_scalp_runtime_admission(settings, now_epoch=NOW)
    first.verification.authority["runtime_authorized"] = False
    first.verification.win_probability_lower_bounds["EURUSD"]["BUY"] = 0.0
    first.verification.cost_calibrations["EURUSD"]["p90_spread_bps"] = 0.0
    second = verify_configured_scalp_runtime_admission(settings, now_epoch=NOW + 1.0)

    assert loads == 2
    assert first is not second
    assert second.verification.authority["runtime_authorized"] is True
    assert second.verification.win_probability_lower_bounds["EURUSD"]["BUY"] > 0.0
    assert second.verification.cost_calibrations["EURUSD"]["p90_spread_bps"] > 0.0
    assert (
        second.verification.qualification_surface["base_break_even_probabilities"]
        is second.verification.base_break_even_probabilities
    )
    assert (
        second.verification.qualification_surface["win_probability_lower_bounds"]
        is second.verification.win_probability_lower_bounds
    )
    assert (
        second.verification.qualification_surface["evidence_cell_sha256"]
        is second.verification.evidence_cell_sha256
    )
    assert (
        second.verification.evidence_cost_row_sha256
        is second.verification.cost_calibration_row_sha256
    )
