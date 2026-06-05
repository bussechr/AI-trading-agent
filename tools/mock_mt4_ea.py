"""Headless mock MT4 Expert Advisor for end-to-end forward testing.

Speaks the real fxstack bridge v2 protocol so the full live lifecycle can be
exercised without the MT4 GUI / a broker connection:

  GET  /v2/handshake            -> protocol + basket-TP fraction
  POST /v2/market/tick          -> stream bid/ask/spread per pair (keeps ticks fresh)
  POST /v2/reports              -> heartbeat (equity) + position snapshots
  GET  /v2/commands/poll?...    -> pull the next MT4 wire-line command
  POST /v2/commands/ack         -> simulate a fill and acknowledge

It maintains a tiny in-memory position book + equity so CLOSE/CLOSE_ALL behave,
and prints a JSON run summary. This is the EA side of the bridge contract that the
runtime's decisions flow through -- the deterministic execution venue, simulated.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

# Plausible mid prices so ticks look like a real venue (random-walked each cycle).
_BASE_PRICES = {
    "EURUSD": 1.10, "GBPUSD": 1.27, "AUDUSD": 0.66, "NZDUSD": 0.61,
    "USDCAD": 1.34, "USDCHF": 0.88, "USDJPY": 148.0, "EURJPY": 162.0,
    "EURGBP": 0.86, "GBPJPY": 188.0, "AUDJPY": 98.0, "CADJPY": 110.0,
    "CHFJPY": 168.0, "EURAUD": 1.66, "EURCAD": 1.47, "EURCHF": 0.97,
    "GBPCAD": 1.70, "GBPCHF": 1.12,
}


def _jpy(pair: str) -> bool:
    return pair.upper().endswith("JPY")


def parse_wire_line(line: str) -> dict[str, str]:
    """Parse a bridge MT4 wire line (``cmd=BUY;symbol=EURUSD;lots=0.1;...``)."""

    out: dict[str, str] = {}
    for token in str(line or "").strip().split(";"):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        out[key.strip()] = value.strip()
    return out


@dataclass
class _Position:
    ticket: int
    symbol: str
    side: str
    lots: float


@dataclass
class MockEA:
    bridge_url: str
    pairs: list[str]
    equity: float = 10000.0
    api_key: str = ""
    seed: int = 7
    digits: int = 5
    _rng_state: int = 0
    positions: dict[int, _Position] = field(default_factory=dict)
    next_ticket: int = 100001
    stats: dict[str, int] = field(default_factory=lambda: {
        "ticks": 0, "heartbeats": 0, "polls": 0, "commands": 0, "acks": 0,
        "opens": 0, "closes": 0, "errors": 0,
    })

    def __post_init__(self) -> None:
        self._rng_state = int(self.seed) or 1
        self._mid = {p: float(_BASE_PRICES.get(p.upper(), 1.0)) for p in self.pairs}

    # -- deterministic LCG so a run is reproducible without Math.random ---------
    def _rand(self) -> float:
        self._rng_state = (1103515245 * self._rng_state + 12345) & 0x7FFFFFFF
        return self._rng_state / 0x7FFFFFFF

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        out: dict[str, str] = {"X-API-Key": self.api_key} if self.api_key else {}
        if extra:
            out.update(extra)
        return out

    def _get(self, path: str, *, headers: dict[str, str] | None = None, **kw: Any) -> requests.Response:
        return requests.get(f"{self.bridge_url.rstrip('/')}{path}", headers=self._headers(headers), timeout=5, **kw)

    def _post(self, path: str, *, headers: dict[str, str] | None = None, **kw: Any) -> requests.Response:
        return requests.post(f"{self.bridge_url.rstrip('/')}{path}", headers=self._headers(headers), timeout=5, **kw)

    def handshake(self) -> dict[str, Any]:
        resp = self._get("/v2/handshake")
        resp.raise_for_status()
        return dict(resp.json())

    def _step_price(self, pair: str) -> tuple[float, float, float]:
        step = (self._rand() - 0.5) * (0.05 if _jpy(pair) else 0.0004)
        self._mid[pair] = max(0.0001, self._mid[pair] + step)
        mid = self._mid[pair]
        half_spread = (0.01 if _jpy(pair) else 0.00006) * (0.6 + self._rand())
        bid, ask = mid - half_spread, mid + half_spread
        spread_bps = max(0.1, (ask - bid) / mid * 10000.0)
        return round(bid, 3 if _jpy(pair) else 5), round(ask, 3 if _jpy(pair) else 5), round(spread_bps, 3)

    def post_ticks(self, ts: float) -> None:
        for pair in self.pairs:
            bid, ask, spread_bps = self._step_price(pair)
            try:
                r = self._post("/v2/market/tick", json={
                    "symbol": pair, "bid": bid, "ask": ask, "spread_bps": spread_bps,
                    "digits": 3 if _jpy(pair) else 5, "ts": ts,
                })
                r.raise_for_status()
                self.stats["ticks"] += 1
            except Exception:
                self.stats["errors"] += 1

    def post_heartbeat(self) -> None:
        try:
            r = self._post("/v2/reports", data=f"HEARTBEAT eq={self.equity:.2f}".encode("utf-8"),
                           headers={"Content-Type": "text/plain"})
            r.raise_for_status()
            self.stats["heartbeats"] += 1
        except Exception:
            self.stats["errors"] += 1

    def _simulate_fill(self, fields: dict[str, str]) -> dict[str, Any]:
        cmd = str(fields.get("cmd", "")).upper()
        symbol = str(fields.get("symbol", "")).upper()
        lots = float(fields.get("lots", fields.get("close_lots", "0")) or 0.0)
        ticket = self.next_ticket
        self.next_ticket += 1
        if cmd in {"BUY", "SELL"}:
            self.positions[ticket] = _Position(ticket=ticket, symbol=symbol, side=cmd, lots=lots)
            self.stats["opens"] += 1
            self.equity -= 0.02  # token round-trip cost so equity moves
        elif cmd == "CLOSE_ALL":
            self.positions.clear()
            self.stats["closes"] += 1
        elif cmd in {"CLOSE", "CLOSE_PARTIAL"}:
            for tk, pos in list(self.positions.items()):
                if pos.symbol == symbol:
                    del self.positions[tk]
            self.stats["closes"] += 1
        # "executed" maps to the bridge's 'acked' terminal status (runtime/dto.py).
        return {"ticket": ticket, "status": "executed"}

    def poll_and_ack(self, max_per_cycle: int = 8) -> None:
        for _ in range(max_per_cycle):
            self.stats["polls"] += 1
            try:
                r = self._get("/v2/commands/poll", params={"format": "line"})
                r.raise_for_status()
                text = r.text or ""
            except Exception:
                self.stats["errors"] += 1
                return
            if "cmd=" not in text:
                return  # no_command marker
            fields = parse_wire_line(text)
            command_id = fields.get("command_id") or fields.get("trace_id") or uuid.uuid4().hex
            self.stats["commands"] += 1
            fill = self._simulate_fill(fields)
            try:
                a = self._post("/v2/commands/ack", json={
                    "command_id": command_id, "ticket": fill["ticket"], "status": fill["status"],
                })
                a.raise_for_status()
                self.stats["acks"] += 1
            except Exception:
                self.stats["errors"] += 1

    def submit_test_command(self, side: str) -> str:
        """Inject a command via the operator endpoint (what the runtime would emit)."""

        command_id = f"fwd-{side.lower()}-{uuid.uuid4().hex[:8]}"
        symbol = self.pairs[0] if self.pairs else "EURUSD"
        body: dict[str, Any] = {"command_id": command_id, "session_id": "default", "intent": "SMOKE_TEST"}
        if side.upper() == "CLOSE_ALL":
            body.update({"cmd": "CLOSE_ALL", "action": "close_all"})
        else:
            body.update({"symbol": symbol, "cmd": side.upper(), "side": side.upper(),
                         "lots": 0.01, "action": "entry"})
        try:
            self._post("/v2/commands", json=body).raise_for_status()
        except Exception:
            self.stats["errors"] += 1
        return command_id

    def command_status(self, command_id: str) -> str:
        try:
            r = self._get("/v2/commands/events", params={"command_id": command_id, "limit": 20})
            r.raise_for_status()
            events = list(r.json().get("events", []) or [])
        except Exception:
            return "unknown"
        for ev in events:  # newest-first; first terminal status wins
            status = str(ev.get("event_status") or ev.get("status") or "").lower()
            if status in {"acked", "failed", "expired"}:
                return status
        return "pending"

    def post_positions(self) -> None:
        for pos in self.positions.values():
            try:
                self._post("/v2/reports", json={
                    "report_type": "position", "symbol": pos.symbol, "ticket": pos.ticket,
                    "side": pos.side, "lots": pos.lots,
                }).raise_for_status()
            except Exception:
                self.stats["errors"] += 1

    def run(self, *, duration_secs: float, tick_interval: float, inject_command: str = "none") -> dict[str, Any]:
        hs = self.handshake()
        injected_id = ""
        if inject_command and inject_command.lower() != "none":
            injected_id = self.submit_test_command(inject_command)
        deadline = time.time() + float(duration_secs)
        cycle = 0
        while time.time() < deadline:
            now = time.time()
            self.post_ticks(now)
            if cycle % 2 == 0:
                self.post_heartbeat()
            self.poll_and_ack()
            if cycle % 5 == 0:
                self.post_positions()
            cycle += 1
            time.sleep(max(0.05, float(tick_interval)))
        injected_status = self.command_status(injected_id) if injected_id else "n/a"
        return {
            "handshake": hs,
            "cycles": cycle,
            "stats": dict(self.stats),
            "open_positions": len(self.positions),
            "final_equity": round(self.equity, 2),
            "injected_command_id": injected_id,
            "injected_command_status": injected_status,
            "forward_test_passed": bool(self.stats["ticks"] > 0 and self.stats["heartbeats"] > 0
                                        and self.stats["errors"] == 0
                                        and (injected_status in {"acked", "n/a"})),
        }


def main() -> None:
    ap = argparse.ArgumentParser(description="Headless mock MT4 EA for bridge forward testing")
    ap.add_argument("--bridge-url", default="http://127.0.0.1:58710")
    ap.add_argument("--pairs", default="EURUSD,GBPUSD,USDJPY,AUDUSD")
    ap.add_argument("--duration-secs", type=float, default=60.0)
    ap.add_argument("--tick-interval", type=float, default=0.5)
    ap.add_argument("--equity", type=float, default=10000.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--inject-command", default="none", choices=["none", "BUY", "SELL", "CLOSE_ALL"],
                    help="Submit a test command at startup and verify it reaches 'acked'")
    args = ap.parse_args()

    pairs = [p.strip().upper() for p in str(args.pairs).split(",") if p.strip()]
    ea = MockEA(
        bridge_url=str(args.bridge_url), pairs=pairs, equity=float(args.equity), seed=int(args.seed),
        api_key=str(os.environ.get("FXSTACK_BRIDGE_API_KEY", "")).strip(),
    )
    summary = ea.run(duration_secs=float(args.duration_secs), tick_interval=float(args.tick_interval),
                     inject_command=str(args.inject_command))
    print(json.dumps(summary, indent=2, sort_keys=True, default=str))
    raise SystemExit(0 if summary.get("forward_test_passed") else 2)


if __name__ == "__main__":
    main()
