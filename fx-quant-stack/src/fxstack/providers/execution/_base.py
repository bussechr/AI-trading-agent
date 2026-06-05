# AGENT: ROLE: Shared dry-run scaffolding for external broker execution connectors.
# AGENT: ENTRYPOINT: imported by oanda.py / ibkr.py / mt5.py connectors.
# AGENT: PRIMARY INPUTS: `ExecutionCommand`, injected secret provider or env.
# AGENT: PRIMARY OUTPUTS: normalized order intents (`ExecutionUpdate`-shaped dicts).
# AGENT: DEPENDS ON: `fxstack/runtime/dto.py`, `fxstack/providers/contracts.py`.
# AGENT: CALLED BY: broker connectors, `tests/test_broker_connectors.py`.
# AGENT: STATE / SIDE EFFECTS: none by default; NO network in dry-run path.
# AGENT: HANDSHAKES: execution-provider order contract (shared with mt4/paper).
from __future__ import annotations

import math
import os
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fxstack.providers.contracts import ExecutionUpdate, ProviderCapabilities
from fxstack.runtime.dto import ExecutionCommand

# Commands handled by the existing execution providers; mirrored here so broker
# connectors reject anything the rest of the stack does not understand.
SUPPORTED_COMMANDS = {"BUY", "SELL", "CLOSE", "CLOSE_ALL", "CLOSE_PARTIAL", "MODIFY_SL", "INFO"}

# Default broker lot conventions (standard FX micro-lot granularity).
DEFAULT_MIN_LOTS = 0.01
DEFAULT_LOT_STEP = 0.01
DEFAULT_MAX_LOTS = 0.0  # 0.0 == unbounded
# 1 standard FX lot == 100_000 units of base currency.
UNITS_PER_LOT = 100_000

# Type alias for an injected secret provider: a mapping or a callable name -> value.
SecretProvider = Mapping[str, Any] | Callable[[str], Any]


def round_lots(
    value: float,
    *,
    min_lot: float = DEFAULT_MIN_LOTS,
    lot_step: float = DEFAULT_LOT_STEP,
    max_lot: float = DEFAULT_MAX_LOTS,
) -> float:
    """Round ``value`` down to the broker lot step.

    Mirrors ``fxstack.risk.kernel._round_lots`` so connector-level normalization
    matches the risk kernel: sub-min requests collapse to zero, otherwise floor to
    the step and clamp to ``max_lot``.
    """

    lots = max(0.0, float(value))
    if lots <= 0.0:
        return 0.0
    min_lot = max(0.0, float(min_lot))
    step = max(1e-9, float(lot_step))
    tolerance = max(1e-9, step / 10.0)
    if min_lot > 0.0 and lots + tolerance < min_lot:
        return 0.0
    lots = math.floor((lots + tolerance) / step) * step
    if min_lot > 0.0 and lots + tolerance < min_lot:
        lots = float(min_lot)
    if max_lot > 0.0:
        lots = min(float(max_lot), lots)
    if min_lot > 0.0 and lots + tolerance < min_lot:
        return 0.0
    return round(float(lots), 8)


def lots_to_units(lots: float, *, side: str, units_per_lot: int = UNITS_PER_LOT) -> int:
    """Convert lots to signed broker units (positive long, negative short)."""

    magnitude = int(round(abs(float(lots)) * int(units_per_lot)))
    return -magnitude if str(side or "").upper() == "SELL" else magnitude


def side_for_cmd(cmd: str) -> str:
    """Map a command verb to a directional side (``BUY``/``SELL``/``""``)."""

    up = str(cmd or "").strip().upper()
    if up in {"BUY", "SELL"}:
        return up
    return ""


def deterministic_ticket(command_id: str) -> int:
    """Stable, offline pseudo-ticket derived from the command id (crc32)."""

    return max(1, int(zlib.crc32(str(command_id or "").encode("utf-8")) & 0x7FFFFFFF))


