"""Tests for the offline encrypted secret store (:mod:`fxstack.security.secrets`).

These tests must pass under the current environment whether or not the optional
``cryptography`` library is installed. We always exercise the pure-stdlib
``dev`` backend explicitly, and additionally exercise ``fernet`` when available.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fxstack.security import (
    ENV_SECRET_KEY,
    SecretStore,
    SecretStoreError,
    active_backend,
    generate_key,
)

# Backends to exercise: always 'dev' (pure stdlib), plus 'fernet' if installed.
_BACKENDS = ["dev"]
if active_backend() == "fernet":
    _BACKENDS.append("fernet")

_KEY = "test-master-key-please-rotate"


def _store(tmp_path: Path, backend: str, **kw) -> SecretStore:
    return SecretStore(tmp_path, master_key=_KEY, backend=backend, **kw)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure the env master key never leaks in from the ambient environment."""

    monkeypatch.delenv(ENV_SECRET_KEY, raising=False)


# --------------------------------------------------------------------------- #
# core behaviour, per backend
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", _BACKENDS)
def test_roundtrip_set_get(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.set("IG_API_KEY", "s3cr3t-value")
    assert store.get("IG_API_KEY") == "s3cr3t-value"


@pytest.mark.parametrize("backend", _BACKENDS)
def test_get_missing_returns_none(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    assert store.get("nope") is None


@pytest.mark.parametrize("backend", _BACKENDS)
def test_overwrite_updates_value(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.set("token", "first")
    store.set("token", "second")
    assert store.get("token") == "second"
    assert store.names() == ["token"]


@pytest.mark.parametrize("backend", _BACKENDS)
def test_delete(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.set("a", "1")
    assert store.delete("a") is True
    assert store.get("a") is None
    # deleting a missing key is a no-op returning False
    assert store.delete("a") is False


@pytest.mark.parametrize("backend", _BACKENDS)
def test_names_sorted_and_contains(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.set("zeta", "z")
    store.set("alpha", "a")
    store.set("mid", "m")
    assert store.names() == ["alpha", "mid", "zeta"]
    assert "alpha" in store
    assert "absent" not in store


@pytest.mark.parametrize("backend", _BACKENDS)
def test_unicode_value_roundtrip(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    value = "pa$$wörd-🔐-Ωmega"
    store.set("u", value)
    assert store.get("u") == value


# --------------------------------------------------------------------------- #
# persistence across instances
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", _BACKENDS)
def test_persistence_across_instances(tmp_path: Path, backend: str) -> None:
    _store(tmp_path, backend).set("broker.pass", "hunter2")
    # brand-new instance, same dir + key
    reopened = _store(tmp_path, backend)
    assert reopened.get("broker.pass") == "hunter2"
    assert reopened.names() == ["broker.pass"]


# --------------------------------------------------------------------------- #
# security properties
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("backend", _BACKENDS)
def test_file_is_not_plaintext(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.set("api", "PLAINTEXT-SENTINEL-12345")
    raw = store.path.read_text(encoding="utf-8")
    assert "PLAINTEXT-SENTINEL-12345" not in raw
    # the file is still valid JSON with a schema + records map
    parsed = json.loads(raw)
    assert "records" in parsed
    assert "api" in parsed["records"]
    assert parsed["records"]["api"]["backend"] == backend


@pytest.mark.parametrize("backend", _BACKENDS)
def test_wrong_key_fails_to_decrypt(tmp_path: Path, backend: str) -> None:
    _store(tmp_path, backend).set("k", "value")
    wrong = SecretStore(tmp_path, master_key="a-different-master-key", backend=backend)
    with pytest.raises(SecretStoreError) as exc:
        wrong.get("k")
    # error message must NOT contain the secret value
    assert "value" not in str(exc.value)


@pytest.mark.parametrize("backend", _BACKENDS)
def test_master_key_not_written_to_file(tmp_path: Path, backend: str) -> None:
    store = _store(tmp_path, backend)
    store.set("k", "v")
    raw = store.path.read_text(encoding="utf-8")
    assert _KEY not in raw


def test_dev_backend_detects_tampering(tmp_path: Path) -> None:
    store = _store(tmp_path, "dev")
    store.set("k", "important")
    # flip ciphertext bytes -> HMAC tag mismatch -> generic error
    parsed = json.loads(store.path.read_text(encoding="utf-8"))
    parsed["records"]["k"]["ct"] = "AAAA" + parsed["records"]["k"]["ct"][4:]
    store.path.write_text(json.dumps(parsed), encoding="utf-8")
    with pytest.raises(SecretStoreError):
        store.get("k")


# --------------------------------------------------------------------------- #
# master-key resolution
# --------------------------------------------------------------------------- #


def test_env_master_key_used_when_no_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SECRET_KEY, "env-provided-key")
    SecretStore(tmp_path, backend="dev").set("k", "v")
    # reopened without explicit key picks up the same env key
    reopened = SecretStore(tmp_path, backend="dev")
    assert reopened.get("k") == "v"
    # no key file was created since env supplied the key
    assert not (tmp_path / "secret.key").exists()


def test_keyfile_generated_and_reused(tmp_path: Path) -> None:
    store = SecretStore(tmp_path, backend="dev")
    store.set("k", "v")
    key_file = tmp_path / "secret.key"
    assert key_file.exists()
    first_key = key_file.read_text(encoding="utf-8")
    # a second instance reuses the same generated key file
    reopened = SecretStore(tmp_path, backend="dev")
    assert reopened.get("k") == "v"
    assert key_file.read_text(encoding="utf-8") == first_key


def test_explicit_key_takes_precedence_over_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    SecretStore(tmp_path, master_key="explicit", backend="dev").set("k", "v")
    monkeypatch.setenv(ENV_SECRET_KEY, "a-totally-different-env-key")
    # explicit key wins, so decryption still succeeds
    store = SecretStore(tmp_path, master_key="explicit", backend="dev")
    assert store.get("k") == "v"


# --------------------------------------------------------------------------- #
# validation + helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["", "   "])
def test_invalid_name_rejected(tmp_path: Path, bad: str) -> None:
    store = _store(tmp_path, "dev")
    with pytest.raises(SecretStoreError):
        store.set(bad, "v")
    with pytest.raises(SecretStoreError):
        store.get(bad)


def test_non_string_value_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path, "dev")
    with pytest.raises(SecretStoreError):
        store.set("k", 123)  # type: ignore[arg-type]


def test_generate_key_unique_and_nonempty() -> None:
    k1, k2 = generate_key(), generate_key()
    assert k1 and k2 and k1 != k2


def test_default_backend_matches_environment(tmp_path: Path) -> None:
    # When backend is unspecified, the best available backend is used.
    store = SecretStore(tmp_path, master_key=_KEY)
    assert store.backend == active_backend()
    store.set("k", "v")
    assert store.get("k") == "v"


def test_unknown_backend_rejected(tmp_path: Path) -> None:
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path, master_key=_KEY, backend="rot13")
