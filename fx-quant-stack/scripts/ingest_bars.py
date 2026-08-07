# AGENT: ROLE: External Dukascopy CSV ingestion CLI.
# AGENT: ISOLATION: help and argument validation run before settings, dataframe, or ingestion imports.
from __future__ import annotations

import argparse


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest Dukascopy CSV bars into parquet partitions")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--granularity", default="M5", choices=["M1", "M5", "M15", "H4", "D"])
    ap.add_argument("--csv-path", default="")
    ap.add_argument("--source-root", default="")
    ap.add_argument("--file-pattern", default="")
    ap.add_argument("--store-root", default="data/raw")
    args = ap.parse_args()

    from fxstack.tasks import ingest_task

    result = ingest_task(
        pair=str(args.pair).upper(),
        granularity=str(args.granularity).upper(),
        store_root=str(args.store_root),
        csv_path=str(args.csv_path),
        source_root=str(args.source_root),
        file_pattern=str(args.file_pattern),
    )
    print(result)


if __name__ == "__main__":
    main()
