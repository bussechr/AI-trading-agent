from __future__ import annotations

from fxstack.settings import (
    KNOWN_NON_SETTINGS_ENV_VARS,
    Settings,
    declared_env_aliases,
    get_settings,
    unknown_fxstack_env_warnings,
)


def test_max_allowed_spread_bps_defaults_to_3_bps(monkeypatch) -> None:
    monkeypatch.delenv("FXSTACK_MAX_ALLOWED_SPREAD_BPS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.max_allowed_spread_bps == 3.0


def test_phase0_orchestration_settings_are_inert_by_default(monkeypatch) -> None:
    for key in [
        "FXSTACK_AGENT_MODE",
        "FXSTACK_AGENT_ALLOW_REMOTE_LLM",
        "FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS",
        "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST",
        "FXSTACK_LIVE_ARMED",
    ]:
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.agent_mode == "off"
    assert settings.agent_durability == "async"
    assert settings.agent_shadow_pair_allowlist == []
    assert settings.agent_paper_pair_allowlist == []
    assert settings.agent_paper_sleeve_allowlist == []
    assert settings.agent_paper_intent_allowlist == ["enter"]
    assert settings.agent_live_pair_allowlist == []
    assert settings.agent_live_sleeve_allowlist == []
    assert settings.agent_live_intent_allowlist == []
    assert settings.live_armed is False
    assert settings.phase6b_canary_ramp_steps_pct == [1, 5, 10]
    assert settings.phase6b_canary_drawdown_deterioration_pct == -1.0
    assert settings.agent_allow_remote_llm is False
    assert settings.agent_allow_external_tools is False


def test_removed_noop_settings_do_not_pollute_runtime_or_release_identity() -> None:
    removed_aliases = {
        "FXSTACK_RUN_FAST_GATE",
        "FXSTACK_SWING_PRIMARY_TIMEFRAME",
        "FXSTACK_STRICT_COMMAND_VALIDATION",
        "FXSTACK_DRIFT_TRIGGER_ECE",
        "FXSTACK_DRIFT_TRIGGER_THROUGHPUT_DROP",
        "FXSTACK_LIVE_SPREAD_REJECT_RATE_TRIGGER",
        "FXSTACK_INTRADAY_TCN_FALLBACK_LIVE_ALLOWED",
        "FXSTACK_MCP_ENABLED",
        "FXSTACK_MCP_TRANSPORT",
        "FXSTACK_OPENCLAW_ENABLED",
        "FXSTACK_OPENCLAW_SCOPES",
        "FXSTACK_OPENCLAW_SANDBOX_REQUIRED",
        "FXSTACK_LLM_SEED",
    }
    removed_public_fields = {
        alias.removeprefix("FXSTACK_").lower() for alias in removed_aliases
    }

    assert removed_aliases.isdisjoint(declared_env_aliases())
    assert removed_public_fields.isdisjoint(Settings(_env_file=None).to_public_dict())
    assert len(
        unknown_fxstack_env_warnings({alias: "1" for alias in removed_aliases})
    ) == len(removed_aliases)
    assert {
        "FXSTACK_PG_SERVICE_NAME",
        "FXSTACK_RUN_SHADOW_24H",
    } <= KNOWN_NON_SETTINGS_ENV_VARS
    assert {
        "FXSTACK_PG_SERVICE_NAME",
        "FXSTACK_RUN_SHADOW_24H",
    }.isdisjoint(declared_env_aliases())
    assert KNOWN_NON_SETTINGS_ENV_VARS.isdisjoint(declared_env_aliases())


def test_agent_mode_paper_selects_paper_execution_provider(monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "paper")
    monkeypatch.delenv("FXSTACK_EXECUTION_PROVIDER", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().normalized_execution_provider == "paper"
    finally:
        get_settings.cache_clear()


def test_agent_mode_live_keeps_mt4_execution_provider(monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_AGENT_MODE", "live")
    monkeypatch.delenv("FXSTACK_EXECUTION_PROVIDER", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().normalized_execution_provider == "mt4"
    finally:
        get_settings.cache_clear()
