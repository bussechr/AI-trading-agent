# AGENT: ROLE: Offline-hardening validator for the local model-serving docker stack.
# AGENT: ENTRYPOINT: `egress_policy_report(compose)` / `validate_offline_compose_file(path)`.
# AGENT: PRIMARY INPUTS: a docker-compose mapping (dict) or its YAML text / file path.
# AGENT: PRIMARY OUTPUTS: a report dict with `ok` + per-invariant violations.
# AGENT: STATE / SIDE EFFECTS: pure (parses YAML only); no network, never binds a port.
# AGENT: SEE: docker/docker-compose.offline.yml ; ops/security/block_egress.sh
"""Static validator for the offline / air-gapped docker-compose stack.

This module asserts the *offline invariants* the local model stack must keep so
that pre-staged model weights are served on loopback only and nothing in the
stack can phone home:

1. **No public port bindings.** Every published port must bind ``127.0.0.1``
   (loopback) explicitly. A bare ``"5000:5000"`` (which Docker exposes on
   ``0.0.0.0``) or a ``0.0.0.0`` host-ip is a violation.
2. **An ``internal: true`` network exists.** Post-setup egress blocking relies
   on an internal docker network that has no gateway to the outside world.
3. **No remote-LLM opt-in.** ``FXSTACK_AGENT_ALLOW_REMOTE_LLM`` must stay falsey
   in every service environment, and configured LLM base URLs must be loopback.

The validator is intentionally pure and dependency-light: it takes a mapping (or
YAML text / a file path parsed via :mod:`yaml`, already a hard dependency) and
returns a structured report. It performs no Docker calls and no network I/O, so
it is safe to run in tests and in CI on an air-gapped box.
"""

from __future__ import annotations

import ipaddress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

__all__ = [
    "EgressPolicyError",
    "egress_policy_report",
    "assert_offline_compose",
    "load_compose",
    "validate_offline_compose_file",
    "is_loopback_host",
]

# Hosts that resolve to the local machine. ``0.0.0.0`` is treated as a binding
# wildcard (NOT loopback) for *port publishing*, because publishing on it exposes
# the port on every interface -- see ``_iter_port_violations``.
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "[::1]"}

# Environment keys whose value gates remote model access. Must stay falsey.
_REMOTE_LLM_FLAGS = ("FXSTACK_AGENT_ALLOW_REMOTE_LLM",)

# Environment keys that carry an LLM endpoint URL; must point at loopback.
_LLM_URL_KEYS = (
    "FXSTACK_LLM_BASE_URL",
    "OLLAMA_BASE_URL",
    "OPENAI_BASE_URL",
    "OPENAI_API_BASE",
)

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


class EgressPolicyError(AssertionError):
    """Raised by :func:`assert_offline_compose` when an invariant is violated."""


def is_loopback_host(host: str | None) -> bool:
    """Return ``True`` when ``host`` unambiguously refers to the loopback iface.

    ``0.0.0.0`` / ``::`` (the bind-all wildcards) and empty hosts return
    ``False`` -- for our purposes those mean "exposed on all interfaces".
    """

    text = (host or "").strip().lower().strip("[]")
    if not text:
        return False
    if text in {"127.0.0.1", "::1"} or text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _url_is_loopback(url: str) -> bool:
    """Return ``True`` when an LLM base URL targets loopback."""

    raw = str(url or "").strip()
    if not raw:
        # An empty/unset URL is not itself a violation here.
        return True
    host = (urlsplit(raw).hostname or "").strip().lower()
    if not host:
        # Recover a bare IPv6 authority like ``http://::1``.
        rest = raw.split("://", 1)[-1].split("/", 1)[0].split("@")[-1]
        host = rest.strip().lower().strip("[]")
    return is_loopback_host(host)


def _is_truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in _TRUTHY


def _normalise_env(raw: Any) -> dict[str, str]:
    """Normalise a compose ``environment`` block to a ``{KEY: value}`` mapping.

    Compose accepts either a mapping (``KEY: value``) or a list of ``KEY=value``
    strings; both are handled here. A bare ``KEY`` (list form, value inherited
    from the host env) maps to an empty string.
    """

    env: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            env[str(key)] = "" if value is None else str(value)
    elif isinstance(raw, (list, tuple)):
        for item in raw:
            text = str(item)
            key, sep, value = text.partition("=")
            env[key.strip()] = value if sep else ""
    return env


