from __future__ import annotations

import json
import time
from pathlib import Path

from fxstack.data.ingest import ingest_dukascopy_csv, load_silver_bars
from fxstack.features.build import build_features, leakage_guard
from fxstack.io.parquet_store import ParquetStore
from fxstack.labels.triple_barrier import TripleBarrierConfig, triple_barrier_labels
from fxstack.models.intraday_tcn import IntradayTCN
from fxstack.models.intraday_xgb import IntradayXGB
from fxstack.models.meta_filter import MetaFilterXGB
from fxstack.models.regime_hmm import RegimeHMM
from fxstack.models.swing_transformer import SwingTransformer
from fxstack.models.swing_xgb import SwingXGB
from fxstack.settings import get_settings


def _provider() -> str:
    return get_settings().normalized_data_provider


def _resolve_csv_path(*, pair: str, granularity: str, csv_path: str, source_root: str, file_pattern: str) -> Path:
    s = get_settings()
    if str(csv_path or "").strip():
        return Path(str(csv_path)).expanduser()

    root_txt = str(source_root or s.dukascopy_source_root).strip()
    if not root_txt:
        raise RuntimeError("dukascopy source root is not configured")
    root = Path(root_txt).expanduser()
    pattern = str(file_pattern or s.dukascopy_file_pattern).strip()
    if not pattern:
        pattern = "{pair}_{granularity}.csv"

    try:
        file_name = pattern.format(
            pair=str(pair).upper(),
            granularity=str(granularity).upper(),
            timeframe=str(granularity).upper(),
        )
    except Exception as exc:
        raise RuntimeError(f"invalid dukascopy file pattern '{pattern}': {exc}") from exc
    return root / file_name


def ingest_task(
    *,
    pair: str,
    granularity: str,
    store_root: str,
    csv_path: str = "",
    source_root: str = "",
    file_pattern: str = "",
) -> dict:
    p = _resolve_csv_path(
        pair=pair,
        granularity=granularity,
        csv_path=csv_path,
        source_root=source_root,
        file_pattern=file_pattern,
    )
    if not p.exists():
        raise RuntimeError(f"csv source not found: {p}")
    res = ingest_dukascopy_csv(
        store_root=Path(store_root),
        pair=pair,
        timeframe=granularity,
        csv_path=p,
        provider=_provider(),
    )
    return {
        "pair": res.pair,
        "timeframe": res.timeframe,
        "rows": res.rows,
        "path": res.path,
        "csv_path": str(p),
    }


def build_features_task(*, pair: str, timeframe: str, input_root: str, output_root: str) -> dict:
    bars = load_silver_bars(store_root=Path(input_root), pair=pair, timeframe=timeframe, provider=_provider())
    feats = build_features(bars)
    leakage_guard(feats)
    out = ParquetStore(Path(output_root)).write_partitioned(feats, provider=_provider(), pair=pair, timeframe=timeframe)
    return {"rows": len(feats), "path": str(out)}


def build_labels_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, horizon_bars: int, tp_mult: float, sl_mult: float) -> dict:
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    labels = triple_barrier_labels(
        feats,
        TripleBarrierConfig(horizon_bars=horizon_bars, tp_atr_mult=tp_mult, sl_atr_mult=sl_mult),
    )
    out = ParquetStore(Path(label_root)).write_partitioned(labels, provider=_provider(), pair=pair, timeframe=timeframe)
    return {"rows": len(labels), "path": str(out)}


