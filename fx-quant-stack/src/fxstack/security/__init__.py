"""Security utilities for fxstack (offline-first).

Currently exposes a local, file-backed encrypted secret store for broker
credentials. See :mod:`fxstack.security.secrets`.
"""

from __future__ import annotations

from fxstack.security.egress import (
    EgressPolicyError,
    assert_offline_compose,
    egress_policy_report,
    validate_offline_compose_file,
)
from fxstack.security.secrets import (
    DEFAULT_SECRETS_DIR,
    ENV_SECRET_KEY,
    SecretStore,
    SecretStoreError,
    active_backend,
    generate_key,
)

__all__ = [
    "DEFAULT_SECRETS_DIR",
    "ENV_SECRET_KEY",
    "SecretStore",
    "SecretStoreError",
    "active_backend",
    "generate_key",
    "EgressPolicyError",
    "egress_policy_report",
    "assert_offline_compose",
    "validate_offline_compose_file",
]
