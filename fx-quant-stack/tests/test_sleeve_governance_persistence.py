from __future__ import annotations

import json

from fxstack.strategy.sleeve_governance import SleeveGovernanceTracker


def _record_trade(
    tracker: SleeveGovernanceTracker,
    *,
    sleeve: str,
    realized_pnl_usd: float,
    pair: str,
    holding_bars: float = 3.0,
    partial_exit_events: int = 0,
    close_reason: str = "adaptive_exit",
    session_bucket: str = "london",
) -> None:
    tracker.record_trade(
        sleeve=sleeve,
        realized_pnl_usd=realized_pnl_usd,
        holding_bars=holding_bars,
        partial_exit_events=partial_exit_events,
        close_reason=close_reason,
        session_bucket=session_bucket,
        pair=pair,
    )


def test_export_restore_round_trip_is_bounded_json_safe_and_snapshot_exact() -> None:
    tracker = SleeveGovernanceTracker(sleeves=["trend", "range"], max_trades=3)
    for index, pnl in enumerate((-8.0, 4.0, 11.5, -2.25)):
        _record_trade(
            tracker,
            sleeve="trend",
            realized_pnl_usd=pnl,
            pair=f"PAIR{index}",
            holding_bars=float(index + 1),
            partial_exit_events=index % 2,
        )
    _record_trade(tracker, sleeve="range", realized_pnl_usd=6.0, pair="EURUSD")

    exported = tracker.export_state()

    assert exported["schema_version"] == 1
    assert exported["max_trades"] == 3
    assert [event["pair"] for event in exported["trade_events"]["trend"]] == [
        "PAIR1",
        "PAIR2",
        "PAIR3",
    ]
    encoded = json.dumps(exported, allow_nan=False)

    restored = SleeveGovernanceTracker(sleeves=["trend", "range"], max_trades=3)
    restored.restore_state(json.loads(encoded))

    assert restored.export_state() == exported
    assert restored.snapshot() == tracker.snapshot()


def test_restore_uses_configured_sleeves_and_preserves_configured_maxlen() -> None:
    tracker = SleeveGovernanceTracker(sleeves=["trend"], max_trades=2)
    event_template = {
        "holding_bars": "2.5",
        "partial_exit_events": "1",
        "close_reason": "adaptive_exit",
        "session_bucket": "new_york",
    }
    tracker.restore_state(
        {
            "schema_version": "1",
            "max_trades": 100_000,
            "trade_events": {
                "unknown": [
                    {
                        **event_template,
                        "realized_pnl_usd": "999",
                        "pair": "UNKNOWN",
                    }
                ],
                "trend": [
                    {**event_template, "realized_pnl_usd": "1", "pair": "FIRST"},
                    {**event_template, "realized_pnl_usd": "2.5", "pair": "SECOND"},
                    {**event_template, "realized_pnl_usd": -3, "pair": "THIRD"},
                ],
            },
        }
    )

    exported = tracker.export_state()

    assert set(exported["trade_events"]) == {"trend"}
    assert exported["max_trades"] == 2
    assert tracker._trade_events["trend"].maxlen == 2
    assert [event["pair"] for event in exported["trade_events"]["trend"]] == ["SECOND", "THIRD"]
    assert exported["trade_events"]["trend"][0]["realized_pnl_usd"] == 2.5
    assert exported["trade_events"]["trend"][0]["partial_exit_events"] == 1


def test_restore_ignores_malformed_or_unknown_payload_without_erasing_history() -> None:
    tracker = SleeveGovernanceTracker(sleeves=["trend"], max_trades=2)
    _record_trade(tracker, sleeve="trend", realized_pnl_usd=7.5, pair="EURUSD")
    baseline = tracker.export_state()

    malformed_payloads = [
        None,
        [],
        {"schema_version": 999, "trade_events": {"trend": []}},
        {"schema_version": 1, "trade_events": []},
        {"schema_version": 1, "trade_events": {"unknown": []}},
        {"schema_version": 1, "trade_events": {"trend": "not-a-list"}},
        {
            "schema_version": 1,
            "trade_events": {
                "trend": [
                    None,
                    {"realized_pnl_usd": "nan"},
                    {
                        "realized_pnl_usd": 1.0,
                        "holding_bars": -1.0,
                        "partial_exit_events": True,
                        "close_reason": [],
                        "session_bucket": {},
                        "pair": "EURUSD",
                    },
                ]
            },
        },
    ]
    for payload in malformed_payloads:
        tracker.restore_state(payload)
        assert tracker.export_state() == baseline


def test_restore_skips_bad_records_within_a_bounded_valid_suffix() -> None:
    tracker = SleeveGovernanceTracker(sleeves=["trend"], max_trades=3)
    tracker.restore_state(
        {
            "schema_version": 1,
            "trade_events": {
                "trend": [
                    {
                        "realized_pnl_usd": 100.0,
                        "holding_bars": 1,
                        "partial_exit_events": 0,
                        "close_reason": "too_old",
                        "session_bucket": "asia",
                        "pair": "OLD",
                    },
                    {"not": "an event"},
                    {
                        "realized_pnl_usd": "4.5",
                        "holding_bars": "2",
                        "partial_exit_events": "0",
                        "close_reason": "adaptive_exit",
                        "session_bucket": "london",
                        "pair": "EURUSD",
                    },
                    {
                        "realized_pnl_usd": -2,
                        "holding_bars": 3,
                        "partial_exit_events": 1,
                        "close_reason": "adaptive_replacement_exit",
                        "session_bucket": "new_york",
                        "pair": "GBPUSD",
                    },
                ]
            },
        }
    )

    exported_events = tracker.export_state()["trade_events"]["trend"]

    assert [event["pair"] for event in exported_events] == ["EURUSD", "GBPUSD"]
    assert tracker.snapshot()["trend"].trades == 2
