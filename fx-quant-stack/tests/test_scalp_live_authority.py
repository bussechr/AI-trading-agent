"""Scalp live-entry authority tests.

Pin the property that matters above all: live entries cannot happen without
the full chain -- arming certificate (issued only by a PASSING battery),
demo attestation, broker specs, protection, caps. Every test drives the pure
authority functions; the service endpoint is a thin binding over them.
"""

from __future__ import annotations

import dataclasses
import json
from functools import lru_cache
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fxstack.scalp.authority import (
    ATTEMPT_LEDGER_SCHEMA,
    CANONICAL_FX_SYMBOLS,
    PREREGISTRATION_SCHEMA,
    SUPPORTED_VENUE_COST_CONTRACT,
    TRADE_EVIDENCE_SCHEMA,
    VENUE_COST_PROVENANCE_SCHEMA,
    WIN_DEFINITION,
    certificate_body_sha256,
    scalp_entry_error as _scalp_entry_error,
    verify_certificate as _verify_certificate,
)
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.validate import (
    ArmingVerdict,
    evaluate_family,
    issue_arming_certificate as _issue_arming_certificate,
    load_certificate as _load_certificate,
    load_revoked_certificate_ids,
    main as validation_main,
    revoke_arming_certificate,
    scalp_config_sha256,
    trades_from_ledger,
)

NOW = 1_754_000_000.0
AUTHORITY_SIGNING_KEY = Ed25519PrivateKey.generate()
AUTHORITY_VERIFY_KEY = AUTHORITY_SIGNING_KEY.public_key()


def issue_arming_certificate(*args, **kwargs):
    kwargs.setdefault("authority_signing_key", AUTHORITY_SIGNING_KEY)
    return _issue_arming_certificate(*args, **kwargs)


def load_certificate(*args, **kwargs):
    kwargs.setdefault("authority_verify_key", AUTHORITY_VERIFY_KEY)
    return _load_certificate(*args, **kwargs)


def verify_certificate(*args, **kwargs):
    kwargs.setdefault("authority_verify_key", AUTHORITY_VERIFY_KEY)
    return _verify_certificate(*args, **kwargs)


def scalp_entry_error(*args, **kwargs):
    certificate = kwargs.get("certificate")
    payload = dict(kwargs.get("payload") or {})
    kwargs.setdefault(
        "expected_config_sha256",
        str((certificate or {}).get("config_sha256") or payload.get("scalp_config_sha256") or ""),
    )
    kwargs.setdefault("authority_verify_key", AUTHORITY_VERIFY_KEY)
    return _scalp_entry_error(*args, **kwargs)


def _write_signing_key(path: Path) -> None:
    path.write_bytes(
        AUTHORITY_SIGNING_KEY.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )


def _write_verify_key(path: Path) -> None:
    path.write_bytes(
        AUTHORITY_VERIFY_KEY.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )


def _config(tmp_path: Path) -> ScalpConfig:
    cfg = ScalpConfig()
    cfg.data_root = str(tmp_path)
    return cfg


