from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pandas as pd

from .portfolio import PortfolioSnapshot, PositionLedger


def normalize_ledger_rows(ledgers: list[PositionLedger] | list[dict[str, Any]]) -> pd.DataFrame:
    rows = [asdict(ledger) if hasattr(ledger, "__dataclass_fields__") else dict(ledger) for ledger in list(ledgers or [])]
    if not rows:
        return pd.DataFrame(columns=["pair", "side", "open_lots", "realized_pnl_usd", "unrealized_pnl_usd"])
    df = pd.DataFrame(rows)
    preferred = [
        "pair",
        "side",
        "open_lots",
        "entry_price",
        "realized_pnl_usd",
        "unrealized_pnl_usd",
        "campaign_state",
        "campaign_reason",
        "partial_close_count",
    ]
    cols = [col for col in preferred if col in df.columns] + [col for col in df.columns if col not in preferred]
    return df[cols].copy()


def build_ledger_report(
    snapshot: PortfolioSnapshot | None = None,
    *,
    ledgers: list[PositionLedger] | list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    df = normalize_ledger_rows(list(ledgers or (snapshot.ledgers if snapshot is not None else [])))
    report = {
        "summary": {
            "equity_usd": float(snapshot.equity_usd if snapshot is not None else 0.0),
            "open_positions": int(snapshot.open_positions if snapshot is not None else len(df[df.get("open_lots", 0) > 0]) if not df.empty else 0),
            "gross_exposure_lots": float(snapshot.gross_exposure_lots if snapshot is not None else float(df.get("open_lots", pd.Series(dtype=float)).abs().sum())),
            "net_exposure_lots": float(snapshot.net_exposure_lots if snapshot is not None else float(df.get("open_lots", pd.Series(dtype=float)).sum())),
            "realized_pnl_usd": float(snapshot.realized_pnl_usd if snapshot is not None else float(df.get("realized_pnl_usd", pd.Series(dtype=float)).sum())),
            "unrealized_pnl_usd": float(snapshot.unrealized_pnl_usd if snapshot is not None else float(df.get("unrealized_pnl_usd", pd.Series(dtype=float)).sum())),
        },
        "rows": df.to_dict(orient="records"),
        "meta": dict(meta or {}),
    }
    return report

