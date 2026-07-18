from __future__ import annotations

import pandas as pd

from fxstack.belief.candidate_builder import _derive_session_bucket
from fxstack.features.fx_lifecycle import _session_tag, infer_scenario_bucket
from fxstack.features.session_contract import (
    FEATURE_SCHEMA_VERSION,
    MULTI_TF_CONTRACT_VERSION,
    SESSION_CONTRACT_TIMEZONE,
    SESSION_CONTRACT_VERSION,
    current_feature_schema,
    normalize_session_bucket,
)
from fxstack.live.policy import session_bucket_from_ts


def test_session_cutovers_match_lifecycle_policy_and_belief_contracts() -> None:
    timestamps = pd.Series(
        [
            "2026-03-24T00:00:00Z",
            "2026-03-24T06:59:59Z",
            "2026-03-24T07:00:00Z",
            "2026-03-24T11:59:59Z",
            "2026-03-24T12:00:00Z",
            "2026-03-24T15:59:59Z",
            "2026-03-24T16:00:00Z",
            "2026-03-24T20:59:59Z",
            "2026-03-24T21:00:00Z",
            "2026-03-24T23:59:59Z",
            "not-a-timestamp",
        ]
    )
    expected = [
        "asia",
        "asia",
        "london_open",
        "london_open",
        "london_ny_overlap",
        "london_ny_overlap",
        "new_york",
        "new_york",
        "pacific",
        "pacific",
        "unknown",
    ]

    assert _session_tag(timestamps).tolist() == expected
    assert _derive_session_bucket(pd.DataFrame({"ts": timestamps})).tolist() == expected
    assert [session_bucket_from_ts(value) for value in timestamps] == expected


def test_session_contract_normalizes_legacy_feature_labels() -> None:
    frame = pd.DataFrame({"session_bucket": ["ny_overlap", "ny", "rollover", None]})

    assert _derive_session_bucket(frame).tolist() == [
        "london_ny_overlap",
        "new_york",
        "pacific",
        "unknown",
    ]
    assert normalize_session_bucket("London/New York Overlap") == "london_ny_overlap"
    assert infer_scenario_bucket({"session_tag": "rollover", "regime_bucket": "range"}) == "rollover_spread_shock"
    assert infer_scenario_bucket({"session_tag": "ny_overlap", "regime_bucket": "range"}) == "ny_overlap"


def test_current_feature_schema_overrides_legacy_contract_markers() -> None:
    schema = current_feature_schema(
        {
            "feature_schema_version": "fx_features_v1",
            "session_contract_version": "utc_session_buckets_v1",
            "intraday_contract": "hierarchical_v1",
        }
    )

    assert schema["feature_schema_version"] == FEATURE_SCHEMA_VERSION
    assert schema["session_contract_version"] == SESSION_CONTRACT_VERSION
    assert schema["session_contract_timezone"] == SESSION_CONTRACT_TIMEZONE
    assert schema["intraday_contract"] == MULTI_TF_CONTRACT_VERSION