def _cert(tmp_path: Path, cfg: ScalpConfig) -> dict:
    issue_arming_certificate(
        verdict=_passing_verdict(), config=cfg, family="test-family",
        symbols=list(CANONICAL_FX_SYMBOLS), now_epoch=NOW,
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


def _required_cell_trades(
    symbols=("EURUSD", "GBPUSD"), *, losses_per_cell: int = 9
) -> list[dict]:
    """Ninety independent trades per cell with configurable losing trades."""
    out: list[dict] = []
    seen = {
        (str(symbol).upper(), side): 0
        for symbol in symbols
        for side in ("BUY", "SELL")
    }
    for quarter in range(6):
        for day in range(15):
            for symbol_index, raw_symbol in enumerate(symbols):
                symbol = str(raw_symbol).upper()
                for side_index, side in enumerate(("BUY", "SELL")):
                    key = (symbol, side)
                    cell_index = seen[key]
                    seen[key] += 1
                    out.append(
                        {
                            "symbol": symbol,
                            "side": side,
                            "r": (
                                -1.0
                                if cell_index < losses_per_cell
                                else (0.95 if cell_index % 2 else 1.0)
                            ),
                            "epoch": _EPOCH_2024
                            + quarter * 92 * 86_400
                            + day * 86_400
                            + (symbol_index * 2 + side_index) * 60,
                            "target_predeclared": True,
                            "full_target_hit_first": cell_index >= losses_per_cell,
                            "initial_risk_bps": 10.0,
                            "initial_target_bps": 10.0,
                            "initial_reward_risk": 1.0,
                        }
                    )
    return out


ARTIFACT_SHA256 = {
    "trade_ledger": "1" * 64,
    "venue_cost_artifact": "2" * 64,
    "preregistration": "3" * 64,
    "attempt_ledger": "4" * 64,
}


def _venue_cost_provenance() -> dict:
    return {
        "schema": VENUE_COST_PROVENANCE_SCHEMA,
        **SUPPORTED_VENUE_COST_CONTRACT,
        "symbols": {
            symbol: {
                "measured": True,
                "spread_samples": 500,
                "slippage_samples": 500,
                "stressed_round_trip_cost_bps": 2.0,
            }
            for symbol in CANONICAL_FX_SYMBOLS
        },
    }


def _preregistration() -> dict:
    return {
        "schema": PREREGISTRATION_SCHEMA,
        "sealed_before_evaluation": True,
        "canonical_fx_symbols": list(CANONICAL_FX_SYMBOLS),
        "directions": ["BUY", "SELL"],
        "win_definition": WIN_DEFINITION,
        "fixed_initial_reward_risk": 1.0,
        "min_target_stressed_cost_multiple": 4.0,
        "max_trades_per_cell_utc_day": 1,
    }


def _attempt_ledger(hypotheses=("fixed-family-v1",)) -> dict:
    return {"schema": ATTEMPT_LEDGER_SCHEMA, "hypotheses": list(hypotheses)}


def _arming_evidence_kwargs(hypotheses=("fixed-family-v1",)) -> dict:
    return {
        "artifact_sha256": dict(ARTIFACT_SHA256),
        "venue_cost_provenance": _venue_cost_provenance(),
        "preregistration": _preregistration(),
        "attempt_ledger": _attempt_ledger(hypotheses),
    }


@lru_cache(maxsize=1)
def _passing_verdict() -> ArmingVerdict:
    verdict = evaluate_family(
        trades=_required_cell_trades(CANONICAL_FX_SYMBOLS, losses_per_cell=0),
        trials=1,
        venue="ignored-client-label",
        required_symbols=list(CANONICAL_FX_SYMBOLS),
        min_cell_trades=90,
        min_cell_independent_days=60,
        min_cell_win_rate=0.90,
        attempted_hypotheses=["fixed-family-v1"],
        **_arming_evidence_kwargs(),
    )
    assert verdict.passed, verdict.reasons
    return verdict


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


def test_free_form_venue_label_is_not_cost_provenance():
    evidence = _arming_evidence_kwargs()
    evidence.pop("venue_cost_provenance")
    verdict = evaluate_family(
        trades=_required_cell_trades(),
        trials=1,
        venue="ig_demo",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_win_rate=0.90,
        attempted_hypotheses=["fixed-family-v1"],
        **evidence,
    )
    assert not verdict.passed
    assert "venue_cost_provenance_schema_invalid" in verdict.reasons


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
    # Spread over many distinct days so the day-floors pass and the QUARTER
    # rule is the thing under test.
    trades = []
    for quarter in range(6):
        for day in range(20):
            for k in range(4):
                trades.append(
                    {
                        "r": 1.2 if quarter == 0 else -0.02,
                        "epoch": _EPOCH_2024 + quarter * 92 * 86_400
                        + day * 86_400 + k * 3_600,
                        "side": "BUY" if k % 2 == 0 else "SELL",
                    }
                )
    verdict = evaluate_family(trades=trades, trials=1, venue="interbank+0.9bps")
    assert not verdict.passed
    assert verdict.independent_days >= 60
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


def test_clustered_trades_cannot_masquerade_as_independent_evidence():
    """The adversarial panel's kill: 42 trades on 6 days scored as n=42.

    Seven correlated pairs firing on one news minute is ONE observation, not
    seven. The clustered bootstrap and the independent-day floor must both
    refuse it, however good the pooled mean looks.
    """
    trades = []
    for day in range(6):
        for pair in range(7):
            trades.append(
                {
                    "r": 0.9 if day % 2 else 0.7,  # uniformly excellent
                    "epoch": _EPOCH_2024 + day * 30 * 86_400 + pair * 60,
                    "side": "BUY" if pair % 2 else "SELL",
                }
            )
    verdict = evaluate_family(trades=trades, trials=1, venue="interbank+0.9bps")
    assert not verdict.passed
    assert verdict.independent_days == 6
    assert any("insufficient_independent_days" in r for r in verdict.reasons)


def test_clustered_bootstrap_is_wider_than_the_iid_one():
    from fxstack.scalp.backtest import bootstrap_ci_mean
    from fxstack.scalp.validate import clustered_bootstrap_ci_mean

    # Same 200 trades; all outcomes within a day are identical, so the real
    # evidence is 10 days, not 200 trades.
    trades = []
    for day in range(10):
        value = 0.5 if day % 2 else -0.3
        for k in range(20):
            trades.append(
                {"r": value, "epoch": _EPOCH_2024 + day * 86_400 + k * 60, "side": "BUY"}
            )
    iid_lo, iid_hi = bootstrap_ci_mean([t["r"] for t in trades])
    clus_lo, clus_hi = clustered_bootstrap_ci_mean(trades)
    assert (clus_hi - clus_lo) > (iid_hi - iid_lo)


def test_direction_evidence_must_be_independent_too():
    # BUY seen on many days, SELL on a single day: "both sides positive" is
    # satisfied numerically but the SELL side has no independent evidence.
    trades = []
    for day in range(80):
        trades.append(
            {"r": 0.2, "epoch": _EPOCH_2024 + day * 86_400, "side": "BUY"}
        )
    for k in range(40):
        trades.append(
            {"r": 0.9, "epoch": _EPOCH_2024 + 5 * 86_400 + k * 60, "side": "SELL"}
        )
    verdict = evaluate_family(trades=trades, trials=1, venue="interbank+0.9bps")
    assert not verdict.passed
    assert any("direction_evidence_too_thin:SELL" in r for r in verdict.reasons)


def test_one_sided_population_is_refused():
    verdict = evaluate_family(
        trades=_trades([0.2] * 400, one_sided=True), trials=1,
        venue="interbank+0.9bps",
    )
    assert not verdict.passed
    assert "one_sided_trade_population" in verdict.reasons


def test_ledger_trades_preserve_normalized_symbol(tmp_path):
    ledger = tmp_path / "ledger_20240115.jsonl"
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "fill",
                        "symbol": " eurusd ",
                        "side": "SELL",
                        "pnl_r": 0.25,
                        "exit_epoch": _EPOCH_2024,
                        "exit_reason": "time_stop",
                        "meta": {
                            "trade_evidence_schema": TRADE_EVIDENCE_SCHEMA,
                            "target_predeclared": True,
                            "initial_risk_bps": 10.0,
                            "initial_target_bps": 10.0,
                        },
                    }
                ),
                json.dumps({"kind": "decision", "symbol": "GBPUSD"}),
                "not-json",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert trades_from_ledger(tmp_path) == [
        {
            "r": 0.25,
            "epoch": _EPOCH_2024,
            "side": "SELL",
            "symbol": "EURUSD",
            "target_predeclared": True,
            "full_target_hit_first": False,
            "initial_risk_bps": 10.0,
            "initial_target_bps": 10.0,
            "initial_reward_risk": 1.0,
        }
    ]


