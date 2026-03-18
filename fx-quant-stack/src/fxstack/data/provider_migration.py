from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.io.parquet_store import ParquetStore


def _partition_value(path: Path, key: str) -> str:
    prefix = f"{key}="
    for part in path.parts:
        if str(part).startswith(prefix):
            return str(part).split("=", 1)[1].strip().upper()
    return ""


def migrate_provider_partitions(
    *,
    store_root: Path,
    source_provider: str,
    target_provider: str,
    dry_run: bool = True,
    remove_source: bool = False,
) -> dict[str, Any]:
    root = Path(store_root)
    source = str(source_provider).strip().lower()
    target = str(target_provider).strip().lower()
    if not source:
        raise ValueError("source_provider is required")
    if not target:
        raise ValueError("target_provider is required")
    if source == target:
        raise ValueError("source_provider and target_provider must differ")

    src_base = root / f"provider={source}"
    if not src_base.exists():
        return {
            "ok": True,
            "dry_run": bool(dry_run),
            "store_root": str(root),
            "source_provider": source,
            "target_provider": target,
            "source_exists": False,
            "files_scanned": 0,
            "rows_scanned": 0,
            "rows_written": 0,
            "removed_source": False,
        }

    files = sorted(src_base.rglob("*.parquet"))
    rows_scanned = 0
    rows_written = 0
    pairs: set[str] = set()
    timeframes: set[str] = set()
    store = ParquetStore(root)

    for p in files:
        df = pd.read_parquet(p)
        rows_scanned += int(len(df))
        pair = ""
        timeframe = ""
        if not df.empty:
            pair = str(df.iloc[0].get("pair", "")).strip().upper()
            timeframe = str(df.iloc[0].get("timeframe", "")).strip().upper()
        if not pair:
            pair = _partition_value(p, "pair")
        if not timeframe:
            timeframe = _partition_value(p, "timeframe")
        if pair:
            pairs.add(pair)
        if timeframe:
            timeframes.add(timeframe)

        if dry_run or df.empty or not pair or not timeframe:
            continue
        store.write_partitioned(df, provider=target, pair=pair, timeframe=timeframe)
        rows_written += int(len(df))

    removed = False
    if not dry_run and remove_source:
        shutil.rmtree(src_base, ignore_errors=True)
        removed = not src_base.exists()

    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "store_root": str(root),
        "source_provider": source,
        "target_provider": target,
        "source_exists": True,
        "files_scanned": int(len(files)),
        "rows_scanned": int(rows_scanned),
        "rows_written": int(rows_written),
        "pairs": sorted(list(pairs)),
        "timeframes": sorted(list(timeframes)),
        "removed_source": bool(removed),
    }