def _train_xy(*, pair: str, timeframe: str, feature_root: str, label_root: str):
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    labels = ParquetStore(Path(label_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    if feats.empty or labels.empty:
        raise RuntimeError("features or labels are empty")
    feats = feats.sort_values("ts").reset_index(drop=True)
    labels = labels.sort_values("ts").reset_index(drop=True)
    if feats["ts"].duplicated().any():
        raise RuntimeError("feature frame contains duplicated timestamps")
    if labels["ts"].duplicated().any():
        raise RuntimeError("label frame contains duplicated timestamps")
    df = feats.merge(labels[["ts", "label"]], on="ts", how="inner")
    df = df[df["label"].isin([-1, 1])].copy()
    if df.empty:
        raise RuntimeError("no train rows after joining features/labels")
    df["y"] = (df["label"] > 0).astype(int)
    drop = {"pair", "timeframe", "date", "label", "y", "t1_index", "ts"}
    X = df[[c for c in df.columns if c not in drop]]
    y = df["y"]
    return X, y


def _artifact_age_hours(path: Path) -> float | None:
    meta = path / "meta.json"
    if not meta.exists():
        return None
    try:
        payload = json.loads(meta.read_text(encoding="utf-8"))
        created = float(payload.get("created_at", 0.0) or 0.0)
        if created <= 0:
            created = float(meta.stat().st_mtime)
    except Exception:
        created = float(meta.stat().st_mtime)
    return max(0.0, (time.time() - created) / 3600.0)


def _is_stale(path: Path, stale_hours: float) -> tuple[bool, float | None]:
    age = _artifact_age_hours(path)
    if age is None:
        return True, None
    return bool(age > max(0.0, float(stale_hours))), age


def train_regime_task(*, pair: str, timeframe: str, feature_root: str, out: str) -> dict:
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    cols = [c for c in ["ret_1", "ret_5", "vol_20", "vol_60", "trend_slope_20"] if c in feats.columns]
    model = RegimeHMM()
    model.fit(feats[cols])
    model.save(Path(out))
    return {"model": "regime_hmm", "rows": len(feats), "path": out}


def train_swing_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y = _train_xy(pair=pair, timeframe=timeframe, feature_root=feature_root, label_root=label_root)
    model = SwingXGB()
    model.fit(X, y)
    model.save(Path(out))
    return {"model": "swing_xgb", "rows": len(X), "path": out}


def train_intraday_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y = _train_xy(pair=pair, timeframe=timeframe, feature_root=feature_root, label_root=label_root)
    model = IntradayXGB()
    model.fit(X, y)
    model.save(Path(out))
    return {"model": "intraday_xgb", "rows": len(X), "path": out}


def train_swing_transformer_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y = _train_xy(pair=pair, timeframe=timeframe, feature_root=feature_root, label_root=label_root)
    s = get_settings()
    model = SwingTransformer(
        window_size=int(s.transformer_window_size),
        epochs=int(s.deep_train_epochs),
        batch_size=int(s.deep_batch_size),
        require_cuda=bool(s.require_cuda),
    )
    model.fit(X, y)
    model.save(Path(out))
    return {"model": "swing_transformer", "rows": len(X), "path": out}


def train_intraday_tcn_task(*, pair: str, timeframe: str, feature_root: str, label_root: str, out: str) -> dict:
    X, y = _train_xy(pair=pair, timeframe=timeframe, feature_root=feature_root, label_root=label_root)
    s = get_settings()
    model = IntradayTCN(
        window_size=int(s.tcn_window_size),
        epochs=int(s.deep_train_epochs),
        batch_size=int(s.deep_batch_size),
        require_cuda=bool(s.require_cuda),
    )
    model.fit(X, y)
    model.save(Path(out))
    return {"model": "intraday_tcn", "rows": len(X), "path": out}


def train_deep_stale_task(
    *,
    pair: str,
    swing_timeframe: str,
    intraday_timeframe: str,
    feature_root: str,
    label_root: str,
    artifact_root: str,
    stale_hours: float | None = None,
) -> dict:
    s = get_settings()
    stale_cutoff = float(s.deep_model_stale_hours if stale_hours is None else stale_hours)
    pair_root = Path(artifact_root) / str(pair).lower()
    swing_path = pair_root / "swing_transformer"
    intraday_path = pair_root / "intraday_tcn"

    swing_stale, swing_age = _is_stale(swing_path, stale_cutoff)
    intraday_stale, intraday_age = _is_stale(intraday_path, stale_cutoff)

    out: dict[str, dict[str, object]] = {
        "swing_transformer": {
            "stale": bool(swing_stale),
            "age_hours": None if swing_age is None else float(swing_age),
            "path": str(swing_path),
            "action": "skip",
        },
        "intraday_tcn": {
            "stale": bool(intraday_stale),
            "age_hours": None if intraday_age is None else float(intraday_age),
            "path": str(intraday_path),
            "action": "skip",
        },
    }

    if swing_stale:
        out["swing_transformer"] = dict(
            train_swing_transformer_task(
                pair=pair,
                timeframe=str(swing_timeframe).upper(),
                feature_root=feature_root,
                label_root=label_root,
                out=str(swing_path),
            ),
            stale=True,
            action="retrained",
        )
    if intraday_stale:
        out["intraday_tcn"] = dict(
            train_intraday_tcn_task(
                pair=pair,
                timeframe=str(intraday_timeframe).upper(),
                feature_root=feature_root,
                label_root=label_root,
                out=str(intraday_path),
            ),
            stale=True,
            action="retrained",
        )

    return {
        "pair": str(pair).upper(),
        "stale_hours": float(stale_cutoff),
        "result": out,
    }


def train_meta_task(*, pair: str, timeframe: str, feature_root: str, out: str) -> dict:
    feats = ParquetStore(Path(feature_root)).read_pair_timeframe(provider=_provider(), pair=pair, timeframe=timeframe)
    feats = feats.copy()
    feats["y"] = ((feats["ret_1"].astype(float) * 10000.0) - (feats.get("spread", 0.0).astype(float) * 10000.0) > 0).astype(int)
    drop = {"pair", "timeframe", "date", "y", "ts"}
    X = feats[[c for c in feats.columns if c not in drop]]
    y = feats["y"]
    model = MetaFilterXGB()
    model.fit(X, y)
    model.save(Path(out))
    return {"model": "meta_filter", "rows": len(X), "path": out}
