"""The scalper decision loop -- a standalone process against the bridge API.

Runs at tick cadence (default 1s poll), decides on M1 bar close, and writes
every decision to the ledger. Talks HTTP to the existing bridge exactly like
any other consumer; imports nothing from the runner or the ceremony layers.

Conjunctive per-bar pipeline (first non-empty reason wins, all recorded):

    bar_invalid -> daily_breaker -> cooldown -> book -> session -> sentinel
                -> signal -> sizing -> open shadow position

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

from fxstack.scalp.bars import M1Aggregator, M1Bar
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.gates import SpreadSentinel, session_veto_reason
from fxstack.scalp.ledger import ScalpLedger
from fxstack.scalp.shadow import ShadowBook
from fxstack.scalp.signals import evaluate_dislocation
from fxstack.scalp.sizing import size_intent


def _parse_epoch(value: Any, *, fallback: float) -> float:
    if isinstance(value, (int, float)) and math.isfinite(float(value)) and float(value) > 0:
        return float(value)
    if isinstance(value, str) and value:
        import datetime as dt

        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return float(fallback)


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
        self.book = ShadowBook(max_concurrent=self.config.max_concurrent)
        self._cooldown_until_minute: dict[str, int] = {}
        self._equity = 0.0
        self._equity_fetched = 0.0
        self._cycles = 0
        self._decisions = 0
        self._intents = 0
        self._opens = 0
        self._state_path = data_root / "scalp_state.json"

    # ------------------------------------------------------------------ cycle

    def run_forever(self) -> None:
        print(
            f"[scalp] shadow loop up symbols={','.join(self.config.symbols)} "
            f"bridge={self.config.bridge_url} risk={self.config.risk_fraction:.3%}/R"
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
        ticks = self.client.ticks()
        finalized: list[M1Bar] = []
        for sym in self.config.symbols:
            tick = ticks.get(sym)
            if not tick:
                continue
            bid = float(tick.get("bid") or 0.0)
            ask = float(tick.get("ask") or 0.0)
            spread = float(tick.get("spread_bps") or 0.0)
            ts = _parse_epoch(
                tick.get("ts") or tick.get("time") or tick.get("received_at"),
                fallback=now_epoch,
            )
            self.sentinel.observe(symbol=sym, spread_bps=spread, ts_epoch=ts)
            fill = self.book.on_tick(symbol=sym, bid=bid, ask=ask, day_key=day_key)
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
        )
        if fill is not None:
            self._record_fill(fill, epoch=now_epoch)
            self._cooldown_until_minute[bar.symbol] = (
                bar.minute_epoch + self.config.cooldown_bars * 60
            )

        reason_chain: dict[str, str] = {}
        intent_payload: dict[str, Any] | None = None
        opened = False

        block = self._entry_block_reason(bar, now_epoch=now_epoch, chain=reason_chain)
        if not block:
            run = self.aggregator.consecutive_valid(bar.symbol)
            intent, signal_reason = evaluate_dislocation(
                bars=run,
                config=self.config,
                spread_bps=self.sentinel.current_spread_bps(bar.symbol),
            )
            reason_chain["signal"] = signal_reason or "proposed"
            if intent is not None:
                self._intents += 1
                sized = size_intent(
                    intent=intent, equity=self._refresh_equity(now_epoch), config=self.config
                )
                reason_chain["sizing"] = sized.reason or (
                    f"lots={sized.lots}" if sized.sizeable else "unsizeable"
                )
                intent_payload = sized.to_dict()
                # Shadow opens even when unsizeable in lots (crypto): PnL is
                # tracked in R so the machinery and the cost verdict still
                # accumulate evidence.
                self.book.open_from(sized)
                self._opens += 1
                opened = True
        self.ledger.record(
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

    # ------------------------------------------------------------------ misc

    def _refresh_equity(self, now_epoch: float) -> float:
        if now_epoch - self._equity_fetched > 30.0:
            fetched = self.client.equity()
            if fetched > 0.0:
                self._equity = fetched
            self._equity_fetched = now_epoch
        return self._equity

    def _record_fill(self, fill: Any, *, epoch: float) -> None:
        self.ledger.record(kind="fill", epoch=epoch, payload=fill.to_dict())
        print(
            f"[scalp] fill {fill.symbol} {fill.side} {fill.exit_reason} "
            f"pnl_r={fill.pnl_r:+.2f} pnl_bps={fill.pnl_bps:+.1f} day_r={self.book.day_r:+.2f}"
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
            f"http_errors={self.client.errors}"
        )


def main() -> None:
    ScalpLoop().run_forever()


if __name__ == "__main__":
    main()