def test_strong_cell_win_rate_clears_simultaneous_ninety_percent_bound():
    verdict = _passing_verdict()
    assert verdict.passed, verdict.reasons
    assert verdict.required_symbols == list(CANONICAL_FX_SYMBOLS)
    assert verdict.passing_symbols == list(CANONICAL_FX_SYMBOLS)
    assert verdict.min_cell_win_rate == pytest.approx(0.90)
    assert (
        verdict.win_rate_confidence["method"]
        == "wilson_one_sided_bonferroni_full_target_hit_first_per_cell_utc_day"
    )
    assert verdict.win_rate_confidence["family_confidence"] == pytest.approx(0.95)
    assert verdict.win_rate_confidence["family_cells"] == 36
    assert verdict.win_rate_confidence["cell_confidence"] == pytest.approx(
        1.0 - 0.05 / 36
    )
    for symbol in verdict.required_symbols:
        for side in ("BUY", "SELL"):
            cell = verdict.cell_stats[symbol][side]
            assert cell["passed"]
            assert cell["trades"] == 90
            assert cell["independent_days"] == 90
            assert cell["mean_r"] > 0.0
            assert cell["ci_lo"] > 0.0
            assert cell["win_rate"] == pytest.approx(1.0)
            assert cell["win_rate_ci_lo"] >= 0.90
            assert cell["win_rate_confidence"] == verdict.win_rate_confidence


def test_observed_ninety_percent_is_not_proof_of_ninety_percent():
    verdict = evaluate_family(
        trades=_required_cell_trades(),
        trials=1,
        venue="interbank+0.9bps",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=90,
        min_cell_independent_days=60,
        min_cell_win_rate=0.90,
        attempted_hypotheses=["fixed-family-v1"],
        **_arming_evidence_kwargs(),
    )
    assert not verdict.passed
    for symbol in verdict.required_symbols:
        for side in ("BUY", "SELL"):
            cell = verdict.cell_stats[symbol][side]
            assert cell["win_rate"] == pytest.approx(0.90)
            assert cell["win_rate_ci_lo"] < 0.90
            assert not cell["passed"]
            assert any(
                reason.startswith(
                    f"cell_win_rate_ci_lower_bound_below_threshold:{symbol}:{side}:"
                )
                for reason in verdict.reasons
            )
    assert verdict.passing_symbols == []


