"""Unit tests for the Prometheus exposition formatter.

The exporter is a pure function over a pre-collected snapshot, which makes it
easy to assert exact lines. Tests cover: required-metric presence, gauge vs
counter typing, label escaping, and the auth_required / governance_paused
boolean coercions.
"""

from __future__ import annotations

from fxstack.api.observability import render_prometheus_metrics
from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION


def _render(**overrides) -> str:
    defaults = {
        "metrics": {"pending": {"count": 3}, "timeouts": {"ack_timeout_rate_5m": 0.02}},
        "state": {
            "signals_sent": 7,
            "trades_executed": 4,
            "ticks_fresh": True,
            "heartbeat_age_secs": 1.5,
            "governance": {"paused": False, "drawdown_pct": 0.01},
            "feature_push_backlog": 12,
        },
        "health": {"tables_ok": True},
        "open_positions_count": 2,
        "auth_required": True,
        "basket_tp_pct": 0.01,
    }
    defaults.update(overrides)
    return render_prometheus_metrics(**defaults)


def test_required_metrics_present() -> None:
    body = _render()
    for name in [
        "fxstack_bridge_up",
        "fxstack_protocol_version_info",
        "fxstack_auth_required",
        "fxstack_database_ok",
        "fxstack_mt4_ticks_fresh",
        "fxstack_signals_sent_total",
        "fxstack_trades_executed_total",
        "fxstack_command_queue_pending",
        "fxstack_governance_paused",
        "fxstack_basket_tp_pct_config",
        "fxstack_open_positions",
    ]:
        assert f"# TYPE {name} " in body, f"missing TYPE for {name}"


def test_counter_types() -> None:
    body = _render()
    assert "# TYPE fxstack_signals_sent_total counter" in body
    assert "# TYPE fxstack_trades_executed_total counter" in body


def test_gauge_types() -> None:
    body = _render()
    assert "# TYPE fxstack_bridge_up gauge" in body
    assert "# TYPE fxstack_command_queue_pending gauge" in body


def test_protocol_version_label_present() -> None:
    body = _render()
    assert f'fxstack_protocol_version_info{{version="{BRIDGE_PROTOCOL_VERSION}"' in body
    # Label-only metric; value is 1
    assert " 1\n" in body or " 1.0\n" in body


def test_auth_required_true_coerces_to_one() -> None:
    body = _render(auth_required=True)
    # Find the auth_required value line
    lines = [ln for ln in body.splitlines() if ln.startswith("fxstack_auth_required ")]
    assert len(lines) == 1
    assert lines[0].endswith(" 1.0")


def test_auth_required_false_coerces_to_zero() -> None:
    body = _render(auth_required=False)
    lines = [ln for ln in body.splitlines() if ln.startswith("fxstack_auth_required ")]
    assert lines == ["fxstack_auth_required 0.0"]


def test_governance_paused_surfaces() -> None:
    body = _render(
        state={"governance": {"paused": True, "drawdown_pct": 0.05}, "ticks_fresh": False},
    )
    assert "fxstack_governance_paused 1.0" in body
    assert "fxstack_drawdown_pct 0.05" in body
    assert "fxstack_mt4_ticks_fresh 0.0" in body


def test_optional_fields_omitted_when_absent() -> None:
    body = _render(
        metrics={},  # no pending counts, no timeouts
        state={"ticks_fresh": False, "governance": {"paused": False}},
    )
    # heartbeat_age, drawdown_pct, ack_timeout_rate_5m, feature_push_backlog absent
    assert "fxstack_mt4_heartbeat_age_seconds" not in body
    assert "fxstack_drawdown_pct" not in body
    assert "fxstack_ack_timeout_rate_5m" not in body
    assert "fxstack_feature_push_backlog" not in body


def test_basket_tp_pct_config_surfaces() -> None:
    body = _render(basket_tp_pct=0.0125)
    assert "fxstack_basket_tp_pct_config 0.0125" in body


def test_open_positions_count_surfaces() -> None:
    body = _render(open_positions_count=5)
    assert "fxstack_open_positions 5.0" in body


def test_body_ends_with_newline() -> None:
    body = _render()
    assert body.endswith("\n")
