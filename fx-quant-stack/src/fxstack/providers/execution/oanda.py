# AGENT: ROLE: OANDA v20 execution connector (dry-run, contract-complete).
# AGENT: ENTRYPOINT: `OandaConnector`, `command_to_wire_line`.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`, injected secret provider or env.
# AGENT: PRIMARY OUTPUTS: normalized order intents; OANDA v20 order bodies.
# AGENT: DEPENDS ON: `fxstack/providers/execution/_base.py`.
# AGENT: CALLED BY: execution provider dispatch, `tests/test_broker_connectors.py`.
# AGENT: STATE / SIDE EFFECTS: NONE in dry-run; NO network on import or default path.
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

PROVIDER = "oanda"

# OANDA "practice" (demo) host; real host is api-fxtrade.oanda.com. Neither is
# contacted unless dry_run=False AND an explicit endpoint is configured.
PRACTICE_ENDPOINT = "https://api-fxpractice.oanda.com"
LIVE_ENDPOINT = "https://api-fxtrade.oanda.com"


def _oanda_instrument(symbol: str) -> str:
    """Convert a canonical 6-char FX symbol (``EURUSD``) to OANDA's ``EUR_USD``."""

    txt = "".join(ch for ch in str(symbol or "").strip().upper() if ch.isalnum())
    if len(txt) == 6 and txt.isalpha():
        return f"{txt[:3]}_{txt[3:]}"
    return txt


def command_to_wire_line(command: ExecutionCommand) -> str:
    """Serialize a command to an OANDA-tagged wire line (mirrors mt4/paper format)."""

    cmd = str(command.cmd).strip().upper()
    parts: list[str] = [f"provider={PROVIDER}", f"cmd={cmd}"]
    if command.symbol:
        parts.append(f"symbol={_oanda_instrument(command.symbol)}")
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
class OandaConnector(BaseBrokerConnector):
    """OANDA v20 execution connector.

    Defaults to ``dry_run=True``: shapes OANDA order bodies and records intents
    with NO network call. The ``oandapyV20`` client is imported lazily and only
    reached when ``dry_run=False`` and an explicit ``endpoint`` is set.
    """

    provider: str = PROVIDER

    def credential_env_keys(self) -> dict[str, str]:
        return {
            "account_id": "OANDA_ACCOUNT_ID",
            "token": "OANDA_API_TOKEN",
        }

    def _shape_order(self, command: ExecutionCommand) -> dict[str, Any]:
        order = BaseBrokerConnector._shape_order(self, command)
        order["instrument"] = _oanda_instrument(command.symbol)
        cmd = order["cmd"]
        side = side_for_cmd(cmd)
        if cmd in {"BUY", "SELL"}:
            # OANDA MARKET orders carry signed units in the body.
            body: dict[str, Any] = {
                "order": {
                    "type": "MARKET",
                    "instrument": order["instrument"],
                    "units": str(order["units"]),
                    "timeInForce": "FOK",
                    "positionFill": "DEFAULT",
                }
            }
            if command.tp_price is not None:
                body["order"]["takeProfitOnFill"] = {"price": f"{float(command.tp_price):.5f}"}
            if command.sl_price is not None:
                body["order"]["stopLossOnFill"] = {"price": f"{float(command.sl_price):.5f}"}
            order["request_body"] = body
            order["request_path"] = "/v3/accounts/{account_id}/orders"
        elif cmd in {"CLOSE", "CLOSE_PARTIAL", "CLOSE_ALL"}:
            units = "ALL" if cmd != "CLOSE_PARTIAL" else str(abs(int(order["units"])))
            order["request_body"] = {"longUnits": units, "shortUnits": units}
            order["request_path"] = f"/v3/accounts/{{account_id}}/positions/{order['instrument']}/close"
        elif cmd == "MODIFY_SL":
            order["request_path"] = "/v3/accounts/{account_id}/trades"
        order["side"] = side
        return order

    def _submit_live(
        self,
        command: ExecutionCommand,
        order: dict[str, Any],
        creds: BrokerCredentials,
    ) -> dict[str, Any]:  # pragma: no cover - requires network + optional dep
        try:
            import oandapyV20  # type: ignore
            from oandapyV20.endpoints import orders as oanda_orders  # type: ignore  # noqa: F401
        except Exception as exc:
            raise RuntimeError(
                "oanda live execution requires optional dependency 'oandapyV20'"
            ) from exc
        if not creds.has_token():
            raise RuntimeError("oanda live execution requires OANDA_API_TOKEN")
        environment = "live" if str(self.endpoint).rstrip("/") == LIVE_ENDPOINT else "practice"
        client = oandapyV20.API(access_token=creds.token, environment=environment)
        request = oanda_orders.OrderCreate(
            accountID=creds.account_id,
            data=order.get("request_body") or {},
        )
        response = client.request(request)
        return {
            "provider": self.provider,
            "command_id": command.command_id,
            "status": "submitted",
            "symbol": command.symbol,
            "trace_id": command.trace_id or command.command_id,
            "raw": {"response": response},
        }