def resolve_secret(
    name: str,
    *,
    secret_provider: SecretProvider | None = None,
    env: Mapping[str, str] | None = None,
    default: str = "",
) -> str:
    """Resolve a credential from an injected provider, then the environment.

    The returned value is never logged by this module. Lookups fall through in
    order: injected provider -> ``env`` (defaults to ``os.environ``) -> ``default``.
    """

    if secret_provider is not None:
        try:
            if callable(secret_provider):
                value = secret_provider(name)
            elif isinstance(secret_provider, Mapping):
                value = secret_provider.get(name)
            else:  # pragma: no cover - defensive
                value = None
        except Exception:
            value = None
        if value is not None and str(value).strip():
            return str(value)
    source = os.environ if env is None else env
    value = source.get(name)
    if value is not None and str(value).strip():
        return str(value)
    return str(default)


@dataclass(slots=True)
class BrokerCredentials:
    """Credential bundle for a broker connector.

    Secret material is held in private fields and deliberately excluded from
    ``__repr__`` / ``redacted()`` output so it never lands in logs.
    """

    account_id: str = ""
    _token: str = field(default="", repr=False)
    _password: str = field(default="", repr=False)
    extra: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def token(self) -> str:
        return self._token

    @property
    def password(self) -> str:
        return self._password

    def has_token(self) -> bool:
        return bool(str(self._token).strip())

    def has_password(self) -> bool:
        return bool(str(self._password).strip())

    def redacted(self) -> dict[str, Any]:
        """Log-safe view: presence flags only, never the secret values."""

        return {
            "account_id": self.account_id,
            "token_present": self.has_token(),
            "password_present": self.has_password(),
            "extra_keys": sorted(self.extra.keys()),
        }


