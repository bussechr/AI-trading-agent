from __future__ import annotations

from fxstack.settings import Settings, get_settings


# Default-value tests intentionally bypass any operator-local ``.env`` file
# (via ``_env_file=None``) so they verify the *code default* and don't fail on
# a developer machine where ``.env`` legitimately overrides the field.
def test_weekly_full_retrain_time_defaults_to_1am(monkeypatch) -> None:
    monkeypatch.delenv("FXSTACK_WEEKLY_FULL_RETRAIN_TIME", raising=False)
    settings = Settings(_env_file=None)
    assert settings.weekly_full_retrain_time == "01:00"


def test_max_allowed_spread_bps_defaults_to_3_bps(monkeypatch) -> None:
    monkeypatch.delenv("FXSTACK_MAX_ALLOWED_SPREAD_BPS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.max_allowed_spread_bps == 3.0


def test_phase0_orchestration_settings_are_inert_by_default(monkeypatch) -> None:
    for key in [
        "FXSTACK_AGENT_MODE",
        "FXSTACK_AGENT_ALLOW_REMOTE_LLM",
        "FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS",
        "FXSTACK_MCP_ENABLED",
        "FXSTACK_OPENCLAW_ENABLED",
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
    assert settings.agent_live_intent_allowlist == ["enter"]
    assert settings.phase6b_canary_ramp_steps_pct == [1, 5, 10]
    assert settings.phase6b_canary_drawdown_deterioration_pct == -1.0
    assert settings.agent_allow_remote_llm is False
    assert settings.agent_allow_external_tools is False
    assert settings.mcp_enabled is False
    assert settings.openclaw_enabled is False


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
