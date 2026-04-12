from __future__ import annotations

from fxstack.runtime.runner import _resolve_main_runtime_rollout_policy


def test_canonical_rollout_disablement_overrides_stale_legacy_rollout_metadata() -> None:
    rollout = _resolve_main_runtime_rollout_policy(
        pair="EURUSD",
        metadata={
            "main_runtime_rollout": {
                "mode": "canary",
                "enabled": False,
                "allowlisted_pairs": ["EURUSD"],
                "budget_scale": 0.2,
            },
            "phase5_rollout": {
                "mode": "canary",
                "enabled": True,
                "allowlisted_pairs": ["EURUSD"],
                "budget_scale": 0.4,
            },
        },
    )

    assert rollout["configured"] is True
    assert rollout["source"] == "main_runtime_rollout"
    assert rollout["mode"] == "canary"
    assert rollout["enabled"] is False
    assert rollout["active"] is False