def test_profitable_non_target_exit_is_not_a_win():
    trades = _required_cell_trades()
    trade = next(
        row
        for row in trades
        if row["symbol"] == "EURUSD"
        and row["side"] == "BUY"
        and row["full_target_hit_first"]
    )
    trade["r"] = 0.75
    trade["full_target_hit_first"] = False
    verdict = evaluate_family(
        trades=trades,
        trials=1,
        venue="ignored",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=90,
        min_cell_independent_days=60,
        min_cell_win_rate=0.90,
        attempted_hypotheses=["fixed-family-v1"],
        **_arming_evidence_kwargs(),
    )
    cell = verdict.cell_stats["EURUSD"]["BUY"]
    assert cell["wins"] == 80
    assert cell["win_definition"] == WIN_DEFINITION


def test_target_must_cover_four_times_stressed_round_trip_cost():
    evidence = _arming_evidence_kwargs()
    provenance = evidence["venue_cost_provenance"]
    for row in provenance["symbols"].values():
        row["stressed_round_trip_cost_bps"] = 3.0
    verdict = evaluate_family(
        trades=_required_cell_trades(),
        trials=1,
        venue="ignored",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=90,
        min_cell_independent_days=60,
        min_cell_win_rate=0.90,
        attempted_hypotheses=["fixed-family-v1"],
        **evidence,
    )
    assert any(
        "target_below_stressed_cost_multiple" in reason
        for reason in verdict.reasons
    )


def test_reward_risk_must_be_fixed_and_preregistered():
    trades = _required_cell_trades()
    trades[0]["initial_target_bps"] = 12.0
    trades[0]["initial_reward_risk"] = 1.2
    verdict = evaluate_family(
        trades=trades,
        trials=1,
        venue="ignored",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=90,
        min_cell_independent_days=60,
        min_cell_win_rate=0.90,
        attempted_hypotheses=["fixed-family-v1"],
        **_arming_evidence_kwargs(),
    )
    assert "invalid_trade:0:reward_risk_not_preregistered" in verdict.reasons


@pytest.mark.parametrize("artifact", list(ARTIFACT_SHA256))
def test_every_evidence_artifact_identity_is_mandatory(tmp_path, artifact):
    identities = dict(ARTIFACT_SHA256)
    identities[artifact] = ""
    forged = dataclasses.replace(
        _passing_verdict(), artifact_sha256=identities, passed=True, reasons=[]
    )
    with pytest.raises(PermissionError, match="artifact_sha256_invalid"):
        issue_arming_certificate(
            verdict=forged,
            config=_config(tmp_path),
            family="unbound-evidence",
            symbols=list(CANONICAL_FX_SYMBOLS),
            now_epoch=NOW,
        )


def test_unsupported_venue_provenance_cannot_issue(tmp_path):
    provenance = _venue_cost_provenance()
    provenance["spread_source"] = "self_reported_average_spread"
    forged = dataclasses.replace(
        _passing_verdict(),
        venue_cost_provenance=provenance,
        passed=True,
        reasons=[],
    )
    with pytest.raises(PermissionError, match="venue_cost_provenance_unsupported"):
        issue_arming_certificate(
            verdict=forged,
            config=_config(tmp_path),
            family="unsupported-costs",
            symbols=list(CANONICAL_FX_SYMBOLS),
            now_epoch=NOW,
        )


def test_required_cells_apply_trade_and_independent_day_floors_per_side():
    trades = _required_cell_trades()
    removed = False
    thinned: list[dict] = []
    for trade in trades:
        if not removed and trade["symbol"] == "GBPUSD" and trade["side"] == "SELL":
            removed = True
            continue
        if trade["symbol"] == "EURUSD" and trade["side"] == "BUY":
            trade["epoch"] = _EPOCH_2024
        thinned.append(trade)
    verdict = evaluate_family(
        trades=thinned,
        trials=1,
        venue="interbank+0.9bps",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=90,
        min_cell_independent_days=60,
        **_arming_evidence_kwargs(),
    )
    assert not verdict.passed
    assert "cell_insufficient_trades:GBPUSD:SELL:89<90" in verdict.reasons
    assert "cell_insufficient_independent_days:EURUSD:BUY:1<60" in verdict.reasons


def test_required_cell_needs_positive_mean_and_clustered_ci():
    trades = _required_cell_trades()
    for trade in trades:
        if trade["symbol"] == "EURUSD" and trade["side"] == "BUY":
            trade["r"] = -0.05
    verdict = evaluate_family(
        trades=trades,
        trials=1,
        venue="interbank+0.9bps",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=90,
        min_cell_independent_days=60,
        **_arming_evidence_kwargs(),
    )
    assert not verdict.passed
    assert any(
        reason.startswith("cell_mean_not_positive:EURUSD:BUY:")
        for reason in verdict.reasons
    )
    assert any(
        reason.startswith("cell_ci_lower_bound_not_positive:EURUSD:BUY:")
        for reason in verdict.reasons
    )


