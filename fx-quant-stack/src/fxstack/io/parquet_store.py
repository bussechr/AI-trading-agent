from __future__ import annotations

from pathlib import Path

import pandas as pd

from fxstack.utils.paths import ensure_dir


class ParquetStore:
    def __init__(self, root: Path) -> None:
        self.root = ensure_dir(Path(root))

    def write_partitioned(self, df: pd.DataFrame, *, provider: str, pair: str, timeframe: str, date_col: str = "date") -> Path:
        out_dir = ensure_dir(self.root / f"provider={provider}" / f"pair={pair}" / f"timeframe={timeframe}")
        if date_col not in df.columns:
            df = df.copy()
            df[date_col] = pd.to_datetime(df["ts"], utc=True).dt.strftime("%Y-%m-%d")
        for day, part in df.groupby(date_col, dropna=False):
            day_str = str(day)
            p = ensure_dir(out_dir / f"date={day_str}") / "bars.parquet"
            if p.exists():
                existing = pd.read_parquet(p)
                merged = pd.concat([existing, part], ignore_index=True).drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")
            else:
                merged = part
            merged.sort_values("ts").to_parquet(p, index=False)
        return out_dir

    def read_pair_timeframe(self, *, provider: str, pair: str, timeframe: str) -> pd.DataFrame:
        base = self.root / f"provider={provider}" / f"pair={pair}" / f"timeframe={timeframe}"
        if not base.exists():
            return pd.DataFrame()
        frames: list[pd.DataFrame] = []
        for p in sorted(base.rglob("*.parquet")):
            frames.append(pd.read_parquet(p))
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")
        return out.sort_values("ts").reset_index(drop=True)
