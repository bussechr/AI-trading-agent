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

import math

import pytest

from fxstack.settings import Settings


def _make_settings(**env: str) -> Settings:
    """Build a Settings instance with the given env vars and no .env file."""
    return Settings(_env_file=None, **env)  # type: ignore[arg-type]


def test_unknown_fxstack_env_names_are_warned_with_nearest_match() -> None:
    """extra='ignore' validates VALUES but not NAMES: a typo'd variable name
    silently no-ops to the default. The warn layer must flag it and suggest
    the nearest real name, while known aliases and operational vars stay quiet."""

    from fxstack.settings import unknown_fxstack_env_warnings

    warnings = unknown_fxstack_env_warnings(
        {
            "FXSTACK_MIN_ENTRY_PORB": "0.7",  # typo of FXSTACK_MIN_ENTRY_PROB
            "FXSTACK_PYTHON": "python.exe",  # known operational, not a field
            "FXSTACK_PAIRS": "EURUSD",  # declared field alias
            "PATH": "irrelevant",  # non-FXSTACK never flagged
        }
    )

    assert len(warnings) == 1
    assert "FXSTACK_MIN_ENTRY_PORB" in warnings[0]
    assert "FXSTACK_MIN_ENTRY_PROB" in warnings[0]


def test_entry_certification_mode_rejects_unknown_values() -> None:
    s = _make_settings(FXSTACK_ENTRY_CERTIFICATION_MODE="yolo")
    errors = s.validate_for_startup()
    assert any("entry_certification_mode" in e for e in errors)


def test_tail_loss_gate_mode_rejects_unknown_values() -> None:
    s = _make_settings(FXSTACK_CAPITAL_TAIL_LOSS_GATE_MODE="sometimes")
    errors = s.validate_for_startup()
    assert any("capital_tail_loss_gate_mode" in e for e in errors)


def test_tail_loss_enforce_requires_capital_governance_enabled() -> None:
    """enforce binds only through the governance snapshot; enforce with
    governance disabled would be telemetry claiming a control that does not
    exist, so startup refuses the combination."""

    s = _make_settings(
        FXSTACK_CAPITAL_TAIL_LOSS_GATE_MODE="enforce",
        FXSTACK_CAPITAL_GOVERNANCE_ENABLED="0",
    )
    errors = s.validate_for_startup()
    assert any(
        "capital_tail_loss_gate_mode" in e and "capital_governance_enabled" in e
        for e in errors
    )

    ok = _make_settings(
        FXSTACK_CAPITAL_TAIL_LOSS_GATE_MODE="enforce",
        FXSTACK_CAPITAL_GOVERNANCE_ENABLED="1",
    )
    assert not any("capital_tail_loss_gate_mode" in e for e in ok.validate_for_startup())


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


def test_default_risk_limits_are_conservative_positive_and_finite() -> None:
    s = Settings(_env_file=None)

    assert s.equity_lots_per_usd == pytest.approx(0.00001)
    assert s.max_order_lots == pytest.approx(0.10)
    assert s.risk_max_drawdown_pct == pytest.approx(5.0)
    assert s.risk_max_gross_exposure == pytest.approx(0.30)
    assert s.risk_max_net_exposure == pytest.approx(0.20)
    assert s.managed_runner_tp_r_multiple == pytest.approx(0.0)
    assert all(
        math.isfinite(value) and value > 0.0
        for value in (
            s.equity_lots_per_usd,
            s.max_order_lots,
            s.risk_max_drawdown_pct,
            s.risk_max_gross_exposure,
            s.risk_max_net_exposure,
        )
    )


@pytest.mark.parametrize(
    ("env_name", "field_name", "value"),
    [
        ("FXSTACK_MANAGED_RUNNER_TP_R_MULTIPLE", "managed_runner_tp_r_multiple", "0.5"),
    ],
)
def test_managed_runner_control_rejects_unsafe_ranges(
    env_name: str,
    field_name: str,
    value: str,
) -> None:
    settings = _make_settings(**{env_name: value})

    errors = settings.validate_for_startup()

    assert any(field_name in error for error in errors), errors


