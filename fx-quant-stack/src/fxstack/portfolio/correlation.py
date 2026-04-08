from __future__ import annotations

from dataclasses import asdict, dataclass, field

from fxstack.providers.catalog import infer_instrument_ref


@dataclass(slots=True)
class CorrelationSnapshot:
    symbol: str = ""
    max_abs_corr: float = 0.0
    avg_abs_corr: float = 0.0
    correlated_symbols: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _heuristic_overlap(symbol: str, other: str) -> float:
    left = infer_instrument_ref(symbol)
    right = infer_instrument_ref(other)
    if left.canonical_symbol == right.canonical_symbol:
        return 1.0
    if left.asset_class != right.asset_class:
        return 0.1
    if left.asset_class == "fx":
        shared = {left.base_ccy, left.quote_ccy} & {right.base_ccy, right.quote_ccy}
        if len(shared) == 2:
            return 0.95
        if len(shared) == 1:
            return 0.6
        return 0.15
    if left.asset_class == "crypto":
        if left.quote_ccy and left.quote_ccy == right.quote_ccy:
            return 0.55
        if left.base_ccy and left.base_ccy == right.base_ccy:
            return 0.45
        return 0.2
    return 0.1


def compute_correlation_snapshot(
    *,
    symbol: str,
    active_symbols: list[str],
) -> CorrelationSnapshot:
    symbol_key = str(symbol or "").strip().upper()
    scores: dict[str, float] = {}
    for other in list(active_symbols or []):
        other_key = str(other or "").strip().upper()
        if not other_key or other_key == symbol_key:
            continue
        scores[other_key] = float(_heuristic_overlap(symbol_key, other_key))
    if not scores:
        return CorrelationSnapshot(symbol=symbol_key)
    max_abs_corr = max(abs(float(value)) for value in scores.values())
    avg_abs_corr = sum(abs(float(value)) for value in scores.values()) / max(1, len(scores))
    return CorrelationSnapshot(
        symbol=symbol_key,
        max_abs_corr=float(max_abs_corr),
        avg_abs_corr=float(avg_abs_corr),
        correlated_symbols={str(k): float(v) for k, v in sorted(scores.items())},
    )
