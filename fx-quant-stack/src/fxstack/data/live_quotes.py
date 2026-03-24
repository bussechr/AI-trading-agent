from __future__ import annotations

from typing import Any

import requests

from fxstack.live.policy import normalize_spread_bps


def fetch_bridge_ticks(bridge_url: str) -> dict[str, dict[str, Any]]:
    from fxstack.settings import get_settings
    api_key = get_settings().bridge_api_key
    headers = {"X-API-Key": api_key} if api_key else None
    url = f"{bridge_url.rstrip('/')}/v2/market/ticks"
    try:
        r = requests.get(url, headers=headers, timeout=2)
        r.raise_for_status()
        payload = r.json()
        return dict(payload if isinstance(payload, dict) else {})
    except Exception:
        return {}


def fetch_bridge_bars(bridge_url: str, *, symbol: str, timeframe: str, limit: int = 400) -> list[dict[str, Any]]:
    from fxstack.settings import get_settings

    api_key = get_settings().bridge_api_key
    headers = {"X-API-Key": api_key} if api_key else None
    url = f"{bridge_url.rstrip('/')}/v2/market/bars"
    try:
        r = requests.get(
            url,
            params={
                "symbol": str(symbol).upper(),
                "timeframe": str(timeframe).upper(),
                "limit": max(1, min(int(limit), 2000)),
            },
            headers=headers,
            timeout=3,
        )
        r.raise_for_status()
        payload = r.json()
        return list(payload.get("bars") or []) if isinstance(payload, dict) else []
    except Exception:
        return []


def fetch_bridge_ready(bridge_url: str) -> dict[str, Any]:
    from fxstack.settings import get_settings

    api_key = get_settings().bridge_api_key
    headers = {"X-API-Key": api_key} if api_key else None
    url = f"{bridge_url.rstrip('/')}/v2/ready"
    try:
        r = requests.get(url, headers=headers, timeout=2)
        r.raise_for_status()
        payload = r.json()
        return dict(payload if isinstance(payload, dict) else {})
    except Exception:
        return {}


def overlay_execution_quote(features_row: dict[str, Any], tick: dict[str, Any]) -> dict[str, Any]:
    out = dict(features_row)
    bid = float(tick.get("bid", 0.0) or 0.0)
    ask = float(tick.get("ask", 0.0) or 0.0)
    spread_bps, spread_source = normalize_spread_bps(
        tick=dict(tick or {}),
        row=out,
        pair=str(out.get("pair") or out.get("symbol") or ""),
    )
    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(out.get("mid_close", 0.0) or 0.0)
    out["live_bid"] = bid
    out["live_ask"] = ask
    out["live_mid"] = mid
    out["spread_bps"] = float(spread_bps)
    out["spread_unit_source"] = str(spread_source)
    return out