def _host_ip_from_port(entry: Any) -> tuple[str | None, str]:
    """Extract ``(host_ip, raw)`` from a single compose ports entry.

    Returns ``host_ip=None`` when the entry publishes a port with no explicit
    host IP (Docker then binds ``0.0.0.0`` -- a violation), or when the entry is
    malformed. Long-form mappings without a ``published`` port (container-only)
    yield a sentinel that callers treat as non-published.
    """

    # Long form: {target, published, host_ip, mode, protocol}
    if isinstance(entry, dict):
        if "published" not in entry or entry.get("published") in (None, ""):
            # No published port -> not reachable from the host; skip.
            return ("__container_only__", str(entry))
        host_ip = entry.get("host_ip")
        return (str(host_ip) if host_ip not in (None, "") else None, str(entry))

    # Short form string/number: "[host_ip:]host:container[/proto]" or just a port.
    text = str(entry).strip()
    if not text:
        return ("__container_only__", text)
    # Strip an optional /tcp|/udp protocol suffix.
    spec = text.split("/", 1)[0]
    parts = spec.split(":")
    if len(parts) == 1:
        # "8080" -- container port only, Docker assigns/exposes on 0.0.0.0.
        return (None, text)
    if len(parts) == 2:
        # "host:container" -- no explicit host IP -> 0.0.0.0.
        return (None, text)
    # 3+ parts: "host_ip:host:container"; host_ip may itself contain ':' (IPv6).
    host_ip = ":".join(parts[:-2])
    return (host_ip or None, text)


def _iter_port_violations(services: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        ports = svc.get("ports") or []
        if not isinstance(ports, (list, tuple)):
            ports = [ports]
        for entry in ports:
            host_ip, raw = _host_ip_from_port(entry)
            if host_ip == "__container_only__":
                continue
            if host_ip is None:
                violations.append(
                    f"service {name!r} publishes port {raw!r} without binding "
                    f"127.0.0.1 (Docker would expose it on 0.0.0.0)"
                )
            elif not is_loopback_host(host_ip):
                violations.append(
                    f"service {name!r} publishes port {raw!r} on non-loopback "
                    f"host ip {host_ip!r}"
                )
    return violations


def _iter_remote_llm_violations(services: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for name, svc in services.items():
        if not isinstance(svc, dict):
            continue
        env = _normalise_env(svc.get("environment"))
        for flag in _REMOTE_LLM_FLAGS:
            if flag in env and _is_truthy(env[flag]):
                violations.append(
                    f"service {name!r} sets {flag}={env[flag]!r}; remote LLM "
                    f"access must stay disabled in the offline stack"
                )
        for key in _LLM_URL_KEYS:
            if key in env and not _url_is_loopback(env[key]):
                violations.append(
                    f"service {name!r} points {key} at non-loopback URL "
                    f"{env[key]!r}"
                )
    return violations


def _internal_network_present(networks: Any) -> bool:
    if not isinstance(networks, dict):
        return False
    for spec in networks.values():
        if isinstance(spec, dict) and _is_truthy(spec.get("internal")):
            return True
    return False


def egress_policy_report(compose: dict[str, Any] | str) -> dict[str, Any]:
    """Validate the offline invariants of a docker-compose definition.

    Parameters
    ----------
    compose:
        Either a parsed compose mapping or raw YAML text. (Use
        :func:`validate_offline_compose_file` to load from a path.)

    Returns
    -------
    dict
        ``{"ok": bool, "violations": [...], "checks": {...}, "services": [...]}``
        where ``checks`` flags each invariant individually so callers can render
        a per-rule report.
    """

    mapping = _coerce_mapping(compose)
    services = mapping.get("services")
    if not isinstance(services, dict):
        services = {}
    networks = mapping.get("networks")

    port_violations = _iter_port_violations(services)
    remote_violations = _iter_remote_llm_violations(services)
    has_internal = _internal_network_present(networks)

    checks = {
        "loopback_ports_only": not port_violations,
        "internal_network_present": has_internal,
        "no_remote_llm": not remote_violations,
    }
    violations = list(port_violations) + list(remote_violations)
    if not has_internal:
        violations.append(
            "no docker network declares 'internal: true'; post-setup egress "
            "blocking has nothing to rely on"
        )

    return {
        "ok": not violations,
        "violations": violations,
        "checks": checks,
        "services": sorted(services.keys()),
    }


def assert_offline_compose(compose: dict[str, Any] | str) -> dict[str, Any]:
    """Like :func:`egress_policy_report` but raise on any violation.

    Returns the report on success so callers can still inspect ``checks``.
    """

    report = egress_policy_report(compose)
    if not report["ok"]:
        joined = "\n - ".join(report["violations"])
        raise EgressPolicyError(f"offline compose invariants violated:\n - {joined}")
    return report


def _coerce_mapping(compose: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(compose, dict):
        return compose
    parsed = yaml.safe_load(str(compose))
    if not isinstance(parsed, dict):
        raise EgressPolicyError("compose YAML did not parse to a mapping")
    return parsed


def load_compose(path: str | Path) -> dict[str, Any]:
    """Parse a compose YAML file into a mapping via :mod:`yaml`."""

    text = Path(path).read_text(encoding="utf-8")
    parsed = yaml.safe_load(text)
    if not isinstance(parsed, dict):
        raise EgressPolicyError(f"{path} did not parse to a compose mapping")
    return parsed


def validate_offline_compose_file(path: str | Path) -> dict[str, Any]:
    """Load ``path`` and return its :func:`egress_policy_report`."""

    return egress_policy_report(load_compose(path))
