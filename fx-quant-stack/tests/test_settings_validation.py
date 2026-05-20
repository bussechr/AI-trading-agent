"""Tests for :meth:`Settings.validate_for_startup`.

Pins the contract that crossfield-invalid config produces a non-empty list
of human-readable errors at startup, so misconfig fails fast instead of
crashing deep in the runtime loop.

Each test sets one or two env vars to express one specific misconfig and
asserts the error message mentions the offending field. The test
deliberately does NOT pin exact error strings (we want to be free to
improve wording) — it pins which field shows up in which message.
"""

from __future__ import annotations

import pytest

from fxstack.settings import Settings


def _make_settings(**env: str) -> Settings:
    """Build a Settings instance with the given env vars and no .env file."""
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_default_settings_validate_clean(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped defaults must produce zero errors.

    If this test ever fails, the defaults shipped with the code base are
    broken — that's a release blocker.
    """
    # The repo conftest sets FXSTACK_BRIDGE_AUTH_REQUIRED=false; that means
    # the empty default api key is acceptable. We need ALLOW_SQLITE for the
    # default database_url to pass (Postgres won't be running in CI).
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    s = Settings(_env_file=None)
    assert s.validate_for_startup() == []


def test_empty_pairs_csv_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_PAIRS", "")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("FXSTACK_PAIRS" in e for e in errors), errors


def test_max_total_below_max_pair_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_MAX_PAIR_POSITIONS", "5")
    monkeypatch.setenv("FXSTACK_MAX_TOTAL_POSITIONS", "2")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("max_total_positions" in e and "max_pair_positions" in e for e in errors), errors


def test_max_pair_positions_zero_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_MAX_PAIR_POSITIONS", "0")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("max_pair_positions" in e for e in errors), errors


def test_max_order_lots_below_min_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_MIN_ORDER_LOTS", "0.1")
    monkeypatch.setenv("FXSTACK_MAX_ORDER_LOTS", "0.05")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("max_order_lots" in e and "min_order_lots" in e for e in errors), errors


def test_default_order_lots_below_min_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_MIN_ORDER_LOTS", "0.5")
    monkeypatch.setenv("FXSTACK_DEFAULT_ORDER_LOTS", "0.1")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("default_order_lots" in e for e in errors), errors


def test_auth_required_with_empty_key_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """The classic misconfig: auth on, key blank — bridge would reject every
    request with 401. Catch it at startup."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_AUTH_REQUIRED", "true")
    monkeypatch.setenv("FXSTACK_BRIDGE_API_KEY", "")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("bridge_auth_required" in e and "bridge_api_key" in e for e in errors), errors


def test_sqlite_url_without_allow_flag_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "sqlite+pysqlite:///./test.db")
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "false")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("sqlite" in e.lower() and "ALLOW_SQLITE" in e for e in errors), errors


def test_empty_database_url_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    assert any("FXSTACK_DATABASE_URL" in e for e in errors), errors


def test_multiple_errors_are_collected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator must collect ALL errors, not bail on the first one,
    so operators fix a broken config in one pass."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", "")
    monkeypatch.setenv("FXSTACK_PAIRS", "")
    monkeypatch.setenv("FXSTACK_MAX_PAIR_POSITIONS", "0")
    s = Settings(_env_file=None)
    errors = s.validate_for_startup()
    # At least three distinct categories of error
    assert len(errors) >= 3, errors


def test_min_trade_prob_out_of_range_caught_by_validator() -> None:
    """The validator catches probability ranges even if a field validator did not.

    This is defense-in-depth: even if a future change drops the pydantic
    Field constraint, the startup validator still catches it.
    """
    # Construct settings normally then mutate the attribute directly to
    # simulate "what if a future code change set this to an invalid value
    # via .copy() or model_construct()?"
    s = Settings(_env_file=None)
    object.__setattr__(s, "min_trade_prob", 1.5)
    errors = s.validate_for_startup()
    assert any("min_trade_prob" in e for e in errors), errors
