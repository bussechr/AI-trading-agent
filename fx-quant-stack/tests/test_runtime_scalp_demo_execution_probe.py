from __future__ import annotations

import pytest

from fxstack.runtime.scalp_demo_execution_probe import (
    SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,
    SCALP_DEMO_EXECUTION_PROBE_SCHEMA_VERSION,
    ScalpDemoExecutionProbeRequest,
    build_scalp_demo_execution_probe_candidate,
    scalp_demo_execution_probe_command_id,
    validate_scalp_demo_execution_probe_request,
)
from fxstack.runtime.scalp_engine_identity import SCALP_ENGINE_COMPONENTS


def test_retired_probe_is_inert_when_no_legacy_fields_are_supplied() -> None:
    result = validate_scalp_demo_execution_probe_request(
        live_mode=True,
        expected_account_mode="demo",
        admission_mode="signed_validation",
    )

    assert result.enabled is False
    assert result.valid is False
    assert result.request is None
    assert result.reasons == ()
    assert result.to_dict() == {
        "schema_version": SCALP_DEMO_EXECUTION_PROBE_SCHEMA_VERSION,
        "enabled": False,
        "valid": False,
        "request": None,
        "reasons": [],
    }


@pytest.mark.parametrize(
    ("probe_id", "symbol", "side"),
    [
        ("legacy-probe", "", ""),
        ("", "EURUSD", ""),
        ("", "", "BUY"),
        ("legacy-probe", "EURUSD", "BUY"),
    ],
)
def test_every_supplied_legacy_probe_request_is_rejected(
    probe_id: str,
    symbol: str,
    side: str,
) -> None:
    for expected_account_mode in ("demo", "real"):
        result = validate_scalp_demo_execution_probe_request(
            probe_id=probe_id,
            symbol=symbol,
            side=side,
            live_mode=True,
            expected_account_mode=expected_account_mode,
            admission_mode="signed_validation",
        )

        assert result.enabled is True
        assert result.valid is False
        assert result.request is None
        assert result.reasons == (SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,)


def test_legacy_request_cannot_mint_payload_command_or_candidate() -> None:
    request = ScalpDemoExecutionProbeRequest(
        probe_id="legacy-probe",
        symbol="EURUSD",
        side="BUY",
    )

    with pytest.raises(
        RuntimeError,
        match=SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,
    ):
        request.command_fields()
    with pytest.raises(
        RuntimeError,
        match=SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,
    ):
        scalp_demo_execution_probe_command_id(
            request,
            runtime_boot_id="runtime-boot",
        )

    result = build_scalp_demo_execution_probe_candidate(
        request,
        tick={"bid": 1.1, "ask": 1.1001},
        contract=object(),
        quote_rates={},
        account_currency="USD",
        broker_account_mode="demo",
        as_of_epoch=1_900_000_000.0,
    )
    assert result.accepted is False
    assert result.qualified_candidate is None
    assert result.reasons == (SCALP_DEMO_EXECUTION_PROBE_REMOVED_REASON,)
    assert result.to_dict()["qualified_candidate"] is None


def test_retired_demo_probe_is_outside_the_active_mtvclc_engine_identity() -> None:
    assert "runtime/scalp_demo_execution_probe.py" not in SCALP_ENGINE_COMPONENTS
