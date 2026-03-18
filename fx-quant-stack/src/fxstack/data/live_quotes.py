from __future__ import annotations

from typing import Any

import requests


def fetch_bridge_ticks(bridge_url: str) -> dict[str, dict[str, Any]]:
    url = f"{bridge_url.rstrip('/')}/v2/market/ticks"
    r = requests.get(url, timeout=2)
    r.raise_for_status()
    payload = r.json()
    return dict(payload if isinstance(payload, dict) else {})


def overlay_execution_quote(features_row: dict[str, Any], tick: dict[str, Any]) -> dict[str, Any]:
    out = dict(features_row)
    bid = float(tick.get("bid", 0.0) or 0.0)
    ask = float(tick.get("ask", 0.0) or 0.0)
    spread = float(tick.get("spread", 0.0) or 0.0)
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(out.get("mid_close", 0.0) or 0.0)
    out["live_bid"] = bid
    out["live_ask"] = ask
    out["live_mid"] = mid
    out["spread_bps"] = spread
    return out
