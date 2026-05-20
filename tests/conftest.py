"""Pytest setup for the root-level compatibility test suite.

This conftest runs before any test in this directory imports modules from
``fxstack``. It mirrors the auth-required opt-out applied in
``fx-quant-stack/tests/conftest.py`` so that importing ``fxstack.api.app`` in
this suite does not trigger the production fail-secure 503 middleware.
"""

from __future__ import annotations

import os

# Bridge auth defaults to required in production; explicitly opt out for tests.
os.environ.setdefault("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
