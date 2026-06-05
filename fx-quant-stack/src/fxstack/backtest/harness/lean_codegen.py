"""Pure codegen: render a runnable QuantConnect Lean (QCAlgorithm) Python module.

A tuned strategy config from the improvement loop (the gate/risk knob dict, see
``fxstack.improve.knobs``) can be exported to a standalone Lean algorithm so the
same thresholds can be replayed under institutional-grade backtesting. This module
contains *no* runtime dependency on the ``lean`` package -- it is string templating
only. The emitted source is deterministic and parses as valid Python via
``ast.parse``.

Public API:

* :func:`render_lean_algorithm` -- return the ``main.py`` source string.
* :func:`render_lean_config` -- return the Lean ``config.json`` payload (dict).
* :func:`write_lean_project` -- write ``main.py`` + ``config.json`` to a directory.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "LeanGateThresholds",
    "render_lean_algorithm",
    "render_lean_config",
    "write_lean_project",
]

# Default algorithm class name. Kept stable so generated output is deterministic.
_DEFAULT_CLASS_NAME = "FxstackGatedAlgorithm"

# Python identifier rule, used to validate/sanitize the class name.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class LeanGateThresholds:
    """Flattened, numeric view of the gate/risk knobs the algorithm enforces.

    These are the values templated into the generated ``OnData`` gate checks. The
    field set mirrors :mod:`fxstack.improve.knobs` so a loop-tuned config round-trips
    without loss.
    """

    min_swing_prob: float = 0.58
    min_entry_prob: float = 0.62
    min_trade_prob: float = 0.60
    min_expected_edge_bps: float = 3.0
    min_expected_edge_rescue_margin_bps: float = 0.5
    max_allowed_spread_bps: float = 3.0
    slippage_bps: float = 0.25
    max_total_positions: int = 6
    max_pair_positions: int = 1
    default_order_lots: float = 0.10
    max_pair_exposure: float = 0.02
    max_total_exposure: float = 0.06
    max_realized_corr_share: float = 0.75

    def to_dict(self) -> dict[str, float | int]:
        return {
            "min_swing_prob": float(self.min_swing_prob),
            "min_entry_prob": float(self.min_entry_prob),
            "min_trade_prob": float(self.min_trade_prob),
            "min_expected_edge_bps": float(self.min_expected_edge_bps),
            "min_expected_edge_rescue_margin_bps": float(
                self.min_expected_edge_rescue_margin_bps
            ),
            "max_allowed_spread_bps": float(self.max_allowed_spread_bps),
            "slippage_bps": float(self.slippage_bps),
            "max_total_positions": int(self.max_total_positions),
            "max_pair_positions": int(self.max_pair_positions),
            "default_order_lots": float(self.default_order_lots),
            "max_pair_exposure": float(self.max_pair_exposure),
            "max_total_exposure": float(self.max_total_exposure),
            "max_realized_corr_share": float(self.max_realized_corr_share),
        }


def _get_path(config: dict[str, Any], path: tuple[str, ...], default: Any) -> Any:
    node: Any = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def thresholds_from_config(config: dict[str, Any] | None) -> LeanGateThresholds:
    """Project a loop config dict onto :class:`LeanGateThresholds`.

    Missing sections/keys fall back to the dataclass defaults, so a partial config
    (or ``None``) still yields a valid, fully-populated threshold set.
    """

    cfg = dict(config or {})
    d = LeanGateThresholds()
    return LeanGateThresholds(
        min_swing_prob=_coerce_float(
            _get_path(cfg, ("gates", "min_swing_prob"), d.min_swing_prob), d.min_swing_prob
        ),
        min_entry_prob=_coerce_float(
            _get_path(cfg, ("gates", "min_entry_prob"), d.min_entry_prob), d.min_entry_prob
        ),
        min_trade_prob=_coerce_float(
            _get_path(cfg, ("gates", "min_trade_prob"), d.min_trade_prob), d.min_trade_prob
        ),
        min_expected_edge_bps=_coerce_float(
            _get_path(cfg, ("gates", "min_expected_edge_bps"), d.min_expected_edge_bps),
            d.min_expected_edge_bps,
        ),
        min_expected_edge_rescue_margin_bps=_coerce_float(
            _get_path(
                cfg,
                ("gates", "min_expected_edge_rescue_margin_bps"),
                d.min_expected_edge_rescue_margin_bps,
            ),
            d.min_expected_edge_rescue_margin_bps,
        ),
        max_allowed_spread_bps=_coerce_float(
            _get_path(cfg, ("gates", "max_allowed_spread_bps"), d.max_allowed_spread_bps),
            d.max_allowed_spread_bps,
        ),
        slippage_bps=_coerce_float(
            _get_path(cfg, ("cost_model", "slippage_bps"), d.slippage_bps), d.slippage_bps
        ),
        max_total_positions=_coerce_int(
            _get_path(cfg, ("risk", "max_total_positions"), d.max_total_positions),
            d.max_total_positions,
        ),
        max_pair_positions=_coerce_int(
            _get_path(cfg, ("risk", "max_pair_positions"), d.max_pair_positions),
            d.max_pair_positions,
        ),
        default_order_lots=_coerce_float(
            _get_path(cfg, ("risk", "default_order_lots"), d.default_order_lots),
            d.default_order_lots,
        ),
        max_pair_exposure=_coerce_float(
            _get_path(cfg, ("risk", "max_pair_exposure"), d.max_pair_exposure),
            d.max_pair_exposure,
        ),
        max_total_exposure=_coerce_float(
            _get_path(cfg, ("risk", "max_total_exposure"), d.max_total_exposure),
            d.max_total_exposure,
        ),
        max_realized_corr_share=_coerce_float(
            _get_path(
                cfg, ("portfolio", "max_realized_corr_share"), d.max_realized_corr_share
            ),
            d.max_realized_corr_share,
        ),
    )


def _normalize_pairs(pairs: Any) -> list[str]:
    if isinstance(pairs, str):
        raw = [pairs]
    else:
        raw = list(pairs or [])
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        sym = re.sub(r"[^A-Za-z]", "", str(item)).upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    if not out:
        raise ValueError("at least one valid FX pair is required")
    return out


def _normalize_date(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    # Accept YYYY-MM-DD or YYYYMMDD.
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unparseable date: {value!r}")


def _sanitize_class_name(name: str | None) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        return _DEFAULT_CLASS_NAME
    # Keep only identifier-safe chars; ensure it doesn't start with a digit.
    cleaned = "".join(ch for ch in candidate if ch.isalnum() or ch == "_")
    if not cleaned or cleaned[0].isdigit():
        cleaned = f"Fxstack{cleaned}"
    if not _IDENTIFIER_RE.fullmatch(cleaned):
        return _DEFAULT_CLASS_NAME
    return cleaned


def render_lean_config(
    config: dict[str, Any] | None = None,
    *,
    pairs: Any,
    start: str | date | datetime,
    end: str | date | datetime,
    cash: float = 100_000,
    algorithm_name: str = "main",
    class_name: str | None = None,
) -> dict[str, Any]:
    """Build the Lean ``config.json`` payload as a plain dict.

    The payload references ``main.py`` as the algorithm location and embeds the
    thresholds under ``parameters`` so the run is fully self-describing.
    """

    thresholds = thresholds_from_config(config)
    pair_list = _normalize_pairs(pairs)
    start_d = _normalize_date(start)
    end_d = _normalize_date(end)
    if end_d < start_d:
        raise ValueError("end date must not precede start date")

    cls = _sanitize_class_name(class_name)
    parameters: dict[str, str] = {
        "pairs": ",".join(pair_list),
        "start-date": start_d.strftime("%Y-%m-%d"),
        "end-date": end_d.strftime("%Y-%m-%d"),
        "starting-cash": repr(float(cash)),
    }
    for key, value in thresholds.to_dict().items():
        parameters[key.replace("_", "-")] = repr(value)

    return {
        "algorithm-language": "Python",
        "algorithm-type-name": cls,
        "algorithm-location": f"{algorithm_name}.py",
        "data-folder": "data",
        "parameters": parameters,
    }


def _format_pairs_literal(pairs: list[str]) -> str:
    inner = ", ".join(repr(p) for p in pairs)
    return f"[{inner}]"


def render_lean_algorithm(
    config: dict[str, Any] | None = None,
    *,
    pairs: Any,
    start: str | date | datetime,
    end: str | date | datetime,
    cash: float = 100_000,
    class_name: str | None = None,
) -> str:
    """Render a runnable ``QCAlgorithm`` (Lean) Python source string.

    The generated class subclasses ``QCAlgorithm`` with ``Initialize`` (cash, dates,
    pair subscriptions) and ``OnData`` (applies the configured gate thresholds before
    placing market orders). The emitted source is deterministic and parses cleanly
    under :func:`ast.parse`. No network or filesystem access occurs at generation or
    at the (templated) Lean-runtime import boundary.

    Parameters
    ----------
    config:
        The improvement-loop config dict (``gates``/``risk``/``cost_model``/
        ``portfolio`` sections). ``None`` yields the default thresholds.
    pairs:
        One symbol string or an iterable of FX pair symbols (e.g. ``["EURUSD"]``).
    start, end:
        Backtest date range; ``str`` (``YYYY-MM-DD``), :class:`date`, or
        :class:`datetime`.
    cash:
        Starting cash in account currency.
    class_name:
        Optional algorithm class name; sanitized to a valid identifier.
    """

    thresholds = thresholds_from_config(config)
    pair_list = _normalize_pairs(pairs)
    start_d = _normalize_date(start)
    end_d = _normalize_date(end)
    if end_d < start_d:
        raise ValueError("end date must not precede start date")

    cls = _sanitize_class_name(class_name)
    t = thresholds
    pairs_literal = _format_pairs_literal(pair_list)

    # The threshold constants are emitted as a class-level dict so they appear
    # verbatim in the source (tests assert their presence) and drive the gate logic.
    thresholds_block_lines = [
        f"        {key!r}: {repr(val)},"
        for key, val in t.to_dict().items()
    ]
    thresholds_block = "\n".join(thresholds_block_lines)

    source = f'''"""Auto-generated QuantConnect Lean algorithm (fxstack codegen -- do not edit).

