from __future__ import annotations

import json
from typing import Any

import pytest

from fxstack.providers.contracts import ProviderCapabilities
from fxstack.providers.execution import ibkr as ibkr_mod
from fxstack.providers.execution import mt5 as mt5_mod
from fxstack.providers.execution import oanda as oanda_mod
from fxstack.providers.execution._base import (
    BaseBrokerConnector,
    BrokerCredentials,
    lots_to_units,
    resolve_secret,
    round_lots,
)
from fxstack.providers.execution.ibkr import IbkrConnector
from fxstack.providers.execution.mt5 import Mt5Connector
from fxstack.providers.execution.oanda import OandaConnector, _oanda_instrument
from fxstack.runtime.dto import ExecutionCommand

# Secret value that must NEVER appear in any intent / log-safe surface.
_SECRET_TOKEN = "super-secret-token-do-not-log"  # noqa: S105 - test fixture only
_SECRET_PASSWORD = "hunter2-secret"  # noqa: S105 - test fixture only

ALL_CONNECTORS = [OandaConnector, IbkrConnector, Mt5Connector]


def _command(
    cmd: str = "BUY",
    *,
    symbol: str = "EURUSD",
    lots: float = 0.1,
    close_lots: float = 0.0,
    tp_price: float | None = None,
    sl_price: float | None = None,
    command_id: str = "cmd-123",
) -> ExecutionCommand:
    return ExecutionCommand(
        command_id=command_id,
        session_id="sess-1",
        proto="v2",
        cmd=cmd,
        symbol=symbol,
        lots=lots,
        close_lots=close_lots,
        tp_price=tp_price,
        sl_price=sl_price,
        intent="ENTRY" if cmd in {"BUY", "SELL"} else "EXIT",
        trace_id="trace-1",
    )


def _secret_env() -> dict[str, str]:
    return {
        "OANDA_ACCOUNT_ID": "001-oanda-acct",
        "OANDA_API_TOKEN": _SECRET_TOKEN,
        "IBKR_ACCOUNT_ID": "DU-ibkr-acct",
        "IBKR_API_TOKEN": _SECRET_TOKEN,
        "MT5_LOGIN": "555555",
        "MT5_PASSWORD": _SECRET_PASSWORD,
        "MT5_SERVER": "Demo-Server",
    }


def _connector(cls: type[BaseBrokerConnector], **kwargs: Any) -> BaseBrokerConnector:
    params: dict[str, Any] = {"env": _secret_env()}
    params.update(kwargs)
    return cls(**params)


