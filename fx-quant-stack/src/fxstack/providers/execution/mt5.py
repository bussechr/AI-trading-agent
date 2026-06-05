# AGENT: ROLE: MetaTrader 5 (MT5) execution connector (dry-run, contract-complete).
# AGENT: ENTRYPOINT: `Mt5Connector`, `command_to_wire_line`.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`, injected secret provider or env.
# AGENT: PRIMARY OUTPUTS: normalized order intents; MT5 `order_send` request dicts.
# AGENT: DEPENDS ON: `fxstack/providers/execution/_base.py`.
# AGENT: CALLED BY: execution provider dispatch, `tests/test_broker_connectors.py`.
# AGENT: STATE / SIDE EFFECTS: NONE in dry-run; local terminal only when live; no network on import.
# AGENT: HANDSHAKES: execution-provider order contract (shared with mt4/paper).
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fxstack.providers.execution._base import (
    BaseBrokerConnector,
    BrokerCredentials,
    round_lots,
    side_for_cmd,
)
from fxstack.runtime.dto import ExecutionCommand

PROVIDER = "mt5"

# MT5 order_send action / type sentinel values (mirror the MetaTrader5 constants
# so request dicts are correct without importing the optional package).
TRADE_ACTION_DEAL = 1
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1


def _mt5_symbol(symbol: str) -> str:
    return "".join(ch for ch in str(symbol or "").strip().upper() if ch.isalnum())


def command_to_wire_line(command: ExecutionCommand) -> str:
    """Serialize a command to an MT5-tagged wire line (mirrors mt4/paper format)."""

    cmd = str(command.cmd).strip().upper()
    parts: list[str] = [f"provider={PROVIDER}", f"cmd={cmd}"]
    if command.symbol:
        parts.append(f"symbol={_mt5_symbol(command.symbol)}")
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
            f"magic={int(command.magic)}",
            f"command_id={command.command_id}",
            f"session_id={command.session_id}",
            f"intent={command.intent}",
            f"trace_id={command.trace_id or command.command_id}",
        ]
    )
    return ";".join(parts)


@dataclass(slots=True)
class Mt5Connector(BaseBrokerConnector):
    """MetaTrader 5 execution connector via the local MT5 terminal.

    Defaults to ``dry_run=True``: shapes ``order_send`` request dicts (lots, not
    units) and records intents with NO network call. The ``MetaTrader5`` package
    is imported lazily and only reached when ``dry_run=False`` and an explicit
    ``endpoint`` (terminal path) is set.
    """

    provider: str = PROVIDER
    deviation: int = 20

    def credential_env_keys(self) -> dict[str, str]:
        return {
            "account_id": "MT5_LOGIN",
            "password": "MT5_PASSWORD",
            "token": "MT5_SERVER",  # server name reused via the generic token slot
        }

    def _shape_order(self, command: ExecutionCommand) -> dict[str, Any]:
        order = BaseBrokerConnector._shape_order(self, command)
        cmd = order["cmd"]
        side = side_for_cmd(cmd)
        symbol = _mt5_symbol(command.symbol)
        # MT5 trades in lots directly; round to the broker step for the request.
        lots = round_lots(
            float(order["lots"]),
            min_lot=self.min_lots,
            lot_step=self.lot_step,
            max_lot=self.max_lots,
        )
        order["mt5_symbol"] = symbol
        order["volume"] = lots
        if cmd in {"BUY", "SELL"}:
            request: dict[str, Any] = {
                "action": TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": lots,
                "type": ORDER_TYPE_BUY if side == "BUY" else ORDER_TYPE_SELL,
                "deviation": int(self.deviation),
                "magic": int(command.magic),
                "type_time": 0,
                "type_filling": 1,  # ORDER_FILLING_IOC
            }
            if command.tp_price is not None:
                request["tp"] = float(command.tp_price)
            if command.sl_price is not None:
                request["sl"] = float(command.sl_price)
            order["request"] = request
        elif cmd in {"CLOSE", "CLOSE_PARTIAL", "CLOSE_ALL"}:
            order["request"] = {
                "action": TRADE_ACTION_DEAL,
                "symbol": symbol if cmd != "CLOSE_ALL" else "",
                "volume": lots,
                "deviation": int(self.deviation),
                "magic": int(command.magic),
                "close": True,
            }
        elif cmd == "MODIFY_SL":
            order["request"] = {
                "action": 2,  # TRADE_ACTION_SLTP
                "symbol": symbol,
                "sl": float(command.sl_price or 0.0),
                "magic": int(command.magic),
            }
        return order

    def _submit_live(
        self,
        command: ExecutionCommand,
        order: dict[str, Any],
        creds: BrokerCredentials,
    ) -> dict[str, Any]:  # pragma: no cover - requires local MT5 terminal + optional dep
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            raise RuntimeError(
                "mt5 live execution requires optional dependency 'MetaTrader5'"
            ) from exc
        if not mt5.initialize(path=str(self.endpoint)):
            raise RuntimeError("mt5 terminal initialize failed")
        try:
            if creds.account_id and creds.has_password():
                mt5.login(int(creds.account_id), password=creds.password, server=creds.token)
            result = mt5.order_send(order.get("request") or {})
            retcode = int(getattr(result, "retcode", -1))
            ticket = int(getattr(result, "order", 0) or 0)
        finally:
            mt5.shutdown()
        return {
            "provider": self.provider,
            "command_id": command.command_id,
            "status": "submitted" if retcode in (10009, 10008) else "failed",
            "symbol": command.symbol,
            "ticket": ticket,
            "trace_id": command.trace_id or command.command_id,
            "raw": {"retcode": retcode},
        }
