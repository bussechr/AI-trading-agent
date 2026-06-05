"""Tests for the offline docker-compose egress-policy validator.

The :mod:`fxstack.security` package ``__init__`` may import sibling modules that
other agents are still landing (e.g. a secrets store). To keep this suite
independent of that in-progress work, we load ``egress.py`` directly by file
path rather than via the package, so the test exercises *our* module regardless
of the package's import state.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

# --- Locate the repo files relative to this test (no install assumptions). ---
_THIS = Path(__file__).resolve()
_STACK_ROOT = _THIS.parents[1]  # .../fx-quant-stack
_EGRESS_PY = _STACK_ROOT / "src" / "fxstack" / "security" / "egress.py"
_COMPOSE_YML = _STACK_ROOT / "docker" / "docker-compose.offline.yml"


def _load_egress() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fxstack_egress_under_test", _EGRESS_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


egress = _load_egress()


# ---------------------------------------------------------------------------
# The shipped offline compose file must satisfy every invariant.
# ---------------------------------------------------------------------------
def test_offline_compose_file_passes_invariants() -> None:
    assert _COMPOSE_YML.exists(), f"missing compose file: {_COMPOSE_YML}"
    report = egress.validate_offline_compose_file(_COMPOSE_YML)
    assert report["ok"] is True, report["violations"]
    assert report["violations"] == []
    assert report["checks"] == {
        "loopback_ports_only": True,
        "internal_network_present": True,
        "no_remote_llm": True,
    }
    # Sanity: the expected services are present.
    assert {"app", "ollama", "vllm"} <= set(report["services"])


def test_assert_offline_compose_file_does_not_raise() -> None:
    # Should return the report (not raise) for the good file.
    report = egress.assert_offline_compose(egress.load_compose(_COMPOSE_YML))
    assert report["ok"] is True


# ---------------------------------------------------------------------------
# A deliberately-bad inline compose must fail -- one violation per invariant.
# ---------------------------------------------------------------------------
_BAD_COMPOSE = {
    "services": {
        "app": {
            # Public bind on all interfaces -> violation #1.
            "ports": ["0.0.0.0:58710:58710"],
            "environment": {
                # Remote LLM opt-in -> violation #2.
                "FXSTACK_AGENT_ALLOW_REMOTE_LLM": "true",
                # Non-loopback LLM endpoint -> violation #3.
                "FXSTACK_LLM_BASE_URL": "https://api.openai.com/v1",
            },
        },
    },
    "networks": {
        # NOT internal -> violation #4.
        "default": {"driver": "bridge"},
    },
}


def test_bad_compose_dict_fails_all_invariants() -> None:
    report = egress.egress_policy_report(_BAD_COMPOSE)
    assert report["ok"] is False
    assert report["checks"] == {
        "loopback_ports_only": False,
        "internal_network_present": False,
        "no_remote_llm": False,
    }
    # Each invariant contributes at least one human-readable violation.
    joined = "\n".join(report["violations"]).lower()
    assert "0.0.0.0" in joined
    assert "remote llm" in joined
    assert "non-loopback url" in joined
    assert "internal" in joined


def test_assert_offline_compose_raises_on_bad() -> None:
    with pytest.raises(egress.EgressPolicyError):
        egress.assert_offline_compose(_BAD_COMPOSE)


def test_bare_port_without_host_ip_is_violation() -> None:
    # "5000:5000" binds 0.0.0.0 in Docker -> must be flagged.
    report = egress.egress_policy_report(
        {
            "services": {"x": {"ports": ["5000:5000"]}},
            "networks": {"n": {"internal": True}},
        }
    )
    assert report["checks"]["loopback_ports_only"] is False
    assert report["checks"]["internal_network_present"] is True


def test_loopback_short_and_long_form_ports_pass() -> None:
    report = egress.egress_policy_report(
        {
            "services": {
                "short": {"ports": ["127.0.0.1:8000:8000"]},
                "long": {
                    "ports": [
                        {
                            "target": 9000,
                            "published": 9000,
                            "host_ip": "127.0.0.1",
                        }
                    ]
                },
                # Container-only (no published port) must not count as a publish.
                "internal_only": {"ports": ["7000"]},
            },
            "networks": {"n": {"internal": True}},
        }
    )
    # The bare "7000" (single value) is treated as a container port that Docker
    # would still expose on 0.0.0.0, so it IS a violation; verify the explicit
    # loopback binds are accepted while the bare one is flagged.
    assert "short" not in "".join(report["violations"])
    assert "long" not in "".join(report["violations"])
    assert any("internal_only" in v for v in report["violations"])


def test_yaml_text_input_is_accepted() -> None:
    text = _COMPOSE_YML.read_text(encoding="utf-8")
    report = egress.egress_policy_report(text)
    assert report["ok"] is True


def test_env_list_form_remote_flag_detected() -> None:
    report = egress.egress_policy_report(
        {
            "services": {
                "app": {
                    "environment": ["FXSTACK_AGENT_ALLOW_REMOTE_LLM=1"],
                },
            },
            "networks": {"n": {"internal": True}},
        }
    )
    assert report["checks"]["no_remote_llm"] is False


def test_ipv6_loopback_url_is_allowed() -> None:
    report = egress.egress_policy_report(
        {
            "services": {
                "app": {
                    "environment": {"FXSTACK_LLM_BASE_URL": "http://[::1]:8000/v1"},
                },
            },
            "networks": {"n": {"internal": True}},
        }
    )
    assert report["checks"]["no_remote_llm"] is True


def test_is_loopback_host_helper() -> None:
    assert egress.is_loopback_host("127.0.0.1") is True
    assert egress.is_loopback_host("::1") is True
    assert egress.is_loopback_host("localhost") is True
    assert egress.is_loopback_host("0.0.0.0") is False
    assert egress.is_loopback_host("10.0.0.5") is False
    assert egress.is_loopback_host(None) is False
