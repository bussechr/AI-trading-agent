"""Immutable host trust policy for live release authority.

The production trust anchor is selected by operating system, never by a
process environment variable.  A deliberately insecure test override exists
only so the parser can be exercised; policy loaded through that override is
always marked ineligible for production authority.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import time
from typing import Any

from fxstack.runtime.release_contract import file_sha256, is_sha256, read_json_object


RELEASE_TRUST_POLICY_SCHEMA = "fxstack_release_trust_policy_v1"
WINDOWS_RELEASE_TRUST_POLICY_PATH = Path(
    r"C:\ProgramData\FXStack\release_trust_policy.json"
)
POSIX_RELEASE_TRUST_POLICY_PATH = Path("/etc/fxstack/release_trust_policy.json")

_PHYSICAL_PROOFS = (
    "least_privilege_db_roles_provisioned",
    "singleton_bridge_consumer_identity_provisioned",
    "terminal_wide_ea_lease_provisioned",
    "poll_ack_consumer_token_provisioned",
    "production_terminal_credential_rotated",
    "research_credentials_absent",
    "physical_boundary_proven",
)
_IDENTITY_FIELDS = (
    "issuer",
    "witness_trust_domain",
    "runtime_trust_domain",
    "runtime_host_id",
    "runtime_principal_id",
    "witness_principal_id",
    "witness_workload_id",
    "witness_purpose",
    "runtime_database_role",
    "research_database_role",
    "bridge_consumer_identity",
    "terminal_ea_lease_scope",
    "poll_ack_consumer_token_scope",
    "bridge_credential_generation_id",
    "installed_package_root",
)
_PINNED_FILES = (
    ("witness_public_key_path", "witness_public_key_sha256"),
    ("build_provenance_public_key_path", "build_provenance_public_key_sha256"),
    ("build_provenance_path", "build_provenance_sha256"),
)


def release_trust_policy_path() -> Path:
    return (
        WINDOWS_RELEASE_TRUST_POLICY_PATH
        if os.name == "nt"
        else POSIX_RELEASE_TRUST_POLICY_PATH
    )


def _is_plain_file(path: Path) -> bool:
    try:
        if not path.is_file() or path.is_symlink():
            return False
        attributes = int(getattr(path.stat(), "st_file_attributes", 0) or 0)
    except OSError:
        return False
    # FILE_ATTRIBUTE_REPARSE_POINT.  Symlink checks alone do not cover Windows
    # junctions and other reparse-backed substitutions.
    return not bool(attributes & 0x400)


def current_runtime_principal_id() -> str:
    """Return the OS workload principal in a stable, policy-comparable form."""

    if os.name != "nt":
        try:
            return f"uid:{os.geteuid()}:gid:{os.getegid()}"
        except AttributeError:
            return ""
    try:
        completed = subprocess.run(
            [r"C:\Windows\System32\whoami.exe", "/user", "/fo", "csv", "/nh"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ""
    if int(completed.returncode) != 0:
        return ""
    line = str(completed.stdout or "").strip().strip("\ufeff")
    # CSV is normally "DOMAIN\\name","S-1-...".  The SID is deliberately
    # selected by shape so localized account names do not matter.
    for token in reversed([item.strip().strip('"') for item in line.split(",")]):
        if token.upper().startswith("S-1-"):
            return token.upper()
    return ""


def _windows_acl_errors(path: Path) -> list[str]:
    if os.name != "nt":
        return []
    try:
        completed = subprocess.run(
            [r"C:\Windows\System32\icacls.exe", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return ["release_trust_policy_acl_unverifiable"]
    if int(completed.returncode) != 0:
        return ["release_trust_policy_acl_unverifiable"]
    acl = str(completed.stdout or "").upper()
    writable_principals = (
        "EVERYONE:",
        "BUILTIN\\USERS:",
        "AUTHENTICATED USERS:",
        "NT AUTHORITY\\INTERACTIVE:",
    )
    writable_rights = ("(F)", "(M)", "(W)", "(WD)", "(AD)")
    for line in acl.splitlines():
        if any(principal in line for principal in writable_principals) and any(
            right in line for right in writable_rights
        ):
            return ["release_trust_policy_acl_user_writable"]
    return []


def _test_override_path() -> Path | None:
    if str(os.environ.get("FXSTACK_ALLOW_INSECURE_TEST_TRUST_POLICY") or "").strip() != "1":
        return None
    text = str(os.environ.get("FXSTACK_TEST_RELEASE_TRUST_POLICY_PATH") or "").strip()
    return Path(text) if text else None


def load_release_trust_policy() -> dict[str, Any]:
    """Read and validate the fixed host trust policy without widening trust."""

    override = _test_override_path()
    path = override if override is not None else release_trust_policy_path()
    production_path = override is None
    errors: list[str] = []
    if not _is_plain_file(path):
        return {
            "schema_version": RELEASE_TRUST_POLICY_SCHEMA,
            "valid": False,
            "production_authority_allowed": False,
            "path": str(path),
            "errors": ["release_trust_policy_unavailable"],
        }
    if production_path:
        errors.extend(_windows_acl_errors(path))
    payload = read_json_object(path)
    if str(payload.get("schema_version") or "") != RELEASE_TRUST_POLICY_SCHEMA:
        errors.append("release_trust_policy_schema_invalid")
    errors.extend(
        f"release_trust_policy_{field}_missing"
        for field in _IDENTITY_FIELDS
        if not str(payload.get(field) or "").strip()
    )
    if str(payload.get("runtime_trust_domain") or "").strip() == str(
        payload.get("witness_trust_domain") or ""
    ).strip():
        errors.append("release_trust_policy_domains_not_separated")
    if str(payload.get("runtime_principal_id") or "").strip().upper() == str(
        payload.get("witness_principal_id") or ""
    ).strip().upper():
        errors.append("release_trust_policy_principals_not_separated")
    if str(payload.get("runtime_database_role") or "").strip() == str(
        payload.get("research_database_role") or ""
    ).strip():
        errors.append("release_trust_policy_database_roles_not_separated")
    observed_principal = current_runtime_principal_id()
    expected_principal = str(payload.get("runtime_principal_id") or "").strip().upper()
    if not observed_principal or observed_principal.upper() != expected_principal:
        errors.append("release_trust_policy_runtime_principal_mismatch")
    for path_field, hash_field in _PINNED_FILES:
        source_text = str(payload.get(path_field) or "").strip()
        expected_hash = str(payload.get(hash_field) or "").strip().lower()
        source = Path(source_text) if source_text else Path()
        if (
            not source_text
            or not source.is_absolute()
            or not _is_plain_file(source)
            or not is_sha256(expected_hash)
            or file_sha256(source) != expected_hash
        ):
            errors.append(f"release_trust_policy_{path_field}_invalid")
    if not production_path:
        errors.append("release_trust_policy_test_override_non_production")
    return {
        **payload,
        "path": str(path.resolve()),
        "file_sha256": file_sha256(path),
        "valid": not errors,
        "production_authority_allowed": bool(production_path and not errors),
        "errors": list(dict.fromkeys(errors)),
    }


def physical_boundary_errors(
    policy: dict[str, Any] | None = None,
    *,
    observed_capabilities: dict[str, Any] | None = None,
) -> list[str]:
    """Return explicit live blockers for unproven physical capabilities.

    Policy booleans are provisioning intent, not evidence that a database
    grant, terminal lease, or credential boundary currently exists.  Callers
    must supply independently observed capabilities from the connected DB and
    bridge/EA handshakes.  Until those probes are implemented and supplied,
    live remains deliberately non-operable.
    """

    trust = dict(policy or load_release_trust_policy())
    details = list(trust.get("errors") or [])
    if trust.get("production_authority_allowed") is not True:
        details.append("release_trust_policy_not_production_authoritative")
    details.extend(
        f"policy_intent_missing:{field}"
        for field in _PHYSICAL_PROOFS
        if trust.get(field) is not True
    )
    observed = dict(observed_capabilities or {})
    if not observed:
        details.extend(
            (
                "database_grants_unobserved",
                "singleton_bridge_consumer_unobserved",
                "terminal_wide_ea_lease_unobserved",
                "poll_ack_consumer_token_unobserved",
                "terminal_credential_rotation_unobserved",
                "research_credential_absence_unobserved",
            )
        )
    else:
        details.extend(
            f"capability_unobserved:{field}"
            for field in _PHYSICAL_PROOFS
            if observed.get(field) is not True
        )
        details.extend(
            f"capability_identity_mismatch:{field}"
            for field in (
                "runtime_database_role",
                "bridge_consumer_identity",
                "terminal_ea_lease_scope",
                "poll_ack_consumer_token_scope",
            )
            if str(observed.get(field) or "").strip()
            != str(trust.get(field) or "").strip()
        )
    if details:
        return [
            "physical_boundary_unproven",
            *[f"physical_boundary:{item}" for item in dict.fromkeys(details)],
        ]
    return []


def observe_physical_capabilities(
    settings: Any,
    *,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read DB and command-channel enforcement state without trusting claims."""

    from fxstack.api.protocol_identity import BRIDGE_PROTOCOL_VERSION

    trust = dict(policy or load_release_trust_policy())
    observed: dict[str, Any] = {
        "runtime_database_role": "",
        "bridge_consumer_identity": "",
        "terminal_ea_lease_scope": "",
        "poll_ack_consumer_token_scope": "",
    }
    try:
        from sqlalchemy import text
        from fxstack.runtime.service import RuntimeService

        service = RuntimeService(database_url=str(settings.database_url))
        engine = service.store.engine
        database_ok = False
        if str(engine.dialect.name).lower() == "postgresql":
            with engine.connect() as conn:
                row = conn.execute(
                    text(
                        "SELECT current_user, rolsuper, rolcreaterole, rolcreatedb "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                ).first()
                runtime_role = str(row[0] if row else "")
                research_role = str(trust.get("research_database_role") or "")
                research_exists = bool(
                    conn.execute(
                        text(
                            "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname = :role)"
                        ),
                        {"role": research_role},
                    ).scalar()
                )
                research_member = bool(
                    research_exists
                    and conn.execute(
                        text("SELECT pg_has_role(current_user, :role, 'MEMBER')"),
                        {"role": research_role},
                    ).scalar()
                )
                database_ok = bool(
                    row
                    and runtime_role == str(trust.get("runtime_database_role") or "")
                    and not bool(row[1])
                    and not bool(row[2])
                    and not bool(row[3])
                    and research_exists
                    and not research_member
                )
                observed["runtime_database_role"] = runtime_role
        state = service.get_state()
        lease = dict(state.get("bridge_consumer_lease") or {})
        now_ts = float(time.time())
        lease_fresh = bool(
            str(lease.get("schema_version") or "")
            == "fxstack_bridge_consumer_lease_v2"
            and str(lease.get("producer_instance_id") or "")
            and str(lease.get("bridge_protocol_version") or "")
            == BRIDGE_PROTOCOL_VERSION
            and float(lease.get("expires_at") or 0.0) > now_ts
        )
        identity = str(lease.get("consumer_identity") or "")
        scope = str(lease.get("terminal_lease_scope") or "")
        generation = str(lease.get("credential_generation_id") or "")
        expected_identity = str(trust.get("bridge_consumer_identity") or "")
        expected_scope = str(trust.get("terminal_ea_lease_scope") or "")
        expected_token_scope = str(trust.get("poll_ack_consumer_token_scope") or "")
        expected_generation = str(trust.get("bridge_credential_generation_id") or "")
        poll_fresh = bool(
            lease_fresh
            and float(lease.get("poll_authenticated_at") or 0.0)
            >= float(lease.get("acquired_at") or 0.0)
        )
        command_token = str(getattr(settings, "bridge_command_token", "") or "")
        api_key = str(getattr(settings, "bridge_api_key", "") or "")
        token_enforced = bool(
            poll_fresh
            and command_token
            and command_token != api_key
            and str(getattr(settings, "bridge_command_token_scope", "") or "")
            == expected_token_scope
        )
        identity_ok = bool(
            lease_fresh
            and identity == expected_identity
            and str(getattr(settings, "bridge_consumer_identity", "") or "")
            == expected_identity
        )
        scope_ok = bool(
            identity_ok
            and scope == expected_scope
            and str(getattr(settings, "bridge_terminal_lease_scope", "") or "")
            == expected_scope
        )
        generation_ok = bool(
            poll_fresh
            and generation == expected_generation
            and str(getattr(settings, "bridge_credential_generation_id", "") or "")
            == expected_generation
        )
        credential_env_names = (
            "FXSTACK_RESEARCH_DATABASE_URL",
            "FXSTACK_RESEARCH_DB_PASSWORD",
            "FXSTACK_RESEARCH_API_KEY",
            "FXSTACK_RESEARCH_TOKEN",
        )
        research_absent = bool(
            database_ok
            and not any(str(os.environ.get(name) or "").strip() for name in credential_env_names)
        )
        observed.update(
            {
                "least_privilege_db_roles_provisioned": database_ok,
                "singleton_bridge_consumer_identity_provisioned": identity_ok,
                "terminal_wide_ea_lease_provisioned": scope_ok,
                "poll_ack_consumer_token_provisioned": token_enforced,
                "production_terminal_credential_rotated": generation_ok,
                "research_credentials_absent": research_absent,
                "runtime_database_role": observed.get("runtime_database_role", ""),
                "bridge_consumer_identity": identity,
                "bridge_producer_instance_id": str(
                    lease.get("producer_instance_id") or ""
                ),
                "terminal_ea_lease_scope": scope,
                "poll_ack_consumer_token_scope": (
                    expected_token_scope if token_enforced else ""
                ),
            }
        )
        observed["physical_boundary_proven"] = all(
            observed.get(field) is True for field in _PHYSICAL_PROOFS[:-1]
        )
    except Exception as exc:
        observed["probe_error"] = type(exc).__name__
    return observed


__all__ = [
    "POSIX_RELEASE_TRUST_POLICY_PATH",
    "RELEASE_TRUST_POLICY_SCHEMA",
    "WINDOWS_RELEASE_TRUST_POLICY_PATH",
    "current_runtime_principal_id",
    "load_release_trust_policy",
    "observe_physical_capabilities",
    "physical_boundary_errors",
    "release_trust_policy_path",
]