This file is produced by ``fxstack.backtest.harness.lean_codegen``. It enforces the
gate/risk thresholds of a tuned strategy config inside a Lean ``QCAlgorithm``.
"""

from datetime import datetime

from AlgorithmImports import *  # noqa: F401,F403  (provided by the Lean runtime)


class {cls}(QCAlgorithm):
    """Lean port of an fxstack gate-driven FX strategy."""

    PAIRS = {pairs_literal}
    START_DATE = datetime({start_d.year}, {start_d.month}, {start_d.day})
    END_DATE = datetime({end_d.year}, {end_d.month}, {end_d.day})
    STARTING_CASH = {float(cash)!r}

    THRESHOLDS = {{
{thresholds_block}
    }}

    def Initialize(self):
        self.SetStartDate(self.START_DATE.year, self.START_DATE.month, self.START_DATE.day)
        self.SetEndDate(self.END_DATE.year, self.END_DATE.month, self.END_DATE.day)
        self.SetCash(self.STARTING_CASH)
        self._symbols = {{}}
        self._pair_positions = {{}}
        for ticker in self.PAIRS:
            security = self.AddForex(ticker, Resolution.Minute, Market.Oanda)
            self._symbols[ticker] = security.Symbol
            self._pair_positions[ticker] = 0

    def _spread_bps(self, data, symbol):
        if not data.ContainsKey(symbol):
            return None
        quote = data[symbol]
        bid = float(getattr(quote, "Bid", 0.0) or 0.0)
        ask = float(getattr(quote, "Ask", 0.0) or 0.0)
        mid = (bid + ask) / 2.0
        if mid <= 0.0:
            return None
        return abs(ask - bid) / mid * 1.0e4

    def _signal(self, symbol):
        """Placeholder signal hook.

        Wire model probabilities/edge here. Defaults keep the gates closed so the
        generated algorithm never trades on synthetic data without an explicit edge.
        """
        return {{
            "swing_prob": 0.0,
            "entry_prob": 0.0,
            "trade_prob": 0.0,
            "expected_edge_bps": 0.0,
            "direction": 0,
        }}

    def _total_open_positions(self):
        return sum(1 for count in self._pair_positions.values() if count > 0)

    def _passes_gates(self, signal, spread_bps, ticker):
        thr = self.THRESHOLDS
        if signal["swing_prob"] < thr["min_swing_prob"]:
            return False
        if signal["entry_prob"] < thr["min_entry_prob"]:
            return False
        if signal["trade_prob"] < thr["min_trade_prob"]:
            return False
        edge_net = signal["expected_edge_bps"] - thr["slippage_bps"]
        hurdle = thr["min_expected_edge_bps"] - thr["min_expected_edge_rescue_margin_bps"]
        if edge_net < hurdle:
            return False
        if spread_bps is None or spread_bps > thr["max_allowed_spread_bps"]:
            return False
        if self._pair_positions.get(ticker, 0) >= thr["max_pair_positions"]:
            return False
        if self._total_open_positions() >= thr["max_total_positions"]:
            return False
        return True

    def OnData(self, data):
        thr = self.THRESHOLDS
        for ticker, symbol in self._symbols.items():
            spread_bps = self._spread_bps(data, symbol)
            signal = self._signal(symbol)
            if not self._passes_gates(signal, spread_bps, ticker):
                continue
            direction = 1 if signal["direction"] >= 0 else -1
            quantity = direction * thr["default_order_lots"] * 100000.0
            self.MarketOrder(symbol, quantity)
            self._pair_positions[ticker] = self._pair_positions.get(ticker, 0) + 1
'''
    return source


def write_lean_project(
    config: dict[str, Any] | None = None,
    out_dir: str | Path = ".",
    *,
    pairs: Any,
    start: str | date | datetime,
    end: str | date | datetime,
    cash: float = 100_000,
    class_name: str | None = None,
    algorithm_filename: str = "main.py",
) -> dict[str, str]:
    """Write ``main.py`` + ``config.json`` into ``out_dir`` and return their paths.

    The directory is created if needed. Returns a mapping with ``"main"`` and
    ``"config"`` absolute paths. No network access; writes are local only.
    """

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    stem = Path(algorithm_filename).stem or "main"
    source = render_lean_algorithm(
        config,
        pairs=pairs,
        start=start,
        end=end,
        cash=cash,
        class_name=class_name,
    )
    lean_config = render_lean_config(
        config,
        pairs=pairs,
        start=start,
        end=end,
        cash=cash,
        algorithm_name=stem,
        class_name=class_name,
    )

    main_path = out / f"{stem}.py"
    config_path = out / "config.json"
    main_path.write_text(source, encoding="utf-8")
    config_path.write_text(
        json.dumps(lean_config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return {"main": str(main_path.resolve()), "config": str(config_path.resolve())}