def test_certificate_scope_cannot_exceed_passing_cell_evidence(tmp_path):
    verdict = _passing_verdict()
    cfg = _config(tmp_path)
    with pytest.raises(PermissionError, match="canonical_symbol_scope_required"):
        issue_arming_certificate(
            verdict=verdict,
            config=cfg,
            family="cell-covered",
            symbols=["EURUSD", "USDJPY"],
            now_epoch=NOW,
        )
    assert load_certificate(tmp_path) is None

    issue_arming_certificate(
        verdict=verdict,
        config=cfg,
        family="cell-covered",
        symbols=list(CANONICAL_FX_SYMBOLS),
        now_epoch=NOW,
    )
    cert = load_certificate(tmp_path)
    assert cert is not None
    assert verify_certificate(
        cert,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ) == ""
    weakened = json.loads(json.dumps(cert))
    weakened["evidence"]["cell_stats"]["EURUSD"]["BUY"]["win_rate_ci_lo"] = 0.10
    weakened["cert_sha256"] = certificate_body_sha256(weakened)
    assert verify_certificate(
        weakened,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ).startswith("scalp_arming_certificate_unauthenticated:")
    widened = dict(cert)
    widened["symbols"] = [*cert["symbols"], "BTCUSD"]
    widened["cert_sha256"] = certificate_body_sha256(widened)
    assert verify_certificate(
        widened,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="BTCUSD",
    ).startswith("scalp_arming_certificate_unauthenticated:")


def test_certificate_requires_explicit_required_symbol_evaluation(tmp_path):
    generic_verdict = dataclasses.replace(
        _passing_verdict(),
        required_symbols=[],
        cell_stats={},
        passing_symbols=[],
    )
    with pytest.raises(PermissionError):
        issue_arming_certificate(
            verdict=generic_verdict,
            config=_config(tmp_path),
            family="generic-only",
            symbols=list(CANONICAL_FX_SYMBOLS),
            now_epoch=NOW,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("trades", 0),
        ("independent_days", 0),
        ("mean_r", float("nan")),
        ("ci_lo", float("inf")),
        ("deflated_sharpe", float("nan")),
        ("deflated_sharpe", 0.0),
    ],
)
def test_passed_flag_cannot_bypass_semantically_invalid_evidence(
    tmp_path, field, value
):
    cfg = _config(tmp_path)
    forged_verdict = dataclasses.replace(
        _passing_verdict(), passed=True, reasons=[], **{field: value}
    )
    with pytest.raises(PermissionError):
        issue_arming_certificate(
            verdict=forged_verdict,
            config=cfg,
            family="forged",
            symbols=list(CANONICAL_FX_SYMBOLS),
            now_epoch=NOW,
        )

    cert = _cert(tmp_path, cfg)
    forged_cert = json.loads(json.dumps(cert))
    forged_cert["evidence"]["passed"] = True
    forged_cert["evidence"]["reasons"] = []
    forged_cert["evidence"][field] = value
    forged_cert["cert_sha256"] = certificate_body_sha256(forged_cert)
    error = verify_certificate(
        forged_cert,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    )
    assert error.startswith("scalp_arming_certificate_unauthenticated:")


