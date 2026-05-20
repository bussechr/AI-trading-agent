"""Pin :class:`RuntimeServiceProtocol` against its concrete implementation.

The protocol exists to:

1. Document the contract the bridge HTTP layer + runtime loop depend on.
2. Catch drift between :class:`RuntimeService` and the consumers — if a method
   is renamed/removed/added, this test fails immediately rather than waiting
   for a runtime ``AttributeError`` in production.
3. Provide a smoke check for alternative implementations (test fakes,
   research-mode in-memory variants).

What this test does NOT check:

* Signature compatibility (parameter types, keyword-only-ness). Python's
  :func:`typing.runtime_checkable` doesn't go that deep; mypy + the
  signature alignment in :mod:`fxstack.runtime.service_contract` are the
  source of truth there.
* Behavioral parity. That's the job of the integration tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fxstack.runtime.service_contract import RuntimeServiceProtocol


def test_runtime_service_satisfies_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete RuntimeService instance must satisfy the protocol."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'protocol.db'}"
    monkeypatch.setenv("FXSTACK_DATABASE_URL", database_url)
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")

    from fxstack.runtime.db_tools import migrate_database
    from fxstack.runtime.service import RuntimeService
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])

    svc = RuntimeService(database_url=database_url)
    assert isinstance(svc, RuntimeServiceProtocol), (
        "RuntimeService is missing a method declared on RuntimeServiceProtocol; "
        "update the protocol or the service to bring them back in sync."
    )


def test_every_protocol_method_exists_on_concrete_service() -> None:
    """Loudly fail with a per-method message when the protocol drifts.

    The ``isinstance`` check above bails on the first missing attribute,
    which is fine for CI. For developer ergonomics, this test reports
    *all* missing attributes at once so a refactor doesn't surface
    failures one push at a time.
    """
    from fxstack.runtime.service import RuntimeService

    protocol_attrs = {
        name
        for name in dir(RuntimeServiceProtocol)
        if not name.startswith("_") and name not in {"mro"}
    }
    service_attrs = {name for name in dir(RuntimeService) if not name.startswith("_")}
    missing = protocol_attrs - service_attrs
    assert not missing, (
        f"RuntimeService is missing {sorted(missing)} declared on "
        "RuntimeServiceProtocol — sync the implementation to the contract."
    )


def test_protocol_is_runtime_checkable() -> None:
    """The decorator must be applied so test fakes can pass `isinstance`."""
    # The presence of `_is_runtime_protocol` is what `runtime_checkable` sets.
    assert getattr(RuntimeServiceProtocol, "_is_runtime_protocol", False) is True


def test_a_partial_fake_fails_isinstance_check() -> None:
    """A fake with only a couple of methods must NOT pass `isinstance`.

    This is the actual drift the protocol guards against. If this stops
    working, the protocol has lost its value.
    """

    class _PartialFake:
        def submit_command(self, payload, *, proto="v2"):
            return ({"status": "queued"}, 200)

        def get_state(self):
            return {}

    fake = _PartialFake()
    assert not isinstance(fake, RuntimeServiceProtocol), (
        "A fake stubbing only two methods must fail the protocol check; "
        "if this fires, runtime_checkable behavior has changed and the "
        "protocol no longer catches drift."
    )
