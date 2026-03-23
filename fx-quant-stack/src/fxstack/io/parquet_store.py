from __future__ import annotations

from pathlib import Path
import time

import pandas as pd

from fxstack.utils.paths import ensure_dir


class ParquetStore:
    def __init__(self, root: Path, *, partition_cache_ttl_secs: float = 15.0) -> None:
        self.root = ensure_dir(Path(root))
        self._partition_cache_ttl_secs = max(0.0, float(partition_cache_ttl_secs))
        self._partition_cache: dict[tuple[str, str, str], tuple[float, list[Path]]] = {}

    def _partition_cache_key(self, *, provider: str, pair: str, timeframe: str) -> tuple[str, str, str]:
        return (str(provider), str(pair), str(timeframe))

    def _invalidate_partition_cache(self, *, provider: str, pair: str, timeframe: str) -> None:
        self._partition_cache.pop(self._partition_cache_key(provider=provider, pair=pair, timeframe=timeframe), None)

    def _list_partition_files(self, *, provider: str, pair: str, timeframe: str) -> list[Path]:
        base = self.root / f"provider={provider}" / f"pair={pair}" / f"timeframe={timeframe}"
        if not base.exists():
            return []

        cache_key = self._partition_cache_key(provider=provider, pair=pair, timeframe=timeframe)
        now = time.time()
        cached = self._partition_cache.get(cache_key)
        if cached and (now - cached[0]) <= self._partition_cache_ttl_secs:
            return list(cached[1])

        date_dirs = sorted(
            path for path in base.iterdir() if path.is_dir() and path.name.startswith("date=")
        )
        files = [path / "bars.parquet" for path in date_dirs if (path / "bars.parquet").exists()]
        self._partition_cache[cache_key] = (now, files)
        return list(files)

    @staticmethod
    def _quarantine_corrupt_partition(path: Path) -> Path:
        stamp = int(time.time())
        quarantined = path.with_name(f"{path.stem}.corrupt.{stamp}{path.suffix}")
        try:
            path.replace(quarantined)
        except Exception:
            # Best-effort fallback; if replace fails we still want callers to proceed.
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        return quarantined

    def _read_partition_or_quarantine(self, path: Path) -> pd.DataFrame:
        try:
            return pd.read_parquet(path)
        except Exception:
            self._quarantine_corrupt_partition(path)
            return pd.DataFrame()

    def write_partitioned(self, df: pd.DataFrame, *, provider: str, pair: str, timeframe: str, date_col: str = "date") -> Path:
        out_dir = ensure_dir(self.root / f"provider={provider}" / f"pair={pair}" / f"timeframe={timeframe}")
        if date_col not in df.columns:
            df = df.copy()
            df[date_col] = pd.to_datetime(df["ts"], utc=True).dt.strftime("%Y-%m-%d")
        for day, part in df.groupby(date_col, dropna=False):
            day_str = str(day)
            p = ensure_dir(out_dir / f"date={day_str}") / "bars.parquet"
            if p.exists():
                existing = self._read_partition_or_quarantine(p)
                merged = pd.concat([existing, part], ignore_index=True).drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")
            else:
                merged = part
            merged.sort_values("ts").to_parquet(p, index=False)
        self._invalidate_partition_cache(provider=provider, pair=pair, timeframe=timeframe)
        return out_dir

    def read_pair_timeframe(self, *, provider: str, pair: str, timeframe: str) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for p in self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe):
            df = self._read_partition_or_quarantine(p)
            if not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = out.drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")
        return out.sort_values("ts").reset_index(drop=True)

    def read_latest_row(self, *, provider: str, pair: str, timeframe: str, tail_files: int = 3) -> pd.DataFrame:
        """Read only the latest row without scanning the full partition history."""
        paths = self._list_partition_files(provider=provider, pair=pair, timeframe=timeframe)
        if not paths:
            return pd.DataFrame()

        n_files = max(1, int(tail_files))
        frames: list[pd.DataFrame] = []
        for p in paths[-n_files:]:
            df = self._read_partition_or_quarantine(p)
            if not df.empty:
                frames.append(df.tail(1))
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        out = out.sort_values("ts").tail(1).reset_index(drop=True)
        return out
