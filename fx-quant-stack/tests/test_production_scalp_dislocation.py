from __future__ import annotations

import ast
from dataclasses import fields, replace
from pathlib import Path

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.scalp.bars import M1Bar
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.signals import evaluate_dislocation as evaluate_research
from fxstack.schemas.entry import EntryBar, EntryEvaluationRequest, EntryProposal
from fxstack.strategy.scalp_dislocation import (
    SCALP_DISLOCATION_POLICY_SCHEMA_VERSION,
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    DislocationPolicy,
    evaluate_dislocation,
)


SOURCE_ID = "ig_mt4_bridge_closed_bars"
SOURCE_VERSION = "v1"
START_EPOCH = 1_800_000_000


def _research_config(policy: DislocationPolicy) -> ScalpConfig:
    config = ScalpConfig()
    for item in fields(policy):
        setattr(config, item.name, getattr(policy, item.name))
    return config


def _bar(
    *,
    symbol: str,
    index: int,
    open_px: float,
    close_px: float,
    spread_bps: float = 0.6,
) -> M1Bar:
    half_spread = close_px * spread_bps / 1e4 / 2.0
    return M1Bar(
        symbol=symbol,
        minute_epoch=START_EPOCH + index * 60,
        open=open_px,
        high=max(open_px, close_px) + 0.00002,
        low=min(open_px, close_px) - 0.00002,
        close=close_px,
        bid_close=close_px - half_spread,
        ask_close=close_px + half_spread,
        spread_max_bps=spread_bps,
        spread_close_bps=spread_bps,
        tick_count=4,
        valid=True,
        quote_changes=3,
    )


def _quiet_run(*, symbol: str, count: int) -> list[M1Bar]:
    bars: list[M1Bar] = []
    for index in range(count):
        wiggle = 0.0001 if index % 2 else -0.0001
        open_px = 1.1 + wiggle
        close_px = open_px + (0.00001 if index % 2 else -0.00001)
        bars.append(
            _bar(
                symbol=symbol,
                index=index,
                open_px=open_px,
                close_px=close_px,
            )
        )
    return bars


def _dislocated_run(
    *,
    symbol: str = "EURUSD",
    direction: float = 1.0,
    count: int = 26,
) -> list[M1Bar]:
    bars = _quiet_run(symbol=symbol, count=count - 1)
    displacement = direction * 0.0044
    bars.append(
        _bar(
            symbol=symbol,
            index=count - 1,
            open_px=1.1 + displacement,
            close_px=1.1 + displacement * 0.9,
        )
    )
    return bars


def _entry_bars(bars: list[M1Bar]) -> tuple[EntryBar, ...]:
    return tuple(
        EntryBar(
            symbol=bar.symbol,
            venue_id=IG_MT4_VENUE_ID,
            source_id=SOURCE_ID,
            source_version=SOURCE_VERSION,
            minute_epoch=bar.minute_epoch,
            bar_seconds=60,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            bid_close=bar.bid_close,
            ask_close=bar.ask_close,
            closed=True,
            quality_flags=(),
        )
        for bar in bars
    )


def _request(bars: list[M1Bar], *, spread_bps: float) -> EntryEvaluationRequest:
    return EntryEvaluationRequest(
        symbol=bars[-1].symbol,
        bars=_entry_bars(bars),
        spread_bps=spread_bps,
    )


@pytest.mark.parametrize(
    ("direction", "expected_side"),
    ((1.0, "SELL"), (-1.0, "BUY")),
)
def test_allowed_candidate_has_exact_research_arithmetic_parity(
    direction: float,
    expected_side: str,
) -> None:
    policy = DislocationPolicy(
        min_history_bars=20,
        z_entry=2.0,
        p_star_max=0.80,
    )
    research_bars = _dislocated_run(direction=direction)
    research, research_reason = evaluate_research(
        bars=research_bars,
        config=_research_config(policy),
        spread_bps=0.6,
    )
    proposal = evaluate_dislocation(_request(research_bars, spread_bps=0.6), policy)

    assert research_reason == ""
    assert research is not None
    assert proposal.allowed is True
    assert proposal.reasons == ()
    assert proposal.side == research.side == expected_side
    assert proposal.minute_epoch == research.minute_epoch
    assert proposal.ref_mid == research.ref_mid
    assert proposal.entry_price == research.entry_price
    assert proposal.sl_price == research.sl_price
    assert proposal.tp_price == research.tp_price
    assert proposal.atr_bps == research.atr_bps
    assert proposal.stop_bps == research.stop_bps
    recorded_cost = proposal.spread_bps + policy.execution_debit_bps
    assert proposal.target_bps == policy.target_cost_multiple * recorded_cost
    assert proposal.stop_bps is not None
    assert proposal.target_bps is not None
    assert proposal.spread_bps is not None
    assert proposal.stop_bps == max(
        policy.stop_cost_multiple * recorded_cost,
        policy.min_stop_bps,
    )
    assert proposal.disp_z == research.disp_z
    assert proposal.spread_bps == research.spread_bps
    assert proposal.p_star == research.p_star
    assert proposal.p_star == (
        proposal.stop_bps + recorded_cost
    ) / (proposal.target_bps + proposal.stop_bps)
    assert proposal.time_stop_bars == research.time_stop_bars

    assert proposal.strategy_id == SCALP_DISLOCATION_STRATEGY_ID
    assert proposal.strategy_version == SCALP_DISLOCATION_STRATEGY_VERSION
    assert proposal.source_id == SOURCE_ID
    assert proposal.source_version == SOURCE_VERSION
    assert proposal.qualification == "candidate_unqualified"
    assert proposal.execution_qualified is False
    assert proposal.win_probability is None


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("insufficient", "insufficient_valid_history"),
        ("no_dislocation", "no_dislocation"),
        ("cost_dead", "bracket_cost_dead"),
        ("gross_too_small", "gross_too_small_vs_cost"),
    ),
)
def test_refusal_reason_has_exact_research_parity(
    case: str,
    expected_reason: str,
) -> None:
    policy = DislocationPolicy(
        min_history_bars=20,
        z_entry=2.0,
        p_star_max=0.80,
    )
    bars = _dislocated_run()
    spread_bps = 0.6
    if case == "insufficient":
        bars = bars[:10]
    elif case == "no_dislocation":
        bars = _quiet_run(symbol="EURUSD", count=26)
    elif case == "cost_dead":
        policy = replace(policy, p_star_max=0.70)
    elif case == "gross_too_small":
        policy = replace(policy, p_star_max=0.99, min_tp_cost_ratio=100.0)

    research, research_reason = evaluate_research(
        bars=bars,
        config=_research_config(policy),
        spread_bps=spread_bps,
    )
    proposal = evaluate_dislocation(_request(bars, spread_bps=spread_bps), policy)

    assert research is None
    assert research_reason == expected_reason
    assert proposal.allowed is False
    assert proposal.reasons == (research_reason,)
    assert proposal.win_probability is None
    assert proposal.execution_qualified is False


