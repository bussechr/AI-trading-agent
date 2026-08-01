"""The scalper decision loop -- a standalone process against the bridge API.

Runs at tick cadence (default 1s poll), decides on M1 bar close, and writes
every decision to the ledger. Talks HTTP to the existing bridge exactly like
any other consumer; imports nothing from the runner or the ceremony layers.

Conjunctive per-bar pipeline (first non-empty reason wins, all recorded):

    bar_invalid -> daily_breaker -> cooldown -> book -> session -> sentinel
                -> signal -> fresh-entry-quote -> sizing -> open shadow position

Honesty rules carried here (adversarial review 2026-08-01):
- Ticks without a parseable broker timestamp are DROPPED, never stamped with
  wall-clock time -- a frozen feed must look frozen.
- Entries fill at the CURRENT fresh quote (adverse of bar-close vs latest),
  never at a quote the market has already left; flush-finalized bars with no
  fresh quote do not open.
- Cooldown arms on EVERY fill (tp/sl/wick/time-stop), not just time stops.
- The daily -3R breaker and today's fills survive restart via ledger replay;
  open positions found in the ledger but not in memory are recorded as
  orphaned, never silently forgotten.

Only shadow mode exists. Live submission is a later, separately-gated step.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fxstack.scalp.bars import (
    M1Aggregator,
    M1Bar,
    aggregate_bars,
    window_is_complete,
)
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.families import evaluate_signal
from fxstack.scalp.gates import SpreadSentinel, session_veto_reason
from fxstack.scalp.ledger import ScalpLedger
from fxstack.scalp.portfolio import CurrencyBook
from fxstack.scalp.shadow import ShadowBook
from fxstack.scalp.signals import ScalpIntent
from fxstack.scalp.sizing import size_intent

#: Sizing runs on attested equity only; beyond this age the cached value is
#: discarded and sizing fails closed with equity_unattested.
_EQUITY_MAX_AGE_SECS = 600.0


def parse_tick_epoch(tick: dict[str, Any]) -> float | None:
    """Broker timestamp of a bridge tick, or None (drop the tick).

    The bridge's canonical fields are ``ts_epoch`` (float) and ``time`` (ISO).
    There is deliberately NO wall-clock fallback: an unattestable timestamp
    must not advance freshness.
    """

    raw = tick.get("ts_epoch")
    if isinstance(raw, (int, float)) and math.isfinite(float(raw)) and float(raw) > 0:
        return float(raw)
    iso = tick.get("time")
    if isinstance(iso, str) and iso:
        import datetime as dt

        try:
            return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class BridgeClient:
    def __init__(self, config: ScalpConfig) -> None:
        self.base = config.bridge_url.rstrip("/")
        self.key = config.api_key()
        self.errors = 0

    def _get(self, path: str) -> dict[str, Any] | None:
        req = urllib.request.Request(
            self.base + path, headers={"X-API-Key": self.key} if self.key else {}
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            self.errors += 1
            return None

    def ticks(self) -> dict[str, dict[str, Any]]:
        payload = self._get("/v2/market/ticks")
        if not payload:
            return {}
        raw = payload.get("ticks", payload)
        if isinstance(raw, list):
            raw = {str(r.get("symbol", "")).upper(): r for r in raw}
        return {str(k).upper(): dict(v) for k, v in dict(raw or {}).items()}

    def equity(self) -> float:
        payload = self._get("/v2/state") or {}
        state = payload.get("state", payload)
        try:
            value = float(dict(state or {}).get("equity") or 0.0)
        except (TypeError, ValueError):
            return 0.0
        return value if math.isfinite(value) and value > 0 else 0.0

    def specs(self) -> dict[str, dict[str, float]]:
        """Broker contract specs as published by the EA; {} if unavailable."""
        payload = self._get("/v2/market/specs") or {}
        raw = payload.get("specs")
        if not isinstance(raw, dict):
            return {}
        out: dict[str, dict[str, float]] = {}
        for sym, item in raw.items():
            if not isinstance(item, dict):
                continue
            clean: dict[str, float] = {}
            for key, value in item.items():
                try:
                    number = float(value)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(number):
                    clean[str(key)] = number
            if clean:
                out[str(sym).upper()] = clean
        return out


class ScalpLoop:
    def __init__(self, config: ScalpConfig | None = None) -> None:
        self.config = config or ScalpConfig()
        problems = self.config.validate()
        if problems:
            raise SystemExit("scalp config invalid: " + "; ".join(problems))
        data_root = Path(self.config.data_root)
        self.client = BridgeClient(self.config)
        self.aggregator = M1Aggregator(
            symbols=self.config.symbols,
            min_ticks_per_bar=self.config.min_ticks_per_bar,
            persist_root=data_root / "bars",
        )
        self.sentinel = SpreadSentinel(self.config)
        self.ledger = ScalpLedger(data_root / "ledger")
        self.book = ShadowBook(
            max_concurrent=self.config.max_concurrent,
            breakeven_at_r=self.config.breakeven_at_r,
        )
        # Every pair may propose simultaneously; currency exposure is what
        # bounds the book (see fxstack/scalp/portfolio.py).
        self.currency_book = CurrencyBook(
            max_currency_net_r=self.config.max_currency_net_r,
            max_total_gross_r=self.config.max_total_gross_r,
            max_concurrent=self.config.max_concurrent,
        )
        self._cooldown_until_minute: dict[str, int] = {}
        self._fresh_quote: dict[str, dict[str, float]] = {}
        self._rates: dict[str, float] = {}
        self._specs: dict[str, dict[str, float]] = {}
        self._specs_fetched = 0.0
        self._equity = 0.0
        self._equity_fetched = 0.0
        self._equity_attested = 0.0
        self._cycles = 0
        self._decisions = 0
        self._intents = 0
        self._opens = 0
        self._ledger_errors = 0
        self._state_path = data_root / "scalp_state.json"
        self.executor = None
        if self.config.mode == "live":
            from fxstack.scalp.authority import verify_certificate
            from fxstack.scalp.executor import LiveExecutor
            from fxstack.scalp.validate import load_certificate, scalp_config_sha256

            sha = scalp_config_sha256(self.config)
            cert = load_certificate(self.config.data_root)
            cert_error = ""
            for sym in self.config.symbols:
                cert_error = verify_certificate(
                    cert, now_epoch=time.time(), expected_config_sha256=sha, symbol=sym
                )
                if cert_error:
                    break
            if cert_error:
                # Fail closed and LOUD: live was requested but not earned.
                # The server would refuse every order anyway; refusing startup
                # surfaces it immediately instead of as a stream of 403s.
                raise SystemExit(f"live mode refused: {cert_error}")
            self.executor = LiveExecutor(self.config, config_sha256=sha)
        self._replay_today(now_epoch=time.time())

    # ------------------------------------------------------------------ cycle

    def run_forever(self) -> None:
        print(
            f"[scalp] shadow loop up symbols={','.join(self.config.symbols)} "
            f"bridge={self.config.bridge_url} risk={self.config.risk_fraction:.3%}/R "
            f"day_r={self.book.day_r:+.2f}"
        )
        while True:
            started = time.time()
            try:
                self.cycle(now_epoch=started)
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 -- loop survives, loudly
                print(f"[scalp] cycle_error {type(exc).__name__}: {exc}")
            elapsed = time.time() - started
            time.sleep(max(0.05, self.config.poll_secs - elapsed))

    def cycle(self, *, now_epoch: float) -> None:
        self._cycles += 1
        day_key = ScalpLedger.day_key(now_epoch)
        if now_epoch - self._specs_fetched > 120.0:
            fetched_specs = self.client.specs()
            if fetched_specs:
                self._specs = fetched_specs
            self._specs_fetched = now_epoch
        ticks = self.client.ticks()
        finalized: list[M1Bar] = []
        for sym in self.config.symbols:
            tick = ticks.get(sym)
            if not tick:
                continue
            bid = float(tick.get("bid") or 0.0)
            ask = float(tick.get("ask") or 0.0)
            ts = parse_tick_epoch(tick)
            if ts is None or bid <= 0.0 or ask <= 0.0 or ask < bid:
                continue
            mid = (bid + ask) / 2.0
            spread = float(tick.get("spread_bps") or 0.0)
            if spread <= 0.0 and mid > 0.0:
                # Derive from the quote rather than trusting a missing field;
                # the sentinel still vetoes if this too is degenerate.
                spread = (ask - bid) / mid * 1e4
            self._rates[sym] = mid
            self._fresh_quote[sym] = {"bid": bid, "ask": ask, "spread": spread, "ts": ts}
            self.sentinel.observe(symbol=sym, spread_bps=spread, ts_epoch=ts)
            fill = self.book.on_tick(
                symbol=sym, bid=bid, ask=ask, day_key=day_key, now_epoch=now_epoch
            )
            if fill is not None:
                self._record_fill(fill, epoch=now_epoch)
            finalized.extend(
                self.aggregator.ingest_tick(
                    symbol=sym, bid=bid, ask=ask, spread_bps=spread, ts_epoch=ts
                )
            )
        finalized.extend(self.aggregator.flush_stale(now_epoch=now_epoch))
        for bar in finalized:
            self._on_bar(bar, now_epoch=now_epoch, day_key=day_key)
        if self._cycles % 60 == 0:
            self._heartbeat(now_epoch=now_epoch)

    # ---------------------------------------------------------------- per-bar

    def _on_bar(self, bar: M1Bar, *, now_epoch: float, day_key: str) -> None:
        self._decisions += 1
        fill = self.book.on_bar_close(
            symbol=bar.symbol,
            bid_close=bar.bid_close,
            ask_close=bar.ask_close,
            day_key=day_key,
            minute_epoch=bar.minute_epoch,
            high=bar.high if bar.valid else None,
            low=bar.low if bar.valid else None,
            spread_max_bps=bar.spread_max_bps,
            now_epoch=now_epoch,
        )
        if fill is not None:
            self._record_fill(fill, epoch=now_epoch)

        reason_chain: dict[str, str] = {}
        intent_payload: dict[str, Any] | None = None
        opened = False

        block = self._entry_block_reason(bar, now_epoch=now_epoch, chain=reason_chain)
        if not block and not window_is_complete(
            bar.minute_epoch, bar_minutes=self.config.bar_minutes
        ):
            # Mid-window minute: fills and vetoes still processed above, but
            # the engine only DECIDES on a closed engine-timeframe bar.
            block = "engine_window_open"
            reason_chain["engine_window"] = "open"
        if not block:
            run = aggregate_bars(
                self.aggregator.consecutive_valid(bar.symbol),
                bar_minutes=self.config.bar_minutes,
            )
            intent, signal_reason = evaluate_signal(
                bars=run,
                config=self.config,
                spread_bps=self.sentinel.current_spread_bps(bar.symbol),
            )
            reason_chain["signal"] = signal_reason or "proposed"
            if intent is not None:
                self._intents += 1
                block, slippage_bps = self._freshen_entry(intent, now_epoch=now_epoch)
                if block:
                    reason_chain["entry_quote"] = block
                else:
                    reason_chain["entry_slippage_bps"] = f"{slippage_bps:+.2f}"
                    sized = size_intent(
                        intent=intent,
                        equity=self._refresh_equity(now_epoch),
                        config=self.config,
                        quote_rates=dict(self._rates),
                        specs=self._specs,
                    )
                    reason_chain["sizing"] = sized.reason or (
                        f"lots={sized.lots}" if sized.sizeable else "unsizeable"
                    )
                    intent_payload = sized.to_dict()
                    # Direction is known now, so the net currency check binds.
                    cluster_block = self.currency_book.admit(
                        symbol=intent.symbol, side=intent.side
                    )
                    if cluster_block:
                        reason_chain["portfolio"] = cluster_block
                        block = cluster_block
                        self._ledger_write(
                            kind="decision", epoch=now_epoch,
                            payload={
                                "symbol": bar.symbol, "minute": bar.minute_epoch,
                                "bar_valid": bar.valid,
                                "spread_bps": bar.spread_close_bps,
                                "reasons": reason_chain, "blocked_by": block,
                                "opened": False, "intent": intent_payload,
                                "mode": self.config.mode,
                            },
                        )
                        return
                    # Shadow opens even when unsizeable in lots (crypto): PnL
                    # is tracked in R so the machinery and the cost verdict
                    # still accumulate evidence.
                    self.book.open_from(sized)
                    self.currency_book.open_position(
                        symbol=intent.symbol, side=intent.side
                    )
                    self._opens += 1
                    opened = True
                    if self.executor is not None:
                        # Live submission runs BESIDE the shadow book, never
                        # instead of it -- divergence is ledger evidence.
                        accepted, live_reason, _ = self.executor.submit(sized)
                        reason_chain["live_submit"] = (
                            "accepted" if accepted else live_reason
                        )
        self._ledger_write(
            kind="decision",
            epoch=now_epoch,
            payload={
                "symbol": bar.symbol,
                "minute": bar.minute_epoch,
                "bar_valid": bar.valid,
                "spread_bps": bar.spread_close_bps,
                "reasons": reason_chain,
                "blocked_by": block,
                "opened": opened,
                "intent": intent_payload,
                "mode": self.config.mode,
            },
        )

    def _entry_block_reason(
        self, bar: M1Bar, *, now_epoch: float, chain: dict[str, str]
    ) -> str:
        if not bar.valid:
            chain["bar"] = bar.invalid_reason or "invalid"
            return "bar_invalid"
        if self.book.day_r <= self.config.daily_loss_stop_r:
            chain["breaker"] = f"day_r={self.book.day_r:.2f}"
            return "daily_loss_breaker"
        cooldown_until = self._cooldown_until_minute.get(bar.symbol, 0)
        if bar.minute_epoch < cooldown_until:
            chain["cooldown"] = f"until={cooldown_until}"
            return "cooldown"
        book_block = self.book.can_open(bar.symbol)
        if book_block:
            chain["book"] = book_block
            return book_block
        # Currency-cluster admission is checked here for the CHEAP refusals
        # (already open / concurrency / gross); the side-dependent net check
        # happens once the signal has a direction, just before opening.
        cluster_block = self.currency_book.admit(symbol=bar.symbol, side="BUY")
        if cluster_block in {"max_concurrent", "book_gross_risk_cap",
                             "unknown_currency_legs", "position_already_open"}:
            chain["portfolio"] = cluster_block
            return cluster_block
        session_block = session_veto_reason(
            symbol=bar.symbol, now_epoch=now_epoch, config=self.config
        )
        if session_block:
            chain["session"] = session_block
            return session_block
        sentinel_block = self.sentinel.veto_reason(symbol=bar.symbol, now_epoch=now_epoch)
        if sentinel_block:
            chain["sentinel"] = sentinel_block
            return sentinel_block
        return ""

    def _freshen_entry(self, intent: ScalpIntent, *, now_epoch: float) -> tuple[str, float]:
        """Re-anchor the intent to the CURRENT quote; refuse stale entries.

        Fill price is the adverse of {bar-close touch, latest touch}: the
        market may have moved since the bar closed, and a real order can only
        ever get the price that exists NOW or worse. Bracket geometry follows
        the actual entry so stop_bps/tp stay as designed.
        """

        quote = self._fresh_quote.get(intent.symbol)
        if quote is None or now_epoch - quote["ts"] > self.config.tick_stale_secs:
            return "no_fresh_entry_quote", 0.0
        signal_entry = intent.entry_price
        current = quote["ask"] if intent.side == "BUY" else quote["bid"]
        if current <= 0.0:
            return "no_fresh_entry_quote", 0.0
        entry = max(signal_entry, current) if intent.side == "BUY" else min(signal_entry, current)
        slippage_bps = (
            (entry - signal_entry) / signal_entry * 1e4 * (1.0 if intent.side == "BUY" else -1.0)
            if signal_entry > 0
            else 0.0
        )
        stop_px = intent.stop_bps / 1e4 * intent.ref_mid
        tp_px = (intent.tp_price - intent.entry_price) if intent.side == "BUY" else (
            intent.entry_price - intent.tp_price
        )
        intent.entry_price = entry
        if intent.side == "BUY":
            intent.sl_price = entry - stop_px
            intent.tp_price = entry + tp_px
        else:
            intent.sl_price = entry + stop_px
            intent.tp_price = entry - tp_px
        return "", slippage_bps

    # ------------------------------------------------------------------ misc

    def _refresh_equity(self, now_epoch: float) -> float:
        if now_epoch - self._equity_fetched > 30.0:
            fetched = self.client.equity()
            self._equity_fetched = now_epoch
            if fetched > 0.0:
                self._equity = fetched
                self._equity_attested = now_epoch
        if now_epoch - self._equity_attested > _EQUITY_MAX_AGE_SECS:
            # Hours-old equity must not size anything: fail closed.
            self._equity = 0.0
        return self._equity

    def _record_fill(self, fill: Any, *, epoch: float) -> None:
        # Cooldown arms on EVERY exit path -- tp, sl, wick, or time stop.
        self._cooldown_until_minute[fill.symbol] = (
            int(epoch // 60) * 60 + self.config.cooldown_bars * 60
        )
        # Release the currency exposure on every exit path too, or the book
        # silently ratchets shut as positions close.
        self.currency_book.close_position(fill.symbol)
        self._ledger_write(kind="fill", epoch=epoch, payload=fill.to_dict())
        print(
            f"[scalp] fill {fill.symbol} {fill.side} {fill.exit_reason} "
            f"pnl_r={fill.pnl_r:+.2f} pnl_bps={fill.pnl_bps:+.1f} day_r={self.book.day_r:+.2f}"
        )

    def _ledger_write(self, *, kind: str, epoch: float, payload: dict[str, Any]) -> None:
        try:
            self.ledger.record(kind=kind, epoch=epoch, payload=payload)
        except OSError:
            self._ledger_errors += 1

    def _replay_today(self, *, now_epoch: float) -> None:
        """Rebuild breaker state from today's ledger after a restart.

        Fills re-sum into day_r; decisions that opened positions without a
        matching later fill are recorded as orphaned (their outcomes are
        unknowable -- the dataset must say so rather than forget them).
        """

        day_key = ScalpLedger.day_key(now_epoch)
        path = Path(self.config.data_root) / "ledger" / f"ledger_{day_key}.jsonl"
        if not path.exists():
            return
        day_r = 0.0
        open_syms: dict[str, int] = {}
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if row.get("kind") == "fill":
                        day_r += float(row.get("pnl_r") or 0.0)
                        open_syms.pop(str(row.get("symbol") or ""), None)
                    elif row.get("kind") == "decision" and row.get("opened"):
                        open_syms[str(row.get("symbol") or "")] = int(row.get("minute") or 0)
                    elif row.get("kind") == "position_orphaned":
                        open_syms.pop(str(row.get("symbol") or ""), None)
        except OSError:
            return
        self.book.day_r = day_r
        self.book._day_key = day_key  # noqa: SLF001 -- deliberate rehydration
        for sym, minute in open_syms.items():
            self._ledger_write(
                kind="position_orphaned",
                epoch=now_epoch,
                payload={"symbol": sym, "opened_minute": minute, "cause": "restart"},
            )
        if day_r != 0.0 or open_syms:
            print(
                f"[scalp] replayed day_r={day_r:+.2f} orphaned={sorted(open_syms)} from {path.name}"
            )

    def _heartbeat(self, *, now_epoch: float) -> None:
        state = {
            "ts": now_epoch,
            "cycles": self._cycles,
            "decisions": self._decisions,
            "intents": self._intents,
            "opens": self._opens,
            "fills": len(self.book.fills),
            "open_positions": sorted(self.book.positions),
            "day_r": round(self.book.day_r, 3),
            "equity": self._equity,
            "http_errors": self.client.errors,
            "ledger_errors": self._ledger_errors,
            "mode": self.config.mode,
        }
        try:
            self._state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        except OSError:
            pass
        print(
            f"[scalp] hb cycles={self._cycles} decisions={self._decisions} "
            f"intents={self._intents} open={len(self.book.positions)} "
            f"fills={len(self.book.fills)} day_r={self.book.day_r:+.2f} "
            f"http_errors={self.client.errors} ledger_errors={self._ledger_errors}"
        )


def main() -> None:
    ScalpLoop().run_forever()


if __name__ == "__main__":
    main()
