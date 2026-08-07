from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from fxstack.backtest.harness.contracts import ScenarioSpec
from fxstack.backtest.harness.nautilus_offline_bundle import (
    REQUIRED_ENGINE_VERSION,
    canonical_json_sha256,
    file_sha256,
)


ENGINE_OUTPUT_SCHEMA = "fxstack_nautilus_raw_engine_output_v1"
BASE_SLIPPAGE_PROBABILITY = 0.05
DEFAULT_ORDER_UNITS = 10_000
FULL_QUOTE_LIQUIDITY_UNITS = 100_000
PARTIAL_QUOTE_LIQUIDITY_UNITS = 2_000
STARTING_BALANCE_USD = 100_000.0


class OfflineEngineError(RuntimeError):
    pass


def _finite_float(value: Any, *, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _stable_fraction(*parts: Any) -> float:
    encoded = "|".join(str(part) for part in parts).encode("utf-8")
    integer = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big")
    return float(integer / ((1 << 64) - 1))


def scenario_execution_parameters(scenario: ScenarioSpec) -> dict[str, Any]:
    spread_multiplier = _finite_float(scenario.spread_multiplier, default=-1.0)
    slippage_multiplier = _finite_float(scenario.slippage_multiplier, default=-1.0)
    latency_ms = _finite_float(scenario.latency_ms, default=-1.0)
    partial_probability = _finite_float(scenario.partial_fill_probability, default=-1.0)
    quote_gap_probability = _finite_float(scenario.quote_gap_probability, default=-1.0)
    cutover_penalty_bps = _finite_float(scenario.session_cutover_penalty_bps, default=-1.0)
    if spread_multiplier <= 0.0 or slippage_multiplier <= 0.0:
        raise OfflineEngineError("spread and slippage multipliers must be positive")
    if latency_ms < 0.0 or cutover_penalty_bps < 0.0:
        raise OfflineEngineError("latency and session penalties must be nonnegative")
    if not 0.0 <= partial_probability <= 1.0:
        raise OfflineEngineError("partial-fill probability must be in [0,1]")
    if not 0.0 <= quote_gap_probability < 1.0:
        raise OfflineEngineError("quote-gap probability must be in [0,1)")
    return {
        "scenario": scenario.to_dict(),
        "quote_transform": {
            "spread_multiplier": spread_multiplier,
            "quote_gap_probability": quote_gap_probability,
            "session_cutover_penalty_bps": cutover_penalty_bps,
            "session_cutover_window_utc": "21:00-21:14",
            "deterministic_hash": "sha256_first_64_bits_v1",
        },
        "fill_model": {
            "class": "nautilus_trader.backtest.models.FillModel",
            "prob_fill_on_limit": 1.0,
            "prob_slippage": min(1.0, BASE_SLIPPAGE_PROBABILITY * slippage_multiplier),
            "random_seed": 73,
        },
        "latency_model": {
            "class": "nautilus_trader.backtest.models.LatencyModel",
            "base_latency_nanos": 1_000_000,
            "insert_latency_nanos": int(round(latency_ms * 1_000_000.0)),
            "update_latency_nanos": 0,
            "cancel_latency_nanos": 0,
        },
        "liquidity_model": {
            "liquidity_consumption": True,
            "partial_fill_probability": partial_probability,
            "full_quote_liquidity_units": FULL_QUOTE_LIQUIDITY_UNITS,
            "partial_quote_liquidity_units": PARTIAL_QUOTE_LIQUIDITY_UNITS,
        },
        "strategy": {
            "order_type": "MARKET",
            "order_units": DEFAULT_ORDER_UNITS,
            "hold_quote_ticks": 4,
            "intent_source": "production_scorer_allowed_only",
        },
    }


def _session_cutover(timestamp: pd.Timestamp) -> bool:
    return int(timestamp.hour) == 21 and int(timestamp.minute) < 15


def _price_points(row: Mapping[str, Any]) -> list[tuple[str, float, float]]:
    bid_open = _finite_float(row.get("bid_open"), default=math.nan)
    bid_close = _finite_float(row.get("bid_close"), default=math.nan)
    ask_open = _finite_float(row.get("ask_open"), default=math.nan)
    ask_close = _finite_float(row.get("ask_close"), default=math.nan)
    required = {
        name: _finite_float(row.get(name), default=math.nan)
        for name in (
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
        )
    }
    if any(not math.isfinite(value) for value in required.values()):
        raise OfflineEngineError("bid/ask bar contains non-finite OHLC values")
    if (bid_close + ask_close) >= (bid_open + ask_open):
        order = ("open", "low", "high", "close")
    else:
        order = ("open", "high", "low", "close")
    points = [
        (suffix, required[f"bid_{suffix}"], required[f"ask_{suffix}"])
        for suffix in order
    ]
    if any(ask < bid for _, bid, ask in points):
        raise OfflineEngineError("bid/ask bar is crossed")
    return points


def build_scenario_quote_records(
    *,
    bars: pd.DataFrame,
    scenario: ScenarioSpec,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if bars.empty:
        raise OfflineEngineError("scenario has no bid/ask bars")
    params = scenario_execution_parameters(scenario)
    transform = dict(params["quote_transform"])
    liquidity = dict(params["liquidity_model"])
    spread_multiplier = float(transform["spread_multiplier"])
    gap_probability = float(transform["quote_gap_probability"])
    cutover_penalty_bps = float(transform["session_cutover_penalty_bps"])
    partial_probability = float(liquidity["partial_fill_probability"])
    records: list[dict[str, Any]] = []
    candidate_count = 0
    gap_count = 0
    partial_liquidity_count = 0
    spread_transform_count = 0
    cutover_transform_count = 0
    ordered = bars.copy()
    ordered["ts"] = pd.to_datetime(ordered["ts"], utc=True, errors="coerce")
    ordered = ordered[ordered["ts"].notna()].sort_values("ts").reset_index(drop=True)
    if ordered.empty:
        raise OfflineEngineError("scenario has no valid bar timestamps")
    offsets_seconds = (0, 75, 150, 240)
    for bar_index, row in ordered.iterrows():
        timestamp = pd.Timestamp(row["ts"])
        for point_index, ((point, raw_bid, raw_ask), offset) in enumerate(
            zip(_price_points(dict(row)), offsets_seconds, strict=True)
        ):
            event_ts = timestamp + pd.Timedelta(seconds=int(offset))
            candidate_count += 1
            if (
                candidate_count > 1
                and gap_probability > 0.0
                and _stable_fraction(scenario.name, "gap", int(event_ts.value), point_index)
                < gap_probability
            ):
                gap_count += 1
                continue
            mid = (raw_bid + raw_ask) / 2.0
            half_spread = max(0.0, (raw_ask - raw_bid) / 2.0) * spread_multiplier
            if spread_multiplier != 1.0:
                spread_transform_count += 1
            if cutover_penalty_bps > 0.0 and _session_cutover(event_ts):
                half_spread += abs(mid) * cutover_penalty_bps / 20_000.0
                cutover_transform_count += 1
            bid = mid - half_spread
            ask = mid + half_spread
            is_partial = (
                partial_probability > 0.0
                and _stable_fraction(scenario.name, "partial", int(event_ts.value), point_index)
                < partial_probability
            )
            quote_size = (
                PARTIAL_QUOTE_LIQUIDITY_UNITS if is_partial else FULL_QUOTE_LIQUIDITY_UNITS
            )
            partial_liquidity_count += int(is_partial)
            records.append(
                {
                    "ts_ns": int(event_ts.value),
                    "ts": event_ts.isoformat(),
                    "bid": round(float(bid), 5),
                    "ask": round(float(ask), 5),
                    "bid_size": int(quote_size),
                    "ask_size": int(quote_size),
                    "bar_index": int(bar_index),
                    "point": point,
                    "source": "causal_bid_ask_bar",
                }
            )
    if not records:
        raise OfflineEngineError("quote-gap transform removed every quote")
    last = dict(records[-1])
    tail_count = max(32, 8)
    for offset in range(1, tail_count + 1):
        event_ts_ns = int(last["ts_ns"]) + (offset * 1_000_000_000)
        records.append(
            {
                **last,
                "ts_ns": event_ts_ns,
                "ts": pd.Timestamp(event_ts_ns, unit="ns", tz="UTC").isoformat(),
                "bar_index": int(last["bar_index"]),
                "point": "close_tail",
                "source": "last_observed_quote_execution_tail",
            }
        )
    exercised = {
        "spread_transform_count": int(spread_transform_count),
        "quote_gap_drop_count": int(gap_count),
        "partial_liquidity_quote_count": int(partial_liquidity_count),
        "session_cutover_transform_count": int(cutover_transform_count),
        "candidate_quote_count": int(candidate_count),
        "engine_quote_count": int(len(records)),
        "execution_tail_quote_count": int(tail_count),
    }
    return records, exercised


def scenario_is_exercised(scenario: ScenarioSpec, counters: Mapping[str, Any]) -> bool:
    name = str(scenario.name)
    if name == "BaseCase":
        return int(counters.get("engine_quote_count") or 0) > 0
    requirements = {
        "WideSpread": "spread_transform_count",
        "QuoteGap": "quote_gap_drop_count",
        "PartialFills": "partial_liquidity_quote_count",
        "SessionCutover": "session_cutover_transform_count",
    }
    if name in requirements:
        return int(counters.get(requirements[name]) or 0) > 0
    if name == "SlippageShock":
        return float(scenario.slippage_multiplier) != 1.0
    if name == "LatencyShock":
        return float(scenario.latency_ms) > 0.0
    return False


def _frame_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    value = frame.copy()
    index_values = [str(item) for item in value.index]
    if "__index__" in value.columns:
        value = value.rename(columns={"__index__": "__source_index__"})
    value.insert(0, "__index__", index_values)
    encoded = value.to_json(
        orient="records",
        date_format="iso",
        date_unit="ns",
        default_handler=str,
    )
    parsed = json.loads(encoded)
    return [dict(item) for item in parsed]


def _write_json_exclusive(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")


def _record_value(record: Mapping[str, Any], names: Sequence[str]) -> Any:
    normalized = {str(key).strip().lower(): value for key, value in record.items()}
    for name in names:
        if str(name).strip().lower() in normalized:
            return normalized[str(name).strip().lower()]
    return None


def _quantity_value(value: Any) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return 0.0
    token = text.split()[0]
    try:
        number = float(token)
    except ValueError:
        return 0.0
    return number if math.isfinite(number) else 0.0


def _timestamp_ns(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = int(value)
        return number if number > 10_000_000_000 else number * 1_000_000_000
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(parsed) else int(pd.Timestamp(parsed).value)


def _metrics_from_ledgers(
    *,
    result: Mapping[str, Any],
    orders: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> dict[str, Any]:
    turnover_units = sum(
        _quantity_value(
            _record_value(fill, ("last_qty", "last_quantity", "quantity", "filled_qty"))
        )
        for fill in fills
    )
    fill_groups: dict[str, int] = {}
    for fill in fills:
        order_id = str(
            _record_value(fill, ("client_order_id", "order_id", "venue_order_id")) or ""
        )
        if order_id:
            fill_groups[order_id] = fill_groups.get(order_id, 0) + 1
    partial_fill_count = sum(max(0, count - 1) for count in fill_groups.values())
    rejected = sum(
        "REJECT" in str(_record_value(order, ("status", "order_status")) or "").upper()
        for order in orders
    )
    order_init: dict[str, int] = {}
    for order in orders:
        order_id = str(
            _record_value(order, ("client_order_id", "order_id", "venue_order_id")) or ""
        )
        timestamp = _timestamp_ns(_record_value(order, ("ts_init", "init_ts", "ts_event")))
        if order_id and timestamp is not None:
            order_init[order_id] = timestamp
    latencies_ms: list[float] = []
    for fill in fills:
        order_id = str(
            _record_value(fill, ("client_order_id", "order_id", "venue_order_id")) or ""
        )
        fill_ts = _timestamp_ns(_record_value(fill, ("ts_event", "event_ts", "ts_init")))
        if order_id in order_init and fill_ts is not None and fill_ts >= order_init[order_id]:
            latencies_ms.append((fill_ts - order_init[order_id]) / 1_000_000.0)
    realized_values = [
        _quantity_value(_record_value(position, ("realized_pnl", "realized_pnl_usd")))
        for position in positions
    ]
    stats_pnls = dict(result.get("stats_pnls") or {})
    usd_stats = dict(stats_pnls.get("USD") or {})
    realized_pnl = 0.0
    preferred = [
        value
        for key, value in usd_stats.items()
        if "pnl" in str(key).lower() and "total" in str(key).lower()
    ]
    if preferred:
        realized_pnl = _finite_float(preferred[0])
    elif realized_values:
        realized_pnl = float(sum(realized_values))
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for pnl in realized_values:
        cumulative += pnl
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    latency_p95 = 0.0
    if latencies_ms:
        latency_p95 = float(pd.Series(latencies_ms, dtype="float64").quantile(0.95))
    return {
        "realized_pnl_usd": float(realized_pnl),
        "unrealized_pnl_usd": 0.0,
        "turnover_lots": float(turnover_units / 100_000.0),
        "max_drawdown_pct": float((max_drawdown / STARTING_BALANCE_USD) * 100.0),
        "margin_utilization_peak": 0.0,
        "trade_count": int(result.get("total_positions") or len(positions)),
        "partial_fill_count": int(partial_fill_count),
        "latency_ms_p95": float(latency_p95),
        "rejection_rate": float(rejected / len(orders)) if orders else 0.0,
        "order_count": int(len(orders)),
        "fill_count": int(len(fills)),
        "position_ledger_count": int(len(positions)),
    }


def _engine_types() -> dict[str, Any]:
    import nautilus_trader
    from nautilus_trader.backtest.engine import BacktestEngine, BacktestEngineConfig
    from nautilus_trader.backtest.models import FillModel, LatencyModel
    from nautilus_trader.common.config import LoggingConfig
    from nautilus_trader.config import StrategyConfig
    from nautilus_trader.model.currencies import EUR, USD
    from nautilus_trader.model.data import QuoteTick
    from nautilus_trader.model.enums import AccountType, OmsType, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Money, Price, Quantity
    from nautilus_trader.trading.strategy import Strategy

    return {
        "nautilus_trader": nautilus_trader,
        "BacktestEngine": BacktestEngine,
        "BacktestEngineConfig": BacktestEngineConfig,
        "FillModel": FillModel,
        "LatencyModel": LatencyModel,
        "LoggingConfig": LoggingConfig,
        "StrategyConfig": StrategyConfig,
        "EUR": EUR,
        "USD": USD,
        "QuoteTick": QuoteTick,
        "AccountType": AccountType,
        "OmsType": OmsType,
        "OrderSide": OrderSide,
        "InstrumentId": InstrumentId,
        "Symbol": Symbol,
        "Venue": Venue,
        "CurrencyPair": CurrencyPair,
        "Money": Money,
        "Price": Price,
        "Quantity": Quantity,
        "Strategy": Strategy,
    }


def _strategy_classes(types: Mapping[str, Any]) -> tuple[type[Any], type[Any]]:
    StrategyConfig = types["StrategyConfig"]
    Strategy = types["Strategy"]
    InstrumentId = types["InstrumentId"]
    OrderSide = types["OrderSide"]

    class IntentReplayConfig(StrategyConfig, frozen=True):
        instrument_id: InstrumentId
        intents: tuple[tuple[int, str], ...]
        order_units: int = DEFAULT_ORDER_UNITS
        hold_quote_ticks: int = 4

    class IntentReplayStrategy(Strategy):
        def __init__(self, config: Any) -> None:
            super().__init__(config)
            self._instrument = None
            self._intent_index = 0
            self._pending_intent: tuple[int, str] | None = None
            self._quote_count = 0
            self._opened_quote_count: int | None = None
            self._inflight = False
            self._closing = False

        def on_start(self) -> None:
            self._instrument = self.cache.instrument(self.config.instrument_id)
            if self._instrument is None:
                raise RuntimeError("replay instrument is missing from Nautilus cache")
            self.subscribe_quote_ticks(self.config.instrument_id)

        def on_quote_tick(self, tick: Any) -> None:
            self._quote_count += 1
            while (
                self._intent_index < len(self.config.intents)
                and int(self.config.intents[self._intent_index][0]) <= int(tick.ts_event)
            ):
                self._pending_intent = self.config.intents[self._intent_index]
                self._intent_index += 1
            if self._closing:
                if self.portfolio.is_flat(self.config.instrument_id):
                    self._closing = False
                    self._opened_quote_count = None
                return
            if not self.portfolio.is_flat(self.config.instrument_id):
                if (
                    not self._inflight
                    and self._opened_quote_count is not None
                    and self._quote_count - self._opened_quote_count
                    >= int(self.config.hold_quote_ticks)
                ):
                    self._closing = True
                    self._inflight = True
                    self.close_all_positions(self.config.instrument_id)
                return
            if self._inflight or self._pending_intent is None:
                return
            _, side = self._pending_intent
            self._pending_intent = None
            order = self.order_factory.market(
                instrument_id=self.config.instrument_id,
                order_side=OrderSide.BUY if str(side).lower() == "long" else OrderSide.SELL,
                quantity=self._instrument.make_qty(int(self.config.order_units)),
            )
            self._inflight = True
            self.submit_order(order)

        def on_order_filled(self, event: Any) -> None:
            self._inflight = False

        def on_order_partially_filled(self, event: Any) -> None:
            self._inflight = False

        def on_order_rejected(self, event: Any) -> None:
            self._inflight = False
            self._closing = False

        def on_order_canceled(self, event: Any) -> None:
            self._inflight = False
            self._closing = False

        def on_order_expired(self, event: Any) -> None:
            self._inflight = False
            self._closing = False

        def on_position_opened(self, event: Any) -> None:
            self._opened_quote_count = self._quote_count

        def on_position_closed(self, event: Any) -> None:
            self._inflight = False
            self._closing = False
            self._opened_quote_count = None

        def on_stop(self) -> None:
            self.unsubscribe_quote_ticks(self.config.instrument_id)

    return IntentReplayConfig, IntentReplayStrategy


def _instrument(types: Mapping[str, Any], *, ts_init: int) -> Any:
    CurrencyPair = types["CurrencyPair"]
    InstrumentId = types["InstrumentId"]
    Symbol = types["Symbol"]
    Venue = types["Venue"]
    Price = types["Price"]
    Quantity = types["Quantity"]
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol("EURUSD"), Venue("SIM")),
        raw_symbol=Symbol("EURUSD"),
        base_currency=types["EUR"],
        quote_currency=types["USD"],
        price_precision=5,
        size_precision=0,
        price_increment=Price(0.00001, 5),
        size_increment=Quantity(1, 0),
        ts_event=int(ts_init),
        ts_init=int(ts_init),
        lot_size=Quantity(100_000, 0),
        min_quantity=Quantity(1_000, 0),
        max_quantity=Quantity(100_000_000, 0),
        margin_init=Decimal("0.0333333333"),
        margin_maint=Decimal("0.0333333333"),
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
    )


def run_nautilus_scenario(
    *,
    bars: pd.DataFrame,
    intents: Sequence[Mapping[str, Any]],
    scenario: ScenarioSpec,
    output_dir: str | Path,
) -> dict[str, Any]:
    target = Path(output_dir).resolve()
    if target.exists():
        raise OfflineEngineError(f"fresh scenario output already exists: {target}")
    target.mkdir(parents=True, exist_ok=False)
    quote_records, exercise_counters = build_scenario_quote_records(
        bars=bars,
        scenario=scenario,
    )
    params = scenario_execution_parameters(scenario)
    types = _engine_types()
    installed_version = str(types["nautilus_trader"].__version__)
    if installed_version != REQUIRED_ENGINE_VERSION:
        raise OfflineEngineError(
            f"Nautilus version mismatch: expected {REQUIRED_ENGINE_VERSION}, got {installed_version}"
        )
    BacktestEngine = types["BacktestEngine"]
    BacktestEngineConfig = types["BacktestEngineConfig"]
    FillModel = types["FillModel"]
    LatencyModel = types["LatencyModel"]
    LoggingConfig = types["LoggingConfig"]
    Venue = types["Venue"]
    OmsType = types["OmsType"]
    AccountType = types["AccountType"]
    Money = types["Money"]
    Price = types["Price"]
    Quantity = types["Quantity"]
    QuoteTick = types["QuoteTick"]
    instrument = _instrument(types, ts_init=int(quote_records[0]["ts_ns"]))
    quote_ticks = [
        QuoteTick(
            instrument_id=instrument.id,
            bid_price=Price(float(record["bid"]), 5),
            ask_price=Price(float(record["ask"]), 5),
            bid_size=Quantity(int(record["bid_size"]), 0),
            ask_size=Quantity(int(record["ask_size"]), 0),
            ts_event=int(record["ts_ns"]),
            ts_init=int(record["ts_ns"]),
        )
        for record in quote_records
    ]
    normalized_intents: tuple[tuple[int, str], ...] = tuple(
        sorted(
            (
                int(pd.Timestamp(pd.to_datetime(intent["ts"], utc=True)).value),
                str(intent["side"]).lower(),
            )
            for intent in intents
            if str(intent.get("side") or "").lower() in {"long", "short"}
        )
    )
    IntentReplayConfig, IntentReplayStrategy = _strategy_classes(types)
    strategy = IntentReplayStrategy(
        IntentReplayConfig(
            instrument_id=instrument.id,
            intents=normalized_intents,
            order_units=int(params["strategy"]["order_units"]),
            hold_quote_ticks=int(params["strategy"]["hold_quote_ticks"]),
        )
    )
    fill_config = dict(params["fill_model"])
    latency_config = dict(params["latency_model"])
    engine = BacktestEngine(
        config=BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
            run_analysis=True,
        )
    )
    try:
        engine.add_venue(
            venue=Venue("SIM"),
            oms_type=OmsType.NETTING,
            account_type=AccountType.MARGIN,
            starting_balances=[Money(STARTING_BALANCE_USD, types["USD"])],
            base_currency=types["USD"],
            default_leverage=Decimal("30"),
            fill_model=FillModel(
                prob_fill_on_limit=float(fill_config["prob_fill_on_limit"]),
                prob_slippage=float(fill_config["prob_slippage"]),
                random_seed=int(fill_config["random_seed"]),
            ),
            latency_model=LatencyModel(
                base_latency_nanos=int(latency_config["base_latency_nanos"]),
                insert_latency_nanos=int(latency_config["insert_latency_nanos"]),
                update_latency_nanos=int(latency_config["update_latency_nanos"]),
                cancel_latency_nanos=int(latency_config["cancel_latency_nanos"]),
            ),
            bar_execution=False,
            trade_execution=False,
            liquidity_consumption=True,
        )
        engine.add_instrument(instrument)
        engine.add_strategy(strategy)
        engine.add_data(quote_ticks)
        engine.run()
        result_obj = engine.get_result()
        result = dataclasses.asdict(result_obj)
        orders = _frame_records(engine.trader.generate_orders_report())
        order_fills = _frame_records(engine.trader.generate_order_fills_report())
        fills = _frame_records(engine.trader.generate_fills_report())
        positions = _frame_records(engine.trader.generate_positions_report())
        account = _frame_records(engine.trader.generate_account_report(venue=Venue("SIM")))
    finally:
        engine.dispose()
    engine_module = importlib.import_module("nautilus_trader.backtest.engine")
    engine_binary = Path(str(engine_module.__file__ or "")).resolve()
    if not engine_binary.is_file():
        raise OfflineEngineError("installed Nautilus engine binary is missing")
    raw_payloads: dict[str, Any] = {
        "quotes.json": quote_records,
        "result.json": result,
        "orders.json": orders,
        "order_fills.json": order_fills,
        "fills.json": fills,
        "positions.json": positions,
        "account.json": account,
    }
    for filename, payload in raw_payloads.items():
        _write_json_exclusive(target / filename, payload)
    inventory = {
        filename: file_sha256(target / filename)
        for filename in sorted(raw_payloads)
    }
    metrics = _metrics_from_ledgers(
        result=result,
        orders=orders,
        fills=fills,
        positions=positions,
    )
    output = {
        "schema_version": ENGINE_OUTPUT_SCHEMA,
        "scenario": str(scenario.name),
        "scenario_parameters": params,
        "scenario_parameters_sha256": canonical_json_sha256(params),
        "scenario_exercise": {
            **exercise_counters,
            "exercised": scenario_is_exercised(scenario, exercise_counters),
        },
        "engine": {
            "package": "nautilus_trader",
            "version": installed_version,
            "class": "nautilus_trader.backtest.engine.BacktestEngine",
            "module_file": engine_binary.name,
            "module_sha256": file_sha256(engine_binary),
            "run_id": str(result.get("run_id") or ""),
            "actual_engine": True,
            "synthetic": False,
            "fxstack_internal_simulator": False,
            "database_configured": False,
            "external_data_clients": 0,
            "external_execution_clients": 0,
        },
        "result_counters": {
            "iterations": int(result.get("iterations") or 0),
            "total_events": int(result.get("total_events") or 0),
            "total_orders": int(result.get("total_orders") or 0),
            "total_positions": int(result.get("total_positions") or 0),
        },
        "economic_metrics": metrics,
        "raw_ledger_inventory": inventory,
        "raw_ledger_payload_sha256": canonical_json_sha256(inventory),
        "input": {
            "causal_bid_ask_bar_count": int(len(bars)),
            "production_intent_count": int(len(normalized_intents)),
            "quote_event_count": int(len(quote_records)),
            "quote_event_sha256": canonical_json_sha256(quote_records),
        },
    }
    _write_json_exclusive(target / "engine_output.json", output)
    return output


def run_real_engine_smoke(output_dir: str | Path) -> dict[str, Any]:
    start = pd.Timestamp("2024-01-02T10:00:00Z")
    rows: list[dict[str, Any]] = []
    for index, mid in enumerate((1.10000, 1.10020, 1.10010)):
        timestamp = start + pd.Timedelta(minutes=5 * index)
        rows.append(
            {
                "pair": "EURUSD",
                "timeframe": "M5",
                "ts": timestamp,
                "bid_open": mid - 0.00005,
                "bid_high": mid + 0.00015,
                "bid_low": mid - 0.00015,
                "bid_close": mid + 0.00005,
                "ask_open": mid + 0.00005,
                "ask_high": mid + 0.00025,
                "ask_low": mid - 0.00005,
                "ask_close": mid + 0.00015,
            }
        )
    return run_nautilus_scenario(
        bars=pd.DataFrame(rows),
        intents=[{"ts": start.isoformat(), "side": "long"}],
        scenario=ScenarioSpec(name="BaseCase"),
        output_dir=output_dir,
    )


__all__ = [
    "ENGINE_OUTPUT_SCHEMA",
    "OfflineEngineError",
    "build_scenario_quote_records",
    "run_nautilus_scenario",
    "run_real_engine_smoke",
    "scenario_execution_parameters",
    "scenario_is_exercised",
]