@pytest.mark.parametrize(
    ("mutation", "expected_reasons"),
    (
        ("open", ("bar_not_closed",)),
        ("quality", ("bar_quality_flags_present",)),
        ("gap", ("non_consecutive_closed_bars",)),
        ("mixed_source", ("mixed_bar_source_identity",)),
    ),
)
def test_evaluator_requires_consecutive_closed_quality_clean_versioned_bars(
    mutation: str,
    expected_reasons: tuple[str, ...],
) -> None:
    policy = DislocationPolicy(min_history_bars=20, p_star_max=0.80)
    bars = list(_entry_bars(_dislocated_run()))
    if mutation == "open":
        bars[-1] = replace(bars[-1], closed=False)
    elif mutation == "quality":
        bars[-1] = replace(bars[-1], quality_flags=("missing_tick",))
    elif mutation == "gap":
        bars[-1] = replace(bars[-1], minute_epoch=bars[-1].minute_epoch + 60)
    elif mutation == "mixed_source":
        bars[-1] = replace(bars[-1], source_version="v2")
    request = EntryEvaluationRequest(
        symbol="EURUSD",
        bars=tuple(bars),
        spread_bps=0.6,
    )

    proposal = evaluate_dislocation(request, policy)

    assert proposal.allowed is False
    assert proposal.reasons == expected_reasons


@pytest.mark.parametrize("symbol", IG_MT4_SCALP_SYMBOLS)
def test_every_catalog_symbol_can_reach_the_same_pure_candidate_layer(
    symbol: str,
) -> None:
    policy = DislocationPolicy(min_history_bars=20, p_star_max=0.80)
    bars = _dislocated_run(symbol=symbol)

    proposal = evaluate_dislocation(_request(bars, spread_bps=0.6), policy)

    assert proposal.allowed is True
    assert proposal.symbol == symbol
    assert proposal.instrument_id.endswith(f":{symbol}")
    assert proposal.venue_id == IG_MT4_VENUE_ID


def test_policy_hash_is_deterministic_complete_and_stamped_on_proposal() -> None:
    policy = DislocationPolicy(min_history_bars=20, p_star_max=0.80)
    same_policy = DislocationPolicy(min_history_bars=20, p_star_max=0.80)
    changed_policy = replace(policy, z_entry=policy.z_entry + 0.01)

    assert set(policy.to_canonical_dict()) == {
        "policy_schema_version",
        "scope_version",
        "production_rollover_guard",
        *(item.name for item in fields(policy)),
    }
    assert SCALP_DISLOCATION_STRATEGY_VERSION == "fxstack.strategy.scalp_dislocation.v5"
    assert SCALP_DISLOCATION_POLICY_SCHEMA_VERSION == (
        "fxstack.strategy.scalp_dislocation.policy.v5"
    )
    assert policy.to_canonical_dict()["scope_version"] == (
        "fxstack.ig_mt4.scalp_scope.v3"
    )
    assert policy.config_sha256() == same_policy.config_sha256()
    assert policy.config_sha256() != changed_policy.config_sha256()
    assert (
        policy.config_sha256()
        == "b2c48fa208a235f085d1514364c7f0cf0194cb36f2988ddc6a5d43d9388692cd"
    )

    proposal = evaluate_dislocation(
        _request(_dislocated_run(), spread_bps=0.6),
        policy,
    )
    assert proposal.config_sha256 == policy.config_sha256()


def test_production_contract_has_no_size_command_or_probability_claim() -> None:
    field_names = set(EntryProposal.__dataclass_fields__)

    assert {"lots", "size", "quantity", "command_id"}.isdisjoint(field_names)
    assert "win_probability" in field_names
    proposal = evaluate_dislocation(
        _request(_dislocated_run(), spread_bps=0.6),
        DislocationPolicy(min_history_bars=20, p_star_max=0.80),
    )
    assert proposal.win_probability is None
    assert proposal.qualification == "candidate_unqualified"
    assert proposal.execution_qualified is False


def test_production_entry_modules_do_not_import_excluded_scalp_package() -> None:
    package_root = Path(__file__).resolve().parents[1] / "src" / "fxstack"
    paths = (
        package_root / "schemas" / "entry.py",
        package_root / "strategy" / "scalp_dislocation.py",
    )
    imported: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)

    assert all(
        module != "fxstack.scalp" and not module.startswith("fxstack.scalp.")
        for module in imported
    )
