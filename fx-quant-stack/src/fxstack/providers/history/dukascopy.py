from __future__ import annotations

from pathlib import Path

import pandas as pd

from fxstack.data.ingest import load_dukascopy_csv
from fxstack.providers.catalog import enrich_bars_frame, infer_instrument_ref


def load_history_frame(*, csv_path: Path, pair: str, timeframe: str) -> pd.DataFrame:
    instrument = infer_instrument_ref(str(pair).upper(), provider="dukascopy", venue="otc", asset_class="fx")
    frame = load_dukascopy_csv(
        csv_path=Path(csv_path),
        pair=str(instrument.canonical_symbol),
        timeframe=str(timeframe).upper(),
    )
    return enrich_bars_frame(
        frame,
        instrument=instrument,
        provider="dukascopy",
        timeframe=str(timeframe).upper(),
        provenance="dukascopy_csv",
    )