# --------------------------------------------------------------------------- #
# Contract conformance
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_connector_defaults_to_dry_run(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    assert conn.dry_run is True
    assert conn.endpoint == ""


@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_connector_capabilities_contract(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    caps = conn.capabilities()
    assert isinstance(caps, ProviderCapabilities)
    assert caps.supports_execution is True
    assert caps.provider == conn.provider
    assert "fx" in caps.asset_classes
    # Dry-run connectors are shadow-only (never touch a live account).
    assert caps.shadow_only is True


@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_wire_line_modules_match_provider_format(cls: type[BaseBrokerConnector]) -> None:
    module = {"oanda": oanda_mod, "ibkr": ibkr_mod, "mt5": mt5_mod}[_connector(cls).provider]
    line = module.command_to_wire_line(_command("BUY", tp_price=1.2, sl_price=1.05))
    assert line.startswith(f"provider={module.PROVIDER};cmd=BUY")
    assert "symbol=" in line
    assert "lots=" in line
    assert "command_id=cmd-123" in line
    # Wire line is a single delimited row (mirrors mt4/paper) with no newlines.
    assert "\n" not in line and "\r" not in line


# --------------------------------------------------------------------------- #
# Dry-run submission returns structured intent without network
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_dry_run_submit_returns_structured_intent(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    intent = conn.submit(_command("BUY", lots=0.1))
    assert intent["provider"] == conn.provider
    assert intent["status"] == "dry_run"
    assert intent["command_id"] == "cmd-123"
    assert intent["symbol"] == "EURUSD"
    assert intent["trace_id"] == "trace-1"
    assert int(intent["ticket"]) > 0
    assert intent["raw"]["dry_run"] is True
    assert intent["raw"]["endpoint_set"] is False
    order = intent["raw"]["order"]
    assert order["cmd"] == "BUY"
    assert order["side"] == "BUY"
    # Intent is recorded on the connector for audit.
    assert conn.intents and conn.intents[-1]["command_id"] == "cmd-123"


@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_dry_run_submit_is_deterministic(cls: type[BaseBrokerConnector]) -> None:
    a = _connector(cls).submit(_command("BUY"))
    b = _connector(cls).submit(_command("BUY"))
    assert a["ticket"] == b["ticket"]
    assert a["raw"]["order"] == b["raw"]["order"]


def test_dry_run_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any attempt to open a socket during a dry-run submit must fail the test.
    import socket

    def _boom(*_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover - guard
        raise AssertionError("dry-run submit attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    monkeypatch.setattr(socket.socket, "connect_ex", _boom)
    for cls in ALL_CONNECTORS:
        conn = _connector(cls)
        for verb in ("BUY", "SELL", "CLOSE", "CLOSE_PARTIAL"):
            cmd = _command(verb, lots=0.2, close_lots=0.1)
            out = conn.submit(cmd)
            assert out["status"] == "dry_run"


# --------------------------------------------------------------------------- #
# Credentials are not logged / exposed
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_secrets_never_appear_in_intent(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    intent = conn.submit(_command("BUY"))
    blob = json.dumps(intent)
    assert _SECRET_TOKEN not in blob
    assert _SECRET_PASSWORD not in blob
    # Only presence flags are surfaced, never the secret values.
    creds_view = intent["raw"]["order"]["credentials"]
    assert set(creds_view.keys()) <= {"account_id", "token_present", "password_present", "extra_keys"}


@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_credentials_repr_redacts_secrets(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    creds = conn.load_credentials()
    assert _SECRET_TOKEN not in repr(creds)
    assert _SECRET_PASSWORD not in repr(creds)
    assert _SECRET_TOKEN not in repr(conn)


def test_injected_secret_provider_callable_used_over_env() -> None:
    captured: list[str] = []

    def provider(name: str) -> str:
        captured.append(name)
        return {"OANDA_API_TOKEN": _SECRET_TOKEN, "OANDA_ACCOUNT_ID": "acct-from-provider"}.get(name, "")

    conn = OandaConnector(secret_provider=provider, env={})
    creds = conn.load_credentials()
    assert creds.account_id == "acct-from-provider"
    assert creds.has_token() is True
    assert "OANDA_API_TOKEN" in captured


def test_resolve_secret_precedence_and_default() -> None:
    # injected provider wins
    assert resolve_secret("K", secret_provider={"K": "from-provider"}, env={"K": "from-env"}) == "from-provider"
    # falls through to env when provider is empty
    assert resolve_secret("K", secret_provider={"K": ""}, env={"K": "from-env"}) == "from-env"
    # default when neither present
    assert resolve_secret("K", secret_provider={}, env={}, default="dflt") == "dflt"


# --------------------------------------------------------------------------- #
# Units / lot rounding
# --------------------------------------------------------------------------- #
def test_round_lots_floors_to_step() -> None:
    assert round_lots(0.137, min_lot=0.01, lot_step=0.01) == 0.13
    assert round_lots(0.1, min_lot=0.01, lot_step=0.01) == 0.1


def test_round_lots_collapses_below_min() -> None:
    assert round_lots(0.004, min_lot=0.01, lot_step=0.01) == 0.0


def test_round_lots_clamps_to_max() -> None:
    assert round_lots(5.0, min_lot=0.01, lot_step=0.01, max_lot=1.0) == 1.0


def test_lots_to_units_signed_by_side() -> None:
    assert lots_to_units(0.1, side="BUY") == 10_000
    assert lots_to_units(0.1, side="SELL") == -10_000
    assert lots_to_units(1.0, side="BUY") == 100_000


@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_submit_normalizes_lot_step(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    intent = conn.submit(_command("BUY", lots=0.137))
    assert intent["raw"]["order"]["lots"] == 0.13


@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_submit_rejects_sub_min_lots(cls: type[BaseBrokerConnector]) -> None:
    conn = _connector(cls)
    with pytest.raises(ValueError):
        conn.submit(_command("BUY", lots=0.004))


def test_oanda_units_and_instrument() -> None:
    conn = OandaConnector(env=_secret_env())
    buy = conn.submit(_command("BUY", lots=0.1))
    order = buy["raw"]["order"]
    assert order["instrument"] == "EUR_USD"
    assert order["units"] == 10_000
    assert order["request_body"]["order"]["units"] == "10000"
    sell = conn.submit(_command("SELL", lots=0.1, command_id="cmd-sell"))
    assert sell["raw"]["order"]["units"] == -10_000
    assert sell["raw"]["order"]["request_body"]["order"]["units"] == "-10000"
    assert _oanda_instrument("gbpjpy") == "GBP_JPY"


def test_ibkr_uses_base_currency_quantity_and_localhost() -> None:
    conn = IbkrConnector(env=_secret_env())
    intent = conn.submit(_command("BUY", lots=0.2))
    order = intent["raw"]["order"]
    assert order["contract"] == {"secType": "CASH", "symbol": "EUR", "currency": "USD", "exchange": "IDEALPRO"}
    assert order["ib_order"]["totalQuantity"] == 20_000
    assert order["host"] in {"127.0.0.1", "localhost"}


def test_mt5_uses_lots_not_units() -> None:
    conn = Mt5Connector(env=_secret_env())
    intent = conn.submit(_command("BUY", lots=0.1, tp_price=1.2, sl_price=1.05))
    order = intent["raw"]["order"]
    assert order["volume"] == 0.1
    assert order["request"]["volume"] == 0.1
    assert order["request"]["type"] == mt5_mod.ORDER_TYPE_BUY
    assert order["request"]["tp"] == 1.2
    assert order["request"]["sl"] == 1.05


# --------------------------------------------------------------------------- #
# Live path gating
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("cls", ALL_CONNECTORS)
def test_live_disabled_without_endpoint(cls: type[BaseBrokerConnector]) -> None:
    # dry_run=False but no endpoint -> still produces an offline dry-run intent.
    conn = _connector(cls, dry_run=False, endpoint="")
    intent = conn.submit(_command("BUY"))
    assert intent["status"] == "dry_run"


def test_live_path_requires_optional_dep_lazily() -> None:
    # With dry_run=False AND an endpoint, the connector reaches _submit_live, which
    # raises a clear RuntimeError when the optional client lib is missing. No
    # network is attempted because the import fails first.
    conn = OandaConnector(env=_secret_env(), dry_run=False, endpoint=oanda_mod.PRACTICE_ENDPOINT)
    try:
        import oandapyV20  # type: ignore  # noqa: F401

        has_dep = True
    except Exception:
        has_dep = False
    if not has_dep:
        with pytest.raises(RuntimeError, match="oandapyV20"):
            conn.submit(_command("BUY"))


def test_ibkr_live_rejects_non_localhost() -> None:
    conn = IbkrConnector(env=_secret_env(), dry_run=False, endpoint="remote:4002", host="10.0.0.5")
    with pytest.raises(RuntimeError, match="localhost"):
        conn.submit(_command("BUY"))


# --------------------------------------------------------------------------- #
# Misc base helpers
# --------------------------------------------------------------------------- #
def test_broker_credentials_redacted_shape() -> None:
    creds = BrokerCredentials(
        account_id="A1",
        _token=_SECRET_TOKEN,
        _password=_SECRET_PASSWORD,
        extra={"k": "v"},
    )
    view = creds.redacted()
    assert view == {
        "account_id": "A1",
        "token_present": True,
        "password_present": True,
        "extra_keys": ["k"],
    }
    assert _SECRET_TOKEN not in repr(creds)
    assert _SECRET_PASSWORD not in repr(creds)
