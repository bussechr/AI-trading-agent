from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fxstack.runtime import execution_egress_control, release_authority
from fxstack.runtime.release_trust import physical_boundary_errors


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "data_provider": "dukascopy",
        "execution_provider": "mt4",
        "pairs": ["EURUSD"],
        "use_portfolio_ranking": True,
        "portfolio_corr_mode": "realized",
        "portfolio_realized_corr_window_bars": 96,
        "portfolio_realized_corr_min_obs": 24,
        "reversal_opportunity_min_prob": 0.5,
        "equity_lots_per_usd": 0.00001,
        "database_url": "postgresql://secret-a",
        "mt4_bridge_url": "http://127.0.0.1:58710",
        "project_root": Path("one"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_execution_config_projection_binds_portfolio_reversal_and_sizing() -> None:
    baseline = _settings()
    baseline_hash = release_authority.runtime_config_sha256(baseline)
    for field, value in (
        ("use_portfolio_ranking", False),
        ("portfolio_corr_mode", "heuristic"),
        ("portfolio_realized_corr_window_bars", 48),
        ("portfolio_realized_corr_min_obs", 12),
        ("reversal_opportunity_min_prob", 0.7),
        ("equity_lots_per_usd", 0.00002),
    ):
        changed = _settings()
        setattr(changed, field, value)
        assert release_authority.runtime_config_sha256(changed) != baseline_hash


def test_execution_config_projection_ignores_endpoint_secret_and_path_values() -> None:
    baseline = _settings()
    moved = _settings(
        database_url="postgresql://other-secret",
        mt4_bridge_url="http://10.0.0.9:65000",
        project_root=Path("elsewhere"),
    )
    assert release_authority.runtime_config_sha256(moved) == release_authority.runtime_config_sha256(
        baseline
    )


def test_authority_rejects_missing_active_database_row() -> None:
    errors = release_authority.authority_request_errors(
        {"schema_version": release_authority.RELEASE_AUTHORITY_REQUEST_SCHEMA},
        active_db_row=None,
        validate_evidence=False,
    )
    assert "release_authority_active_db_row_missing" in errors


def test_physical_boundary_reports_exact_unproven_blocker() -> None:
    errors = physical_boundary_errors(
        {
            "production_authority_allowed": False,
            "errors": ["release_trust_policy_unavailable"],
        }
    )
    assert errors[0] == "physical_boundary_unproven"
    assert "physical_boundary:release_trust_policy_unavailable" in errors
    assert (
        "physical_boundary:policy_intent_missing:terminal_wide_ea_lease_provisioned"
        in errors
    )
    assert "physical_boundary:terminal_wide_ea_lease_unobserved" in errors
    assert "physical_boundary:poll_ack_consumer_token_unobserved" in errors


class _FakeService:
    def __init__(self, *, database_url: str) -> None:
        self.database_url = database_url
        self.disabled = False

    def disable_execution_egress(self, *, reason: str, revoke_release: bool) -> dict[str, object]:
        assert revoke_release is False
        self.disabled = True
        return {"quarantined_command_count": 3, "reason": reason}

    def get_state(self) -> dict[str, object]:
        return {
            "execution_egress_enabled": False if self.disabled else True,
            "release_authority": {"status": "active"},
            "runtime_diag": {
                "orchestration_live": {
                    "runtime_enabled": not self.disabled,
                    "queue_kill_active": self.disabled,
                }
            },
        }

    def get_metrics(self) -> dict[str, object]:
        return {"pending": {"count": 0}}


def test_stop_control_verifies_committed_disable(monkeypatch) -> None:
    monkeypatch.setattr(execution_egress_control, "RuntimeService", _FakeService)
    monkeypatch.setattr(
        execution_egress_control,
        "get_settings",
        lambda: SimpleNamespace(database_url="postgresql://runtime"),
    )
    result = execution_egress_control.disable_and_verify(reason="operator_stop_all")
    assert result["ok"] is True
    assert result["pending_command_count"] == 0
    assert result["quarantined_command_count"] == 3
