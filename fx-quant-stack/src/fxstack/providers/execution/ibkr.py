# AGENT: ROLE: Interactive Brokers (IBKR) execution connector (dry-run, contract-complete).
# AGENT: ENTRYPOINT: `IbkrConnector`, `command_to_wire_line`.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`, injected secret provider or env.
# AGENT: PRIMARY OUTPUTS: normalized order intents; IB order/contract specs.
# AGENT: DEPENDS ON: `fxstack/providers/execution/_base.py`.
# AGENT: CALLED BY: execution provider dispatch, `tests/test_broker_connectors.py`.
# AGENT: STATE / SIDE EFFECTS: NONE in dry-run; localhost-only when live; no network on import.
# AGENT: HANDSHAKES: execution-provider order contract (shared with mt4/paper).
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fxstack.providers.execution._base import (
    BaseBrokerConnector,
    BrokerCredentials,
    side_for_cmd,
)
from fxstack.runtime.dto import ExecutionCommand

PROVIDER = "ibkr"

# IBKR routes through a local TWS / IB Gateway process; never a remote host.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PAPER_PORT = 4002  # IB Gateway paper; live gateway is 4001, TWS 7497/7496.


def _ib_pair(symbol: str) -> str:
    """Canonical 6-char FX symbol for ``ib_insync.Forex`` (``EURUSD``)."""

    return "".join(ch for ch in str(symbol or "").strip().upper() if ch.isalnum())


def command_to_wire_line(command: ExecutionCommand) -> str:
    """Serialize a command to an IBKR-tagged wire line (mirrors mt4/paper format)."""

    cmd = str(command.cmd).strip().upper()
    parts: list[str] = [f"provider={PROVIDER}", f"cmd={cmd}"]
    if command.symbol:
        parts.append(f"symbol={_ib_pair(command.symbol)}")
    lots_value = float(command.lots)
    if cmd == "CLOSE_PARTIAL":
        lots_value = float(command.close_lots if command.close_lots > 0.0 else command.lots)
        parts.append(f"close_lots={float(lots_value)}")
    parts.append(f"lots={float(lots_value)}")
    if command.tp_price is not None:
        parts.append(f"tp_price={float(command.tp_price)}")
    if command.sl_price is not None:
        parts.append(f"sl={float(command.sl_price)}")
    parts.extend(
        [
            f"command_id={command.command_id}",
            f"session_id={command.session_id}",
            f"intent={command.intent}",
            f"trace_id={command.trace_id or command.command_id}",
        ]
    )
    return ";".join(parts)


@dataclass(slots=True)
class IbkrConnector(BaseBrokerConnector):
    """Interactive Brokers execution connector via TWS / IB Gateway.

    Defaults to ``dry_run=True``: shapes IB ``Forex`` contracts + ``MarketOrder``
    specs and records intents with NO network call. ``ib_insync`` is imported
    lazily and only reached when ``dry_run=False`` and an explicit ``endpoint``
    (a localhost ``host:port``) is set.
    """

    provider: str = PROVIDER
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PAPER_PORT
    client_id: int = 1

    def credential_env_keys(self) -> dict[str, str]:
        # IBKR auth lives in the local TWS/Gateway session, not API tokens. We
        # still expose an account id (for order routing / multi-account logins).
        return {
            "account_id": "IBKR_ACCOUNT_ID",
            "token": "IBKR_API_TOKEN",
        }

    def _shape_order(self, command: ExecutionCommand) -> dict[str, Any]:
        order = BaseBrokerConnector._shape_order(self, command)
        cmd = order["cmd"]
        side = side_for_cmd(cmd)
        pair = _ib_pair(command.symbol)
        order["pair"] = pair
        order["contract"] = {"secType": "CASH", "symbol": pair[:3], "currency": pair[3:], "exchange": "IDEALPRO"}
        if cmd in {"BUY", "SELL"}:
            order["ib_order"] = {
                "action": side,
                "orderType": "MKT",
                # IB Forex quantity is the base-currency amount (== |units|).
                "totalQuantity": abs(int(order["units"])),
                "tif": "IOC",
            }
        elif cmd in {"CLOSE", "CLOSE_PARTIAL", "CLOSE_ALL"}:
            # Closes flip the resting side; quantity 0 == flatten everything.
            order["ib_order"] = {
                "action": "FLATTEN" if cmd != "CLOSE_PARTIAL" else "REDUCE",
                "orderType": "MKT",
                "totalQuantity": abs(int(order["units"])),
                "tif": "IOC",
            }
        elif cmd == "MODIFY_SL":
            order["ib_order"] = {"orderType": "STP", "auxPrice": float(command.sl_price or 0.0)}
        order["host"] = str(self.host)
        order["port"] = int(self.port)
        return order

    def _submit_live(
        self,
        command: ExecutionCommand,
        order: dict[str, Any],
        creds: BrokerCredentials,
    ) -> dict[str, Any]:  # pragma: no cover - requires local gateway + optional dep
        host = str(self.host or DEFAULT_HOST)
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("ibkr connector only permits localhost TWS/Gateway hosts")
        try:
            from ib_insync import IB, Forex, MarketOrder  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "ibkr live execution requires optional dependency 'ib_insync'"
            ) from exc
        ib = IB()
        ib.connect(host, int(self.port), clientId=int(self.client_id))
        try:
            contract = Forex(order["pair"])
            ib_order = MarketOrder(order["ib_order"]["action"], order["ib_order"]["totalQuantity"])
            trade = ib.placeOrder(contract, ib_order)
            ticket = int(getattr(getattr(trade, "order", None), "orderId", 0) or 0)
        finally:
            ib.disconnect()
        return {
            "provider": self.provider,
            "command_id": command.command_id,
            "status": "submitted",
            "symbol": command.symbol,
            "ticket": ticket,
            "trace_id": command.trace_id or command.command_id,
            "raw": {"host": host, "port": int(self.port)},
        }
