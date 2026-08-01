# AGENT: ROLE: Server-side entry authority for scalp live commands -- the conjunctive chain every scalp order must survive before the outbox sees it.
# AGENT: ENTRYPOINT: `scalp_entry_error` (pure); `config_sha256`; `verify_certificate`.
# AGENT: PRIMARY INPUTS: command payload, bridge state snapshot, symbol_specs, arming certificate dict.
# AGENT: PRIMARY OUTPUTS: "" (approved) or a refusal reason string.
# AGENT: CALLED BY: `fxstack/runtime/service.py` (submit_scalp_command), tests.
# AGENT: STATE / SIDE EFFECTS: pure computation; certificate IO lives in `fxstack/scalp/validate.py`.
"""Scalp live-entry authority: propose-only clients, a disposing server.

The legacy committee lane guards entries with FinalEntryApproval bound to the
release ceremony (model identity, rollout allowlists). The scalper's lane
keeps the same INVARIANT -- no naked entries, server-side conjunctive
approval -- with evidence suited to its doctrine:

1. ARMING CERTIFICATE: issued exclusively by ``fxstack.scalp.validate`` when
   a signal family clears the pre-registered battery; sha-bound to the exact
   scalp config, symbol-scoped, expiring. No certificate, no live entry.
2. DEMO ATTESTATION: the broker heartbeat must attest a demo account. A real
   account is refused outright in this build -- there is no flag for it.
3. BROKER SPECS: no order without the symbol's published contract truth.
4. PROTECTION: server-side SL and TP prices are required and must sit on the
   correct side of each other for the side traded.
5. CAPS: lots within the broker's min/max, and within the margin-utilization
   ceiling at attested equity.

Every check returns a stable reason string; the first failure wins and is
recorded by the caller. All functions are pure for testability.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

#: Certificates older than this are dead regardless of their expiry field --
#: a stale edge is no edge. Fourteen days forces re-validation cadence.
MAX_CERTIFICATE_AGE_SECS = 14 * 86_400.0

_REQUIRED_CERT_FIELDS = (
    "family",
    "config_sha256",
    "symbols",
    "issued_at_epoch",
    "expires_at_epoch",
    "evidence",
    "cert_sha256",
)


def config_sha256(config_payload: dict[str, Any]) -> str:
    """Canonical sha over the scalp config dict (sorted keys, no floats drift)."""
    canonical = json.dumps(
        {str(k): config_payload[k] for k in sorted(config_payload)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def certificate_body_sha256(cert: dict[str, Any]) -> str:
    body = {k: v for k, v in dict(cert or {}).items() if k != "cert_sha256"}
    return config_sha256(body)


def verify_certificate(
    cert: dict[str, Any] | None,
    *,
    now_epoch: float,
    expected_config_sha256: str,
    symbol: str,
) -> str:
    """"" if the arming certificate authorizes this symbol now, else a reason."""
    if not isinstance(cert, dict) or not cert:
        return "scalp_arming_certificate_missing"
    for key in _REQUIRED_CERT_FIELDS:
        if key not in cert:
            return f"scalp_arming_certificate_malformed:{key}"
    if certificate_body_sha256(cert) != str(cert.get("cert_sha256") or ""):
        return "scalp_arming_certificate_tampered"
    try:
        issued = float(cert["issued_at_epoch"])
        expires = float(cert["expires_at_epoch"])
    except (TypeError, ValueError):
        return "scalp_arming_certificate_malformed:timestamps"
    if not (math.isfinite(issued) and math.isfinite(expires)):
        return "scalp_arming_certificate_malformed:timestamps"
    if now_epoch >= expires:
        return "scalp_arming_certificate_expired"
    if now_epoch - issued > MAX_CERTIFICATE_AGE_SECS:
        return "scalp_arming_certificate_stale"
    if issued > now_epoch + 300.0:
        return "scalp_arming_certificate_future_dated"
    if str(cert.get("config_sha256") or "") != str(expected_config_sha256 or "") or not str(
        expected_config_sha256 or ""
    ):
        return "scalp_arming_certificate_config_mismatch"
    symbols = [str(s).upper() for s in list(cert.get("symbols") or [])]
    if str(symbol).upper() not in symbols:
        return "scalp_arming_certificate_symbol_not_covered"
    if not bool(dict(cert.get("evidence") or {}).get("passed")):
        # A certificate whose own evidence says the battery failed is a
        # forgery attempt or a bug; either way it does not arm anything.
        return "scalp_arming_certificate_evidence_failed"
    return ""


def scalp_entry_error(
    *,
    payload: dict[str, Any],
    state: dict[str, Any],
    specs: dict[str, dict[str, float]],
    certificate: dict[str, Any] | None,
    now_epoch: float,
    margin_utilization_cap: float = 0.25,
) -> str:
    """First failing check of the scalp live-entry chain; "" approves."""
    p = dict(payload or {})
    cmd = str(p.get("cmd") or "").strip().upper()
    if cmd not in {"BUY", "SELL"}:
        return "scalp_command_not_an_entry"
    symbol = str(p.get("symbol") or "").strip().upper()
    if not symbol:
        return "scalp_symbol_missing"

    # 2) Demo attestation -- refuse real accounts outright, no override knob.
    account_mode = str(state.get("broker_account_mode") or "").strip().lower()
    if account_mode != "demo":
        return "scalp_requires_demo_account_attestation"
    if not str(state.get("broker_account_scope") or "").strip():
        return "scalp_account_scope_unattested"

    # 1) Arming certificate (config-bound, symbol-scoped, expiring).
    cert_error = verify_certificate(
        certificate,
        now_epoch=now_epoch,
        expected_config_sha256=str(p.get("scalp_config_sha256") or ""),
        symbol=symbol,
    )
    if cert_error:
        return cert_error

    # 3) Broker contract truth.
    spec = dict((specs or {}).get(symbol) or {})
    if float(spec.get("lot_size") or 0.0) <= 0.0:
        return "scalp_broker_spec_missing"

    # 4) Server-side protection, side-consistent.
    try:
        lots = float(p.get("lots") or 0.0)
        sl = float(p.get("sl_price") or 0.0)
        tp = float(p.get("tp_price") or 0.0)
    except (TypeError, ValueError):
        return "scalp_order_fields_invalid"
    if not all(map(math.isfinite, (lots, sl, tp))) or lots <= 0.0 or sl <= 0.0 or tp <= 0.0:
        return "scalp_protection_required"
    if cmd == "BUY" and not sl < tp:
        return "scalp_protection_sides_inverted"
    if cmd == "SELL" and not tp < sl:
        return "scalp_protection_sides_inverted"

    # 5) Caps: broker lot bounds, then margin ceiling at attested equity.
    min_lot = float(spec.get("min_lot") or 0.01)
    max_lot = float(spec.get("max_lot") or 0.0)
    if lots < min_lot:
        return "scalp_lots_below_broker_minimum"
    if max_lot > 0.0 and lots > max_lot:
        return "scalp_lots_above_broker_maximum"
    margin_required = float(spec.get("margin_required") or 0.0)
    try:
        equity = float(state.get("equity") or 0.0)
    except (TypeError, ValueError):
        equity = 0.0
    if equity <= 0.0:
        return "scalp_equity_unattested"
    if margin_required > 0.0:
        cap = max(0.0, min(1.0, float(margin_utilization_cap)))
        if lots * margin_required > equity * cap:
            return "scalp_margin_cap_exceeded"
    return ""
