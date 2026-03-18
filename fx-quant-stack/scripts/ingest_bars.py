from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.data.ingest import ingest_dukascopy_csv
from fxstack.settings import get_settings


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Dukascopy CSV bars into parquet partitions")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--granularity", default="M5", choices=["M1", "M5", "M15", "H4", "D"])
    ap.add_argument("--csv-path", default="")
    ap.add_argument("--source-root", default="")
    ap.add_argument("--file-pattern", default="")
    ap.add_argument("--store-root", default="data/raw")
    args = ap.parse_args()

    s = get_settings()
    pair = str(args.pair).upper()
    granularity = str(args.granularity).upper()

    if str(args.csv_path).strip():
        csv_path = Path(str(args.csv_path)).expanduser()
    else:
        source_root = str(args.source_root or s.dukascopy_source_root).strip()
        pattern = str(args.file_pattern or s.dukascopy_file_pattern).strip() or "{pair}_{granularity}.csv"
        try:
            file_name = pattern.format(
                pair=pair,
                granularity=granularity,
                timeframe=granularity,
            )
        except Exception as exc:
            raise SystemExit(f"invalid file pattern '{pattern}': {exc}")
        csv_path = Path(source_root).expanduser() / file_name
    if not csv_path.exists():
        raise SystemExit(f"CSV source file not found: {csv_path}")

    res = ingest_dukascopy_csv(
        store_root=Path(args.store_root),
        pair=pair,
        timeframe=granularity,
        csv_path=csv_path,
        provider=s.normalized_data_provider,
    )
    print({"pair": res.pair, "timeframe": res.timeframe, "rows": res.rows, "path": res.path, "csv_path": str(csv_path)})


if __name__ == "__main__":
    main()