@dataclass(slots=True)
class BaseBrokerConnector:
    """Common dry-run execution scaffolding for external broker connectors.

    Subclasses set :pyattr:`provider` and may override :meth:`_shape_order` to add
    venue-specific fields. By default ``dry_run=True``: :meth:`submit` shapes and
    records the intended action and returns a structured ``ExecutionUpdate``-shaped
    intent WITHOUT any network call. Live API access is only reachable when
    ``dry_run=False`` AND an explicit endpoint is configured, and the real client
    library is imported lazily inside :meth:`_submit_live`.
    """

    provider: str = "broker"
    dry_run: bool = True
    endpoint: str = ""
    account_id: str = ""
    min_lots: float = DEFAULT_MIN_LOTS
    lot_step: float = DEFAULT_LOT_STEP
    max_lots: float = DEFAULT_MAX_LOTS
    units_per_lot: int = UNITS_PER_LOT
    secret_provider: SecretProvider | None = field(default=None, repr=False)
    env: Mapping[str, str] | None = field(default=None, repr=False)
    intents: list[dict[str, Any]] = field(default_factory=list, repr=False)

    # -- credentials ---------------------------------------------------------
    def credential_env_keys(self) -> dict[str, str]:
        """Map logical credential roles to environment variable names.

        Override per broker. Roles: ``account_id``, ``token``, ``password``.
        """

        return {}

    def load_credentials(self) -> BrokerCredentials:
        keys = self.credential_env_keys()
        account = self.account_id or resolve_secret(
            keys.get("account_id", ""),
            secret_provider=self.secret_provider,
            env=self.env,
        )
        token = resolve_secret(
            keys.get("token", ""),
            secret_provider=self.secret_provider,
            env=self.env,
        )
        password = resolve_secret(
            keys.get("password", ""),
            secret_provider=self.secret_provider,
            env=self.env,
        )
        return BrokerCredentials(account_id=str(account), _token=token, _password=password)

    # -- capabilities --------------------------------------------------------
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider=str(self.provider),
            asset_classes=["fx"],
            supports_execution=True,
            supports_bid_ask=False,
            supports_proxy_spread=False,
            shadow_only=bool(self.dry_run),
            metadata={"dry_run": bool(self.dry_run)},
        )

    # -- order shaping -------------------------------------------------------
    def normalize_command(self, command: ExecutionCommand) -> ExecutionCommand:
        """Validate and apply risk-relevant order normalization.

        Returns a copy of ``command`` with lots/close_lots rounded to the broker
        lot step. Raises ``ValueError`` for unsupported / malformed commands.
        """

        command.validate()
        cmd = str(command.cmd).strip().upper()
        if cmd not in SUPPORTED_COMMANDS:
            raise ValueError(f"unsupported cmd: {command.cmd}")
        out = ExecutionCommand(**command.to_dict())
        if cmd in {"BUY", "SELL"}:
            out.lots = round_lots(
                command.lots,
                min_lot=self.min_lots,
                lot_step=self.lot_step,
                max_lot=self.max_lots,
            )
            if out.lots <= 0.0:
                raise ValueError("normalized lots collapsed below min_lot")
        elif cmd == "CLOSE_PARTIAL":
            raw = command.close_lots if command.close_lots > 0.0 else command.lots
            out.close_lots = round_lots(
                raw,
                min_lot=self.min_lots,
                lot_step=self.lot_step,
                max_lot=self.max_lots,
            )
            out.lots = out.close_lots
            if out.close_lots <= 0.0:
                raise ValueError("normalized close_lots collapsed below min_lot")
        return out

    def _shape_order(self, command: ExecutionCommand) -> dict[str, Any]:
        """Venue-neutral order shape. Subclasses extend with broker fields."""

        cmd = str(command.cmd).strip().upper()
        side = side_for_cmd(cmd)
        lots = float(command.close_lots if cmd == "CLOSE_PARTIAL" and command.close_lots > 0.0 else command.lots)
        units = lots_to_units(lots, side=side, units_per_lot=self.units_per_lot)
        order: dict[str, Any] = {
            "provider": str(self.provider),
            "command_id": command.command_id,
            "symbol": command.symbol,
            "cmd": cmd,
            "side": side,
            "lots": lots,
            "units": units,
            "units_per_lot": int(self.units_per_lot),
            "trace_id": command.trace_id or command.command_id,
        }
        if command.tp_price is not None:
            order["tp_price"] = float(command.tp_price)
        if command.sl_price is not None:
            order["sl_price"] = float(command.sl_price)
        return order

    # -- submission ----------------------------------------------------------
    def submit(self, command: ExecutionCommand) -> dict[str, Any]:
        """Submit (or, in dry-run, simulate) an order.

        In dry-run mode this performs NO network I/O: it normalizes the command,
        shapes the broker order, records the intent, and returns a structured
        ``ExecutionUpdate``-shaped dict with ``status='dry_run'``. Credentials are
        resolved only as presence flags for the audit record and are never logged.
        """

        normalized = self.normalize_command(command)
        order = self._shape_order(normalized)
        creds = self.load_credentials()
        order["credentials"] = creds.redacted()  # presence flags only
        if self.dry_run or not str(self.endpoint).strip():
            update = ExecutionUpdate(
                provider=str(self.provider),
                command_id=normalized.command_id,
                status="dry_run",
                symbol=normalized.symbol,
                ticket=deterministic_ticket(normalized.command_id),
                message=f"{self.provider}_dry_run_intent",
                trace_id=normalized.trace_id or normalized.command_id,
                raw={"order": order, "dry_run": True, "endpoint_set": bool(str(self.endpoint).strip())},
            )
            intent = update.to_dict()
            self.intents.append(intent)
            return intent
        # Live path: explicitly opted in (dry_run=False AND endpoint set).
        return self._submit_live(normalized, order, creds)

    def _submit_live(
        self,
        command: ExecutionCommand,
        order: dict[str, Any],
        creds: BrokerCredentials,
    ) -> dict[str, Any]:  # pragma: no cover - never exercised in tests/offline
        raise NotImplementedError("live submission not implemented for base connector")
