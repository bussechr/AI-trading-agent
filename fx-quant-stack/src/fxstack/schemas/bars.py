from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class BarRecord(BaseModel):
    pair: str = Field(min_length=6, max_length=7)
    ts: datetime
    timeframe: str
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    mid_open: float
    mid_high: float
    mid_low: float
    mid_close: float
    spread: float | None = None
    volume: float | None = None
