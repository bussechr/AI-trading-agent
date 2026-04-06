from __future__ import annotations

from fxstack.settings import get_settings


def test_weekly_full_retrain_time_defaults_to_1am(monkeypatch) -> None:
    monkeypatch.delenv("FXSTACK_WEEKLY_FULL_RETRAIN_TIME", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().weekly_full_retrain_time == "01:00"
    finally:
        get_settings.cache_clear()
