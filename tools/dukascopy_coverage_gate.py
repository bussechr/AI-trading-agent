from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


DEFAULT_PAIRS = [
    "EURUSD",
    "USDJPY",
    "GBPUSD",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "EURGBP",
    "EURJPY",
    "NZDUSD",
]
DEFAULT_TIMEFRAMES = ["M1", "M5", "M15", "H4", "D"]
DEFAULT_MIN_ROWS = {
    "M1": 20000,
    "M5": 10000,
    "M15": 4000,
    "H4": 1000,
    "D": 400,
}


def _parse_csv_list(raw: str, *, upper: bool = True) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        item = str(part).strip()
        if not item:
            continue
        out.append(item.upper() if upper else item)
    return out


def _row_count(path: Path) -> int:
    count = 0
    with path.open("r", encoding="utf-8", errors="ignore", newline="") as fh:
        reader = csv.reader(fh)
        # Assume first row is header when present.
        first = True
        for row in reader:
            if not row:
                continue
            if first:
                first = False
                continue
            if any(str(col).strip() for col in row):
                count += 1
    return int(count)


def _resolve_csv_path(source_root: Path, pattern: str, pair: str, timeframe: str) -> Path:
    file_name = str(pattern).format(
        pair=str(pair).upper(),
        granularity=str(timeframe).upper(),
        timeframe=str(timeframe).upper(),
    )
    return source_root / file_name


def run(args: argparse.Namespace) -> int:
    source_root = Path(str(args.source_root)).expanduser()
    pattern = str(args.file_pattern).strip() or "{pair}_{granularity}.csv"
    pairs = _parse_csv_list(str(args.pairs)) or list(DEFAULT_PAIRS)
    timeframes = _parse_csv_list(str(args.timeframes)) or list(DEFAULT_TIMEFRAMES)

    min_rows = {
        "M1": int(args.min_rows_m1),
        "M5": int(args.min_rows_m5),
        "M15": int(args.min_rows_m15),
        "H4": int(args.min_rows_h4),
        "D": int(args.min_rows_d),
    }
    for tf in timeframes:
        if tf not in min_rows:
            min_rows[tf] = int(DEFAULT_MIN_ROWS.get(tf, 1))

    files: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    insufficient: list[dict[str, Any]] = []

    for pair in pairs:
        for timeframe in timeframes:
            path = _resolve_csv_path(source_root, pattern, pair, timeframe)
            record = {
                "pair": pair,
                "timeframe": timeframe,
                "path": str(path),
                "exists": bool(path.exists()),
                "rows": 0,
                "min_rows": int(min_rows.get(timeframe, 1)),
                "ok": False,
            }
            if not path.exists():
                missing.append(
                    {
                        "pair": pair,
                        "timeframe": timeframe,
                        "path": str(path),
                    }
                )
                files.append(record)
                continue

            rows = _row_count(path)
            record["rows"] = int(rows)
            record["ok"] = bool(rows >= int(record["min_rows"]))
            if not bool(record["ok"]):
                insufficient.append(
                    {
                        "pair": pair,
                        "timeframe": timeframe,
                        "path": str(path),
                        "rows": int(rows),
                        "min_rows": int(record["min_rows"]),
                    }
                )
            files.append(record)

    expected_files = len(pairs) * len(timeframes)
    existing_files = len([r for r in files if bool(r.get("exists"))])
    passed = (len(missing) == 0) and (len(insufficient) == 0)

    payload: dict[str, Any] = {
        "meta": {
            "source_root": str(source_root),
            "file_pattern": pattern,
            "pairs": pairs,
            "timeframes": timeframes,
            "min_rows": min_rows,
        },
        "summary": {
            "expected_files": int(expected_files),
            "existing_files": int(existing_files),
            "missing_count": int(len(missing)),
            "insufficient_count": int(len(insufficient)),
            "passed": bool(passed),
        },
        "missing": missing,
        "insufficient": insufficient,
        "files": files,
    }

    print(json.dumps(payload, indent=2, sort_keys=True))
    out_path = str(args.out).strip()
    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    return 0 if bool(passed) else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Validate Dukascopy CSV coverage and minimum row thresholds.")
    ap.add_argument("--source-root", default="fx-quant-stack/data/dukascopy")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    ap.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    ap.add_argument("--file-pattern", default="{pair}_{granularity}.csv")
    ap.add_argument("--min-rows-m1", type=int, default=DEFAULT_MIN_ROWS["M1"])
    ap.add_argument("--min-rows-m5", type=int, default=DEFAULT_MIN_ROWS["M5"])
    ap.add_argument("--min-rows-m15", type=int, default=DEFAULT_MIN_ROWS["M15"])
    ap.add_argument("--min-rows-h4", type=int, default=DEFAULT_MIN_ROWS["H4"])
    ap.add_argument("--min-rows-d", type=int, default=DEFAULT_MIN_ROWS["D"])
    ap.add_argument("--out", default="")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
