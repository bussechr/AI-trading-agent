"""Scalp live-entry authority tests.

Pin the property that matters above all: live entries cannot happen without
the full chain -- arming certificate (issued only by a PASSING battery),
demo attestation, broker specs, protection, caps. Every test drives the pure
authority functions; the service endpoint is a thin binding over them.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from fxstack.scalp.authority import (
    certificate_body_sha256,
    scalp_entry_error,
    verify_certificate,
)
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.validate import (
    ArmingVerdict,
    evaluate_family,
    issue_arming_certificate,
    load_certificate,
    scalp_config_sha256,
)

NOW = 1_754_000_000.0


def _config(tmp_path: Path) -> ScalpConfig:
    cfg = ScalpConfig()
    cfg.data_root = str(tmp_path)
    return cfg


def _passing_verdict() -> ArmingVerdict:
    return ArmingVerdict(
        passed=True, reasons=[], trades=500, mean_r=0.08, ci_lo=0.02,
        ci_hi=0.14, deflated_sharpe=0.97, trials=30, venue="interbank+0.9bps",
    )


def _cert(tmp_path: Path, cfg: ScalpConfig, symbols=("EURUSD",)) -> dict:
    issue_arming_certificate(
        verdict=_passing_verdict(), config=cfg, family="test-family",
        symbols=list(symbols), now_epoch=NOW,
    )
    cert = load_certificate(cfg.data_root)
    assert cert is not None
    return cert


def _entry_payload(cfg: ScalpConfig, **over) -> dict:
    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.22,
        "sl_price": 1.0995,
        "tp_price": 1.1008,
        "scalp_config_sha256": scalp_config_sha256(cfg),
    }
    payload.update(over)
    return payload


def _state(**over) -> dict:
    state = {
        "broker_account_mode": "demo",
        "broker_account_scope": "12345|IG-DEMO|777",
        "equity": 10_000.0,
    }
    state.update(over)
    return state


SPECS = {"EURUSD": {"lot_size": 100_000.0, "min_lot": 0.01, "max_lot": 50.0,
                    "margin_required": 580.0}}


# ------------------------------------------------------------- battery gate

_EPOCH_2024 = 1_705_276_800.0  # 2024-01-15 UTC


def _trades(rs: list[float], *, quarters: int = 6, one_sided: bool = False) -> list[dict]:
    """Spread a R series across quarters with alternating sides."""
    out = []
    for i, r in enumerate(rs):
        out.append(
            {
                "r": float(r),
                "epoch": _EPOCH_2024 + (i % quarters) * 92 * 86_400 + (i // quarters) * 3_600,
                "side": "BUY" if (one_sided or i % 2 == 0) else "SELL",
            }
        )
    return out


def test_failing_battery_cannot_issue_certificate(tmp_path):
    verdict = evaluate_family(
        trades=_trades([-0.1] * 400), trials=30, venue="interbank+0.9bps"
    )
    assert not verdict.passed
    with pytest.raises(PermissionError):
        issue_arming_certificate(
            verdict=verdict, config=_config(tmp_path), family="dead-family",
            symbols=["EURUSD"], now_epoch=NOW,
        )
    assert load_certificate(tmp_path) is None


def test_interbank_raw_costs_never_pass():
    rs = [0.5] * 400  # absurdly good -- and still refused on venue
    verdict = evaluate_family(trades=_trades(rs), trials=1, venue="interbank_raw")
    assert not verdict.passed
    assert any("costs_not_venue_realistic" in r for r in verdict.reasons)


def test_search_size_deflates_the_sharpe():
    # A weak-but-positive series that survives 1 trial must NOT survive the
    # honest trial count of a 30-config search.
    rs = ([0.06] * 220) + ([-0.05] * 180)
    one = evaluate_family(trades=_trades(rs), trials=1, venue="interbank+0.9bps")
    many = evaluate_family(trades=_trades(rs), trials=30, venue="interbank+0.9bps")
    assert many.deflated_sharpe <= one.deflated_sharpe


def test_one_lucky_quarter_cannot_carry_the_family():
    # Flat-to-losing everywhere except one massive quarter: pooled stats look
    # fine, the sliced battery refuses -- this is the anti-trend-overfit gate.
    trades = []
    for i in range(400):
        quarter = i % 6
        r = 1.2 if quarter == 0 else -0.02
        trades.append(
            {
                "r": r,
                "epoch": _EPOCH_2024 + quarter * 92 * 86_400 + (i // 6) * 3_600,
                "side": "BUY" if i % 2 == 0 else "SELL",
            }
        )
    verdict = evaluate_family(trades=trades, trials=1, venue="interbank+0.9bps")
    assert not verdict.passed
    assert any(
        "single_quarter_dependence" in r or "edge_not_repeatable" in r
        for r in verdict.reasons
    )


def test_long_only_profit_is_a_trend_bet_not_an_edge():
    # BUY trades print, SELL trades bleed: that is riding one trend, and the
    # direction slice refuses it even though pooled mean R is positive.
    trades = _trades([0.0] * 400)
    for t in trades:
        t["r"] = 0.30 if t["side"] == "BUY" else -0.10
    verdict = evaluate_family(trades=trades, trials=1, venue="interbank+0.9bps")
    assert not verdict.passed
    assert any("direction_dependent_edge:SELL" in r for r in verdict.reasons)


def test_one_sided_population_is_refused():
    verdict = evaluate_family(
        trades=_trades([0.2] * 400, one_sided=True), trials=1,
        venue="interbank+0.9bps",
    )
    assert not verdict.passed
    assert "one_sided_trade_population" in verdict.reasons


# ------------------------------------------------------- certificate checks


def test_certificate_round_trip_and_tamper_detection(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    sha = scalp_config_sha256(cfg)
    assert verify_certificate(
        cert, now_epoch=NOW + 60, expected_config_sha256=sha, symbol="EURUSD"
    ) == ""
    tampered = dict(cert)
    tampered["symbols"] = ["EURUSD", "GBPUSD"]  # widen scope without re-signing
    assert verify_certificate(
        tampered, now_epoch=NOW + 60, expected_config_sha256=sha, symbol="GBPUSD"
    ) == "scalp_arming_certificate_tampered"
    assert verify_certificate(
        cert, now_epoch=NOW + 60, expected_config_sha256=sha, symbol="GBPUSD"
    ) == "scalp_arming_certificate_symbol_not_covered"
    assert verify_certificate(
        cert, now_epoch=NOW + 8 * 86_400, expected_config_sha256=sha,
        symbol="EURUSD",
    ) == "scalp_arming_certificate_expired"
    assert verify_certificate(
        None, now_epoch=NOW, expected_config_sha256=sha, symbol="EURUSD"
    ) == "scalp_arming_certificate_missing"


def test_config_drift_invalidates_certificate(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    drifted = _config(tmp_path)
    drifted.risk_fraction = 0.02  # someone turned up the risk after arming
    assert verify_certificate(
        cert, now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(drifted), symbol="EURUSD",
    ) == "scalp_arming_certificate_config_mismatch"


def test_certificate_sha_binds_evidence(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    forged = dict(cert)
    forged["evidence"] = dict(cert["evidence"]) | {"mean_r": 9.9}
    assert certificate_body_sha256(forged) != forged["cert_sha256"]


# ------------------------------------------------------------- entry chain


def test_full_chain_passes_with_everything_attested(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    assert scalp_entry_error(
        payload=_entry_payload(cfg), state=_state(), specs=SPECS,
        certificate=cert, now_epoch=NOW + 60,
    ) == ""


def test_real_account_is_refused_outright(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    assert scalp_entry_error(
        payload=_entry_payload(cfg), state=_state(broker_account_mode="real"),
        specs=SPECS, certificate=cert, now_epoch=NOW + 60,
    ) == "scalp_requires_demo_account_attestation"


def test_missing_certificate_blocks_entry(tmp_path):
    cfg = _config(tmp_path)
    assert scalp_entry_error(
        payload=_entry_payload(cfg), state=_state(), specs=SPECS,
        certificate=None, now_epoch=NOW,
    ) == "scalp_arming_certificate_missing"


def test_missing_spec_blocks_entry(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    assert scalp_entry_error(
        payload=_entry_payload(cfg), state=_state(), specs={},
        certificate=cert, now_epoch=NOW + 60,
    ) == "scalp_broker_spec_missing"


def test_naked_or_inverted_protection_blocks_entry(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    naked = _entry_payload(cfg, sl_price=0.0)
    assert scalp_entry_error(
        payload=naked, state=_state(), specs=SPECS, certificate=cert,
        now_epoch=NOW + 60,
    ) == "scalp_protection_required"
    inverted = _entry_payload(cfg, sl_price=1.1010, tp_price=1.0990)
    assert scalp_entry_error(
        payload=inverted, state=_state(), specs=SPECS, certificate=cert,
        now_epoch=NOW + 60,
    ) == "scalp_protection_sides_inverted"


def test_margin_cap_blocks_oversized_entry(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    big = _entry_payload(cfg, lots=10.0)  # 10 * 580 = 5800 > 10000 * 0.25
    assert scalp_entry_error(
        payload=big, state=_state(), specs=SPECS, certificate=cert,
        now_epoch=NOW + 60,
    ) == "scalp_margin_cap_exceeded"


def test_unattested_equity_blocks_entry(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    assert scalp_entry_error(
        payload=_entry_payload(cfg), state=_state(equity=0.0), specs=SPECS,
        certificate=cert, now_epoch=NOW + 60,
    ) == "scalp_equity_unattested"


# ---------------------------------------------------------------- loop gate


def test_live_loop_refuses_startup_without_certificate(tmp_path, monkeypatch):
    from fxstack.scalp.loop import ScalpLoop

    cfg = _config(tmp_path)
    cfg.mode = "live"
    with pytest.raises(SystemExit, match="live mode refused"):
        ScalpLoop(cfg)