def test_coherent_self_rehashed_zero_trade_certificate_is_refused(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    forged = json.loads(json.dumps(cert))
    evidence = forged["evidence"]
    evidence.update(
        {
            "passed": True,
            "reasons": [],
            "trades": 0,
            "independent_days": 0,
            "mean_r": 0.0,
            "ci_lo": 0.0,
            "ci_hi": 0.0,
            "deflated_sharpe": 1.0,
            "quarter_stats": {},
            "side_means": {"BUY": 0.0, "SELL": 0.0},
        }
    )
    for cells in evidence["cell_stats"].values():
        for side in ("BUY", "SELL"):
            cells[side].update(
                {
                    "passed": True,
                    "reasons": [],
                    "trades": 0,
                    "independent_days": 0,
                    "wins": 0,
                    "win_rate": 0.0,
                    "winning_days": 0,
                    "day_win_rate": 0.0,
                    "win_rate_ci_lo": 0.0,
                    "day_stats": {},
                    "mean_r": 0.0,
                    "ci_lo": 0.0,
                    "ci_hi": 0.0,
                }
            )
    forged["cert_sha256"] = certificate_body_sha256(forged)
    error = verify_certificate(
        forged,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    )
    assert error.startswith("scalp_arming_certificate_unauthenticated:")


@pytest.mark.parametrize(
    "updates",
    [
        {"min_cell_win_rate": None},
        {"min_cell_win_rate": 0.89},
        {"win_rate_family_confidence": 0.90},
        {"hypothesis_audit": {}},
    ],
)
def test_arming_policy_or_hypothesis_audit_cannot_be_removed(tmp_path, updates):
    cfg = _config(tmp_path)
    forged_verdict = dataclasses.replace(
        _passing_verdict(), passed=True, reasons=[], **updates
    )
    with pytest.raises(PermissionError):
        issue_arming_certificate(
            verdict=forged_verdict,
            config=cfg,
            family="weakened",
            symbols=list(CANONICAL_FX_SYMBOLS),
            now_epoch=NOW,
        )

    cert = _cert(tmp_path, cfg)
    forged_cert = json.loads(json.dumps(cert))
    forged_cert["evidence"].update(updates)
    forged_cert["evidence"]["passed"] = True
    forged_cert["evidence"]["reasons"] = []
    forged_cert["cert_sha256"] = certificate_body_sha256(forged_cert)
    error = verify_certificate(
        forged_cert,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    )
    assert error.startswith("scalp_arming_certificate_unauthenticated:")


def test_duplicated_within_day_wins_do_not_inflate_independent_evidence():
    base = _required_cell_trades(losses_per_cell=1)
    cell_seen: dict[tuple[str, str], int] = {}
    duplicated: list[dict] = []
    for trade in base:
        key = (trade["symbol"], trade["side"])
        cell_index = cell_seen.get(key, 0)
        cell_seen[key] = cell_index + 1
        if cell_index >= 10:
            continue
        copies = 100 if trade["r"] > 0.0 else 1
        duplicated.extend(dict(trade) for _ in range(copies))
    verdict = evaluate_family(
        trades=duplicated,
        trials=1,
        venue="interbank+0.9bps",
        required_symbols=["EURUSD", "GBPUSD"],
        min_cell_trades=30,
        min_cell_independent_days=60,
        min_cell_win_rate=0.90,
        attempted_hypotheses=["duplicate-attack"],
        **_arming_evidence_kwargs(("duplicate-attack",)),
    )
    assert not verdict.passed
    for symbol in verdict.required_symbols:
        for side in ("BUY", "SELL"):
            cell = verdict.cell_stats[symbol][side]
            assert cell["trades"] == 901
            assert cell["win_rate"] > 0.99
            assert cell["independent_days"] == 10
            assert cell["winning_days"] == 0
            assert cell["win_rate_ci_lo"] < 0.90
    assert any(
        reason.startswith("multiple_trades_per_cell_utc_day:")
        for reason in verdict.reasons
    )


def test_malformed_nonfinite_ledger_rows_refuse_without_raising(tmp_path):
    ledger = tmp_path / "ledger_20240115.jsonl"
    ledger.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "kind": "fill",
                        "symbol": "EURUSD",
                        "side": "BUY",
                        "pnl_r": float("nan"),
                        "exit_epoch": _EPOCH_2024,
                    }
                ),
                json.dumps(
                    {
                        "kind": "fill",
                        "symbol": "EURUSD",
                        "side": "BUY",
                        "pnl_r": 0.2,
                        "exit_epoch": "not-a-time",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )
    errors: list[str] = []
    assert trades_from_ledger(tmp_path, errors=errors) == []
    assert len(errors) == 2


def test_failed_issue_revalidation_quarantines_existing_certificate(tmp_path):
    cfg = _config(tmp_path)
    _cert(tmp_path, cfg)
    trades_path = tmp_path / "failed_trades.json"
    hypotheses_path = tmp_path / "hypotheses.json"
    venue_cost_path = tmp_path / "venue_cost.json"
    preregistration_path = tmp_path / "preregistration.json"
    attempt_ledger_path = tmp_path / "attempt_ledger.json"
    signing_key_path = tmp_path / "arming-signing-key.pem"
    _write_signing_key(signing_key_path)
    trades_path.write_text(
        json.dumps(
            {
                "trades": _required_cell_trades(
                    CANONICAL_FX_SYMBOLS, losses_per_cell=90
                ),
            }
        ),
        encoding="utf-8",
    )
    hypotheses_path.write_text(json.dumps(["failed-family"]), encoding="utf-8")
    venue_cost_path.write_text(
        json.dumps(_venue_cost_provenance()), encoding="utf-8"
    )
    preregistration_path.write_text(
        json.dumps(_preregistration()), encoding="utf-8"
    )
    attempt_ledger_path.write_text(
        json.dumps(_attempt_ledger(("failed-family",))), encoding="utf-8"
    )
    result = validation_main(
        [
            "--trades-json",
            str(trades_path),
            "--trials",
            "1",
            "--family",
            "failed-family",
            "--symbols",
            ",".join(CANONICAL_FX_SYMBOLS),
            "--venue-cost-json",
            str(venue_cost_path),
            "--preregistration-json",
            str(preregistration_path),
            "--attempt-ledger-json",
            str(attempt_ledger_path),
            "--hypotheses-json",
            str(hypotheses_path),
            "--data-root",
            str(tmp_path),
            "--signing-key-file",
            str(signing_key_path),
            "--issue",
        ]
    )
    assert result == 1
    assert load_certificate(tmp_path) is None
    quarantined = list(tmp_path.glob("arming_certificate.revoked.*.*.json"))
    assert quarantined
    marker = json.loads(
        (tmp_path / "arming_certificate.revoked.json").read_text(encoding="utf-8")
    )
    assert marker["prior_certificate_sha256"]
    assert marker["quarantined_filename"] == quarantined[0].name


# ------------------------------------------------------- certificate checks


def test_certificate_round_trip_and_tamper_detection(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    sha = scalp_config_sha256(cfg)
    assert verify_certificate(
        cert, now_epoch=NOW + 60, expected_config_sha256=sha, symbol="EURUSD"
    ) == ""
    tampered = dict(cert)
    tampered["symbols"] = ["EURUSD", "GBPUSD"]  # shrink scope without re-signing
    assert verify_certificate(
        tampered, now_epoch=NOW + 60, expected_config_sha256=sha, symbol="GBPUSD"
    ) == "scalp_arming_certificate_tampered"
    assert verify_certificate(
        cert, now_epoch=NOW + 60, expected_config_sha256=sha, symbol="BTCUSD"
    ) == "scalp_arming_certificate_symbol_not_covered"
    assert verify_certificate(
        cert, now_epoch=NOW + 8 * 86_400, expected_config_sha256=sha,
        symbol="EURUSD",
    ) == "scalp_arming_certificate_expired"
    assert verify_certificate(
        None, now_epoch=NOW, expected_config_sha256=sha, symbol="EURUSD"
    ) == "scalp_arming_certificate_missing"


def test_self_rehash_cannot_renew_expired_certificate(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    renewed = json.loads(json.dumps(cert))
    renewed["issued_at_epoch"] = NOW + 30 * 86_400
    renewed["expires_at_epoch"] = renewed["issued_at_epoch"] + 7 * 86_400
    renewed["cert_sha256"] = certificate_body_sha256(renewed)
    error = verify_certificate(
        renewed,
        now_epoch=renewed["issued_at_epoch"] + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    )
    assert error == "scalp_arming_certificate_unauthenticated:signature_invalid"


def test_missing_or_wrong_verify_key_fails_closed(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    raw_without_authority_state = json.loads(
        (tmp_path / "arming_certificate.json").read_text(encoding="utf-8")
    )
    assert verify_certificate(
        raw_without_authority_state,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ) == "scalp_arming_revocation_state_invalid"
    assert _verify_certificate(
        cert,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ) == "scalp_arming_verify_key_unavailable"
    wrong_key = Ed25519PrivateKey.generate().public_key()
    assert _verify_certificate(
        cert,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
        authority_verify_key=wrong_key,
    ) == "scalp_arming_certificate_unauthenticated:signing_key_mismatch"
    assert _load_certificate(tmp_path) is None


def test_issuance_without_operator_signing_key_fails_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("FXSCALP_ARMING_SIGNING_KEY_FILE", raising=False)
    with pytest.raises(PermissionError, match="signing_key_unavailable"):
        _issue_arming_certificate(
            verdict=_passing_verdict(),
            config=_config(tmp_path),
            family="unsigned",
            symbols=list(CANONICAL_FX_SYMBOLS),
            now_epoch=NOW,
        )


def test_operator_private_and_runtime_public_key_files_interoperate(
    tmp_path, monkeypatch
):
    signing_path = tmp_path / "signing.pem"
    verify_path = tmp_path / "verify.pem"
    _write_signing_key(signing_path)
    _write_verify_key(verify_path)
    monkeypatch.setenv("FXSCALP_ARMING_SIGNING_KEY_FILE", str(signing_path))
    monkeypatch.setenv("FXSCALP_ARMING_VERIFY_KEY_FILE", str(verify_path))
    cfg = _config(tmp_path)
    _issue_arming_certificate(
        verdict=_passing_verdict(),
        config=cfg,
        family="file-keyed",
        symbols=list(CANONICAL_FX_SYMBOLS),
        now_epoch=NOW,
    )
    cert = _load_certificate(tmp_path)
    assert cert is not None
    assert _verify_certificate(
        cert,
        now_epoch=NOW + 60,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ) == ""


def test_signed_revocation_blocks_restored_quarantined_identity(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    active = tmp_path / "arming_certificate.json"
    original_bytes = active.read_bytes()
    revoked_id = cert["cert_sha256"]
    quarantined = revoke_arming_certificate(
        tmp_path,
        reason="failed_revalidation",
        now_epoch=NOW + 120,
        authority_signing_key=AUTHORITY_SIGNING_KEY,
    )
    assert quarantined is not None
    revoked_ids, error = load_revoked_certificate_ids(
        tmp_path, authority_verify_key=AUTHORITY_VERIFY_KEY
    )
    assert error == ""
    assert revoked_id in revoked_ids
    assert load_certificate(tmp_path) is None

    # Restoring the exact previously valid bytes must not resurrect authority.
    active.write_bytes(original_bytes)
    assert load_certificate(tmp_path) is None
    assert verify_certificate(
        cert,
        now_epoch=NOW + 180,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
        revoked_certificate_ids=revoked_ids,
    ) == "scalp_arming_certificate_revoked"


def test_corrupt_revocation_registry_fails_closed(tmp_path):
    cfg = _config(tmp_path)
    _cert(tmp_path, cfg)
    marker = tmp_path / "arming_certificate.revoked.json"
    marker.write_text('{"revoked_certificate_ids": []}', encoding="utf-8")
    assert load_certificate(tmp_path) is None


def test_deleted_authority_registry_cannot_restore_certificate(tmp_path):
    cfg = _config(tmp_path)
    _cert(tmp_path, cfg)
    (tmp_path / "arming_certificate.revoked.json").unlink()
    assert load_certificate(tmp_path) is None


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


def test_nonfinite_or_malformed_certificate_fields_return_refusal(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    bad_expiry = json.loads(json.dumps(cert))
    bad_expiry["expires_at_epoch"] = float("nan")
    bad_expiry["cert_sha256"] = certificate_body_sha256(bad_expiry)
    assert verify_certificate(
        bad_expiry,
        now_epoch=NOW,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ).startswith("scalp_arming_certificate_unauthenticated:")
    assert verify_certificate(
        cert,
        now_epoch=float("nan"),
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    ) == "scalp_now_epoch_invalid"

    malformed_evidence = json.loads(json.dumps(cert))
    malformed_evidence["evidence"] = ["not", "an", "object"]
    malformed_evidence["cert_sha256"] = certificate_body_sha256(malformed_evidence)
    error = verify_certificate(
        malformed_evidence,
        now_epoch=NOW,
        expected_config_sha256=scalp_config_sha256(cfg),
        symbol="EURUSD",
    )
    assert error.startswith("scalp_arming_certificate_unauthenticated:")


# ------------------------------------------------------------- entry chain


def test_runtime_scalp_ingress_is_fail_closed_until_authority_is_production_bound():
    from fxstack.runtime.service import RuntimeService

    service = RuntimeService.__new__(RuntimeService)
    service._submit_command = lambda *_args, **_kwargs: pytest.fail(
        "disabled scalp ingress reached the command queue"
    )
    response, status = service.submit_scalp_command(
        {
            "cmd": "BUY",
            "symbol": "EURUSD",
            "scalp_data_root": "client-selected",
            "scalp_config_sha256": "f" * 64,
        }
    )
    assert status == 403
    assert response == {
        "status": "forbidden",
        "error": "scalp_live_ingress_disabled_unvalidated_authority",
    }


def test_legacy_live_executor_refuses_locally_without_bridge_access(tmp_path):
    from fxstack.scalp.executor import LiveExecutor

    executor = LiveExecutor(_config(tmp_path), config_sha256="f" * 64)
    accepted, reason, payload = executor.submit(None)  # type: ignore[arg-type]
    assert accepted is False
    assert reason == "scalp_live_ingress_disabled_unvalidated_authority"
    assert payload == {}
    assert executor.submitted == 0
    assert executor.refused == 1


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


def test_nonfinite_equity_specs_and_cap_are_refused(tmp_path):
    cfg = _config(tmp_path)
    cert = _cert(tmp_path, cfg)
    assert scalp_entry_error(
        payload=_entry_payload(cfg),
        state=_state(),
        specs=SPECS,
        certificate=cert,
        now_epoch=float("nan"),
    ) == "scalp_now_epoch_invalid"
    assert scalp_entry_error(
        payload=_entry_payload(cfg),
        state=_state(equity=float("nan")),
        specs=SPECS,
        certificate=cert,
        now_epoch=NOW + 60,
    ) == "scalp_equity_unattested"
    for invalid in (float("nan"), float("inf")):
        for field in ("lot_size", "min_lot", "max_lot", "margin_required"):
            bad_specs = {"EURUSD": dict(SPECS["EURUSD"]) | {field: invalid}}
            assert scalp_entry_error(
                payload=_entry_payload(cfg),
                state=_state(),
                specs=bad_specs,
                certificate=cert,
                now_epoch=NOW + 60,
            ) == "scalp_broker_spec_invalid"
    assert scalp_entry_error(
        payload=_entry_payload(cfg),
        state=_state(),
        specs=SPECS,
        certificate=cert,
        now_epoch=NOW + 60,
        margin_utilization_cap=float("nan"),
    ) == "scalp_margin_cap_invalid"


# ---------------------------------------------------------------- loop gate


def test_live_loop_refuses_startup_because_standalone_live_ingress_is_disabled(
    tmp_path,
):
    from fxstack.scalp.loop import ScalpLoop

    cfg = _config(tmp_path)
    cfg.mode = "live"
    with pytest.raises(SystemExit, match="must be shadow"):
        ScalpLoop(cfg)
