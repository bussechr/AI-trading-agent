from __future__ import annotations

import argparse
from pathlib import Path

from fxstack.data.provider_migration import migrate_provider_partitions


def main() -> None:
    ap = argparse.ArgumentParser(description="Migrate parquet partitions between providers")
    ap.add_argument("--store-root", default="data/raw")
    ap.add_argument("--source-provider", default="oanda")
    ap.add_argument("--target-provider", default="dukascopy")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    ap.add_argument("--remove-source", action="store_true")
    args = ap.parse_args()

    dry_run = not bool(args.apply)
    if bool(args.dry_run):
        dry_run = True

    out = migrate_provider_partitions(
        store_root=Path(str(args.store_root)),
        source_provider=str(args.source_provider).strip().lower(),
        target_provider=str(args.target_provider).strip().lower(),
        dry_run=dry_run,
        remove_source=bool(args.remove_source),
    )
    print(out)


if __name__ == "__main__":
    main()
