"""Prometheus-compatible metrics exporter for the fxstack bridge.

Exposes a focused set of operational gauges and counters in the Prometheus
text exposition format (v0.0.4). Distinct from ``GET /v2/metrics``, which
returns a fat JSON document for the dashboard — this surface is for
Prometheus / Grafana / alert routing.

The metric set is deliberately small. Add a new metric only when an
operator-facing alert or dashboard needs it; the JSON ``/v2/metrics``
endpoint remains the firehose for ad-hoc inspection.
"""

from __future__ import annotations

from typing import Any, Callable

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION, _build_revision

#: Media type clients should send when scraping the Prometheus endpoint.
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label_value(value: object) -> str:
    """Apply the three escapes Prometheus requires for label values."""
    text = str(value)
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\"", "\\\"")


def _gauge(
    lines: list[str],
    name: str,
    value: float | int | bool,
    *,
    help_text: str,
    labels: dict[str, object] | None = None,
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} gauge")
    label_str = ""
    if labels:
        parts = [f'{k}="{_escape_label_value(v)}"' for k, v in labels.items()]
        label_str = "{" + ",".join(parts) + "}"
    if isinstance(value, bool):
        rendered: float = 1.0 if value else 0.0
    else:
        rendered = float(value)
    lines.append(f"{name}{label_str} {rendered}")


def _counter(
    lines: list[str],
    name: str,
    value: int | float,
    *,
    help_text: str,
) -> None:
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} counter")
    lines.append(f"{name} {float(value)}")


def render_prometheus_metrics(
    *,
    metrics: dict[str, Any],
    state: dict[str, Any],
    health: dict[str, Any],
    open_positions_count: int,
    auth_required: bool,
    basket_tp_pct: float,
) -> str:
    """Render the bridge's operational state as Prometheus exposition text.

    Pure function over a pre-collected snapshot so it's trivial to unit-test.
    """
    lines: list[str] = []

    _gauge(lines, "fxstack_bridge_up", 1, help_text="Bridge process is alive and responding")
    _gauge(
        lines,
        "fxstack_protocol_version_info",
        1,
        help_text="Bridge wire-protocol version and build (label-only)",
        labels={"version": BRIDGE_PROTOCOL_VERSION, "build": _build_revision()},
    )
    _gauge(
        lines,
        "fxstack_auth_required",
        auth_required,
        help_text="Whether the bridge requires X-API-Key (1=required)",
    )
    _gauge(
        lines,
        "fxstack_database_ok",
        bool(health.get("tables_ok")),
        help_text="Database connection healthy and schema verified (1=ok)",
    )
    _gauge(
        lines,
        "fxstack_mt4_ticks_fresh",
        bool(state.get("ticks_fresh", False)),
        help_text="Whether the MT4 tick stream is within its freshness window (1=fresh)",
    )

    hb_age = state.get("heartbeat_age_secs")
    if hb_age is not None:
        _gauge(
            lines,
            "fxstack_mt4_heartbeat_age_seconds",
            float(hb_age),
            help_text="Seconds since the last MT4 heartbeat",
        )

    _counter(
        lines,
        "fxstack_signals_sent_total",
        int(state.get("signals_sent") or 0),
        help_text="Total signals sent since runtime start",
    )
    _counter(
        lines,
        "fxstack_trades_executed_total",
        int(state.get("trades_executed") or 0),
        help_text="Total trades executed since runtime start",
    )

    pending = dict(metrics.get("pending") or {})
    _gauge(
        lines,
        "fxstack_command_queue_pending",
        int(pending.get("count") or 0),
        help_text="Pending (unacked) commands in the queue",
    )

    timeouts = dict(metrics.get("timeouts") or {})
    ack_timeout = timeouts.get("ack_timeout_rate_5m")
    if ack_timeout is not None:
        _gauge(
            lines,
            "fxstack_ack_timeout_rate_5m",
            float(ack_timeout),
            help_text="Ack timeout rate over the last 5 minutes (fraction in [0,1])",
        )

    governance = dict(state.get("governance") or {})
    dd_pct = governance.get("drawdown_pct")
    if dd_pct is not None:
        _gauge(
            lines,
            "fxstack_drawdown_pct",
            float(dd_pct),
            help_text="Current drawdown as a fraction of equity",
        )
    _gauge(
        lines,
        "fxstack_governance_paused",
        bool(governance.get("paused", False)),
        help_text="Whether capital governance has paused trading (1=paused)",
    )

    _gauge(
        lines,
        "fxstack_basket_tp_pct_config",
        float(basket_tp_pct),
        help_text="Configured basket take-profit fraction (Python source of truth)",
    )

    feature_push_backlog = state.get("feature_push_backlog")
    if feature_push_backlog is not None:
        _gauge(
            lines,
            "fxstack_feature_push_backlog",
            int(feature_push_backlog),
            help_text="Feature push outbox backlog count",
        )

    _gauge(
        lines,
        "fxstack_open_positions",
        int(open_positions_count),
        help_text="Open positions known to the runtime/DB",
    )

    return "\n".join(lines) + "\n"


def collect_and_render(
    *,
    service: Any,
    settings_obj: Any,
    state_with_liveness: Callable[[dict[str, Any]], dict[str, Any]],
) -> str:
    """Convenience wrapper used by the bridge's HTTP handler.

    Pulls the snapshot off the live service, applies the bridge's liveness
    overlay, and hands the result to :func:`render_prometheus_metrics`.
    """
    metrics = dict(service.get_metrics() or {})
    state = state_with_liveness(service.get_state())
    health = dict(service.get_health() or {})
    return render_prometheus_metrics(
        metrics=metrics,
        state=state,
        health=health,
        open_positions_count=len(service.get_open_positions() or []),
        auth_required=bool(getattr(settings_obj, "bridge_auth_required", True)),
        basket_tp_pct=float(getattr(settings_obj, "basket_tp_pct", 0.01)),
    )
