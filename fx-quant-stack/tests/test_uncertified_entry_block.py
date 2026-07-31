"""An unwarranted model set may manage positions, but may not open new ones.

The certificate gate in ``training/activation.py`` is prospective: it refuses a
NEW unvalidated activation but grandfathers a set activated before it was turned
on. Measured on this repo's own manifest: EURUSD, 8 artifacts, 0 certified,
enabled and trading, against models that fail MCPT at p=0.48 and PBO 0.75.

Reporting that made it visible. This makes it stop -- for entries only.

The property that matters most here is the NEGATIVE one: blocking protective
actions on a governance failure would strand open positions, which is strictly
worse than the thing being prevented. That is pinned first.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fxstack.runtime.runner import (
    _OPERATIONAL_HARD_ENTRY_BLOCK_REASONS,
    _exploration_demo_entry_block_reason,
    _is_operational_hard_entry_block_reason,
    _resolved_entry_certification_mode,
    _uncertified_entry_block_reason,
)

CERTIFIED = {"validation_certificates": {"all_certified": True, "grandfathered": False}}
UNCERTIFIED = {"validation_certificates": {"all_certified": False, "grandfathered": True}}


def _settings(*, enforce: bool = True) -> SimpleNamespace:
    return SimpleNamespace(require_certified_models_for_entry=enforce)


# ------------------------------------------------------------- the block


def test_uncertified_set_blocks_entries():
    assert (
        _uncertified_entry_block_reason(preflight=UNCERTIFIED, settings=_settings())
        == "models_uncertified"
    )


def test_certified_set_permits_entries():
    assert _uncertified_entry_block_reason(preflight=CERTIFIED, settings=_settings()) == ""


def test_missing_report_is_not_treated_as_healthy():
    """Absence of evidence must not read as evidence of a warrant."""

    for preflight in ({}, {"validation_certificates": {}}, {"validation_certificates": None}):
        assert (
            _uncertified_entry_block_reason(preflight=preflight, settings=_settings())
            == "models_uncertified"
        )


def test_reporting_error_fails_closed_for_entries():
    preflight = {"validation_certificates": {"error": "boom", "all_certified": False}}
    assert (
        _uncertified_entry_block_reason(preflight=preflight, settings=_settings())
        == "models_uncertified"
    )


def test_explicit_operator_opt_out_is_honoured():
    """Failing open requires a deliberate, logged choice -- never a default."""

    assert (
        _uncertified_entry_block_reason(preflight=UNCERTIFIED, settings=_settings(enforce=False))
        == ""
    )


def test_default_is_enforcement_when_the_setting_is_absent():
    """A settings object predating this flag must not silently disable it."""

    assert (
        _uncertified_entry_block_reason(preflight=UNCERTIFIED, settings=SimpleNamespace())
        == "models_uncertified"
    )


# ------------------------------------------------- entry_certification_mode


def test_mode_enum_wins_over_legacy_boolean():
    assert (
        _resolved_entry_certification_mode(
            SimpleNamespace(
                entry_certification_mode="exploration_demo",
                require_certified_models_for_entry=True,
            )
        )
        == "exploration_demo"
    )
    assert (
        _resolved_entry_certification_mode(
            SimpleNamespace(
                entry_certification_mode="required",
                require_certified_models_for_entry=False,
            )
        )
        == "required"
    )


def test_unset_mode_derives_from_legacy_boolean():
    assert _resolved_entry_certification_mode(SimpleNamespace()) == "required"
    assert (
        _resolved_entry_certification_mode(_settings(enforce=False)) == "exploration_demo"
    )


def test_exploration_demo_permits_uncertified_entries():
    settings = SimpleNamespace(
        entry_certification_mode="exploration_demo",
        require_certified_models_for_entry=True,
    )
    assert _uncertified_entry_block_reason(preflight=UNCERTIFIED, settings=settings) == ""


def test_exploration_demo_fence_requires_attested_demo_account():
    """The fence fails CLOSED: real, unattested, and pre-heartbeat all block."""

    for account_mode in ("real", "", "unknown", "REAL"):
        assert (
            _exploration_demo_entry_block_reason(
                mode="exploration_demo", broker_account_mode=account_mode
            )
            == "exploration_demo_requires_demo_account_attestation"
        )
    assert (
        _exploration_demo_entry_block_reason(
            mode="exploration_demo", broker_account_mode="demo"
        )
        == ""
    )
    # In required mode the fence is inert -- certification does the blocking.
    assert (
        _exploration_demo_entry_block_reason(mode="required", broker_account_mode="real")
        == ""
    )


def test_exploration_demo_fence_is_an_operational_hard_block():
    """The adaptive policy may not override the demo-account fence."""

    assert (
        "exploration_demo_requires_demo_account_attestation"
        in _OPERATIONAL_HARD_ENTRY_BLOCK_REASONS
    )


def test_exploration_demo_fence_binds_in_the_loop_entry_only():
    """Pins the CONTIGUOUS guarded construct. An earlier version of this test
    checked that 'if not positions:' appeared anywhere before the fence call,
    which unrelated earlier occurrences satisfied trivially -- moving the fence
    outside the flat-only guard would not have failed it."""

    import inspect

    from fxstack.runtime import runner

    src = inspect.getsource(runner.run_loop)
    guarded = (
        "if not positions:\n"
        "                exploration_demo_block_reason = _exploration_demo_entry_block_reason("
    )
    assert guarded in src, (
        "the demo-attestation fence must sit directly inside the flat-only "
        "guard; blocking exits on a governance failure would strand open "
        "positions"
    )


# ------------------------------------------- it must actually bind, and only to entries


def test_reason_survives_adaptive_override():
    """The adaptive policy may override strategy opinions, not this.

    If ``models_uncertified`` were a mere strategy reason it would be filtered
    out of residual_strict_reasons whenever the adaptive policy selected a
    candidate, and the block would silently do nothing.
    """

    assert "models_uncertified" in _OPERATIONAL_HARD_ENTRY_BLOCK_REASONS
    assert _is_operational_hard_entry_block_reason("models_uncertified")


def test_block_is_applied_only_when_flat():
    """THE safety property: protective actions must never consult this gate.

    Blocking exits on a governance failure would strand open positions -- worse
    than the risk being prevented.
    """

    import inspect

    from fxstack.runtime import runner

    src = inspect.getsource(runner.run_loop)
    assert "if not positions and uncertified_entry_block_reason:" in src, (
        "the uncertified-model gate is no longer entry-only; if it can fire while "
        "a position is open it can block exits and strand the book"
    )


def test_block_is_computed_once_at_startup():
    import inspect

    from fxstack.runtime import runner

    src = inspect.getsource(runner.run_loop)
    assert "uncertified_entry_block_reason = _uncertified_entry_block_reason(" in src


@pytest.mark.parametrize("protective", ["exit", "reduce", "tighten_stop", "partial_tp"])
def test_protective_actions_are_not_in_the_entry_block_set(protective):
    """Sanity: the reason set governs entries; no protective verb belongs in it."""

    assert protective not in _OPERATIONAL_HARD_ENTRY_BLOCK_REASONS
