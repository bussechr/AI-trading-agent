"""Pytest setup for the root-level compatibility test suite.

This conftest runs before any test in this directory imports modules from
``fxstack``. It mirrors the explicit project-root and auth opt-out applied in
``fx-quant-stack/tests/conftest.py`` so collection never depends on ambient
machine configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

# Bridge auth defaults to required in production; explicitly opt out for tests.
os.environ.setdefault("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
os.environ.setdefault("FXSTACK_PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))
