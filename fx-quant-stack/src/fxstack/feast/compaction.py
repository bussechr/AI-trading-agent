from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fxstack.feast.parquet_adapter import FeastParquetArtifact, build_stable_feast_parquet_outputs, compact_pair_feature_views
from fxstack.feast.repository import feature_repo_root
from fxstack.settings import get_settings


@dataclass(slots=True)
class FeastCompactionResult:
    output_root: Path
    provider: str
    pairs: tuple[str, ...]
    artifacts: list[FeastParquetArtifact]


def compact_feature_repo_for_pair(
    *,
    pair: str,
    feature_root: str | Path,
    output_root: str | Path | None = None,
    timeframes: list[str] | None = None,
) -> dict[str, Any]:
    s = get_settings()
    target_root = Path(output_root or (feature_repo_root() / "offline_store"))
    return compact_pair_feature_views(
        feature_root=feature_root,
        output_root=target_root,
        provider=s.normalized_data_provider,
        pair=str(pair).upper(),
        timeframes=timeframes,
    )


def compact_feature_lake_to_feast(
    *,
    source_root: Path | str,
    output_root: Path | str,
    provider: str | None = None,
    pairs: list[str] | None = None,
) -> FeastCompactionResult:
    s = get_settings()
    provider_value = str(provider or s.normalized_data_provider)
    artifacts = build_stable_feast_parquet_outputs(
        source_root=source_root,
        output_root=output_root,
        provider=provider_value,
        pairs=[str(item).upper() for item in list(pairs or s.pairs)],
    )
    return FeastCompactionResult(
        output_root=Path(output_root),
        provider=provider_value,
        pairs=tuple(sorted({str(item.pair).upper() for item in artifacts})),
        artifacts=artifacts,
    )
