from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import numpy as np

from tools import export_legacy_tick_training_snapshot as exporter


def test_legacy_export_is_read_only_deduplicated_and_hash_bound(tmp_path: Path) -> None:
    database = tmp_path / "ticks.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE market_ticks (id INTEGER PRIMARY KEY, symbol TEXT, "
            "bid REAL, ask REAL, ts REAL, market_source_authenticated INTEGER)"
        )
        rows = []
        row_id = 1
        for symbol in ("EURUSD", "USDJPY"):
            for offset in range(240):
                bid = 1.0 + (offset // 2) * 0.00001
                rows.append((row_id, symbol, bid, bid + 0.00002, 1000.0 + offset, 0))
                row_id += 1
        rows.append((row_id, "EURUSD", 1.2, 1.20002, 2000.0, 1))
        connection.executemany(
            "INSERT INTO market_ticks VALUES (?, ?, ?, ?, ?, ?)", rows
        )
        connection.commit()

    result, start, end = exporter._read_rows(
        database_url=f"sqlite:///{database.as_posix()}",
        symbols=("EURUSD", "USDJPY"),
        lookback_days=1.0,
        max_rows_per_symbol=200,
        max_total_rows=400,
    )
    assert end == 2000.0
    assert start < 1000.0
    assert all(value[0].shape[0] == 120 for value in result.values())

    output = exporter._emit(
        output_dir=tmp_path / "snapshot",
        symbols=("EURUSD", "USDJPY"),
        rows=result,
        requested_start_epoch=start,
        authenticated_boundary_epoch=end,
        created_at_epoch=2100.0,
    )
    payload = json.loads((output / exporter.JSON_FILENAME).read_text("utf-8"))
    body = dict(payload)
    expected = body.pop("payload_sha256")
    assert exporter._sha256_value(body) == expected
    assert payload["legacy_untrusted"] is True
    assert payload["activation_authorized"] is False
    with np.load(output / exporter.NPZ_FILENAME, allow_pickle=False) as arrays:
        assert arrays["sample_epoch"].shape[0] == 240
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM market_ticks").fetchone()[0] == 481
