"""Security utilities for fxstack (offline-first).

Currently exposes a local, file-backed encrypted secret store for broker
credentials. See :mod:`fxstack.security.secrets`.
"""

from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "DEFAULT_SECRETS_DIR": "fxstack.security.secrets",
    "ENV_SECRET_KEY": "fxstack.security.secrets",
    "EgressPolicyError": "fxstack.security.egress",
    "SecretStore": "fxstack.security.secrets",
    "SecretStoreError": "fxstack.security.secrets",
    "active_backend": "fxstack.security.secrets",
    "assert_offline_compose": "fxstack.security.egress",
    "egress_policy_report": "fxstack.security.egress",
    "generate_key": "fxstack.security.secrets",
    "validate_offline_compose_file": "fxstack.security.egress",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