@pytest.mark.parametrize(
    ("env_name", "field_name", "value"),
    [
        ("FXSTACK_EQUITY_LOTS_PER_USD", "equity_lots_per_usd", "0"),
        ("FXSTACK_MAX_ORDER_LOTS", "max_order_lots", "0"),
        ("FXSTACK_RISK_MAX_DRAWDOWN_PCT", "risk_max_drawdown_pct", "0"),
        ("FXSTACK_RISK_MAX_GROSS_EXPOSURE", "risk_max_gross_exposure", "0"),
        ("FXSTACK_RISK_MAX_NET_EXPOSURE", "risk_max_net_exposure", "nan"),
    ],
)
def test_paper_live_posture_rejects_disabled_or_nonfinite_hard_limits(
    env_name: str,
    field_name: str,
    value: str,
) -> None:
    s = _make_settings(
        FXSTACK_START_PROFILE="paper",
        FXSTACK_AGENT_MODE="paper",
        FXSTACK_DATABASE_URL="sqlite+pysqlite:///./test.db",
        FXSTACK_ALLOW_SQLITE="true",
        **{env_name: value},
    )

    errors = s.validate_for_startup()
    assert any(field_name in error and "paper/live" in error for error in errors), errors


def test_net_exposure_cap_cannot_exceed_gross_cap() -> None:
    s = _make_settings(
        FXSTACK_RISK_MAX_GROSS_EXPOSURE="0.20",
        FXSTACK_RISK_MAX_NET_EXPOSURE="0.30",
    )

    errors = s.validate_for_startup()
    assert any(
        "risk_max_net_exposure" in error and "risk_max_gross_exposure" in error
        for error in errors
    ), errors


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


def test_bridge_url_falls_back_to_windows_host_port_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MT4_BRIDGE_URL", "TRADER_BRIDGE_URL", "BRIDGE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TRADER_BRIDGE_HOST", "127.0.0.1")
    monkeypatch.setenv("TRADER_BRIDGE_PORT", "59991")
    settings = Settings(_env_file=None)
    assert settings.mt4_bridge_url == "http://127.0.0.1:59991"


def test_bridge_auth_accepts_trader_aliases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FXSTACK_BRIDGE_API_KEY", raising=False)
    monkeypatch.delenv("FXSTACK_BRIDGE_AUTH_REQUIRED", raising=False)
    monkeypatch.setenv("TRADER_BRIDGE_API_KEY", "local-test-key")
    monkeypatch.setenv("TRADER_BRIDGE_AUTH_REQUIRED", "true")
    settings = Settings(_env_file=None)
    assert settings.bridge_api_key == "local-test-key"
    assert settings.bridge_auth_required is True


@pytest.mark.parametrize(
    "value",
    ["127.0.0.1:58710", "ftp://127.0.0.1:58710", "http://127.0.0.1", "http://127.0.0.1:58710/v2"],
)
def test_bridge_url_requires_http_base_with_explicit_port(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MT4_BRIDGE_URL", value)
    settings = Settings(_env_file=None)
    errors = settings.validate_for_startup()
    assert any("MT4_BRIDGE_URL" in error for error in errors), errors


def test_enabled_operator_plane_requires_supported_safety_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FXSTACK_MCP_ENABLED", "true")
    monkeypatch.setenv("FXSTACK_MCP_TRANSPORT", "http")
    monkeypatch.setenv("FXSTACK_OPENCLAW_ENABLED", "true")
    monkeypatch.setenv("FXSTACK_OPENCLAW_SANDBOX_REQUIRED", "false")
    errors = Settings(_env_file=None).validate_for_startup()
    assert any("MCP_TRANSPORT=stdio" in error for error in errors), errors
    assert any("OPENCLAW_SANDBOX_REQUIRED" in error for error in errors), errors
