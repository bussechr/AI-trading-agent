"""Offline, file-backed encrypted secret store for broker credentials.

This module provides :class:`SecretStore`, a small encrypted key/value store
intended for sensitive local configuration such as broker API keys, account
identifiers, and bridge tokens. It is deliberately **offline**: nothing here
performs network I/O on import or in any code path. Secrets live in a single
JSON file under a configurable directory.

Master key resolution (first hit wins):

1. an explicit ``master_key`` argument to :class:`SecretStore`;
2. the environment variable ``FXSTACK_SECRET_KEY``;
3. a key file (``<dir>/secret.key``), auto-generated on first use if absent.

Encryption backends
-------------------
* **fernet** (preferred): if :mod:`cryptography` is importable, values are
  sealed with :class:`cryptography.fernet.Fernet` (AES-128-CBC + HMAC-SHA256,
  authenticated). This is the recommended, production-grade backend.
* **dev** (stdlib fallback): if ``cryptography`` is *not* installed, a
  pure-stdlib scheme is used: a per-record key is derived with
  :func:`hashlib.scrypt` from the master key plus a random salt, the plaintext
  is XORed against an HMAC-SHA256 keystream, and an HMAC tag authenticates the
  ciphertext. This avoids a hard dependency but is **dev-grade**; install
  ``cryptography`` for anything resembling production. The active backend is
  recorded per record so a store written by one backend is readable as long as
  that backend is available.

Security notes
--------------
* Secret *values* are never logged and never placed in exception messages.
* The master key itself is never written to the encrypted file.
* Errors during decryption raise :class:`SecretStoreError` with a generic
  message (e.g. "wrong key or corrupt data"), never the plaintext.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets as _secrets
from pathlib import Path
from typing import Final

__all__ = [
    "DEFAULT_SECRETS_DIR",
    "ENV_SECRET_KEY",
    "SecretStore",
    "SecretStoreError",
    "active_backend",
    "generate_key",
]

ENV_SECRET_KEY: Final[str] = "FXSTACK_SECRET_KEY"
"""Environment variable consulted for the master key when no explicit key is given."""

# Default location lives next to the package install root's repo, but is fully
# overridable via the constructor. We resolve it relative to this file so the
# default works regardless of the current working directory.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
DEFAULT_SECRETS_DIR: Final[Path] = _REPO_ROOT / ".secrets"
"""Default directory holding the encrypted store and (optionally) the key file."""

_STORE_FILENAME: Final[str] = "secrets.enc.json"
_KEY_FILENAME: Final[str] = "secret.key"
_FILE_SCHEMA: Final[str] = "fxstack.security.secrets.v1"

# dev-backend KDF parameters (scrypt). n must be a power of two.
_SCRYPT_N: Final[int] = 1 << 14
_SCRYPT_R: Final[int] = 8
_SCRYPT_P: Final[int] = 1
_SCRYPT_DKLEN: Final[int] = 32
_SALT_BYTES: Final[int] = 16


class SecretStoreError(RuntimeError):
    """Raised for store-level failures.

    Messages are intentionally generic and never contain secret values.
    """


def _cryptography_available() -> bool:
    """Return True if the optional ``cryptography`` library can be imported."""

    try:  # pragma: no cover - trivial import guard
        import cryptography.fernet  # noqa: F401
    except Exception:  # pragma: no cover - defensive
        return False
    return True


def active_backend() -> str:
    """Return the encryption backend that will be used: ``"fernet"`` or ``"dev"``."""

    return "fernet" if _cryptography_available() else "dev"


def generate_key() -> str:
    """Generate a fresh master key suitable for either backend.

    The key is a URL-safe base64 string of 32 random bytes. For the ``fernet``
    backend this is exactly a valid Fernet key; the ``dev`` backend treats the
    key as opaque key material.
    """

    return base64.urlsafe_b64encode(_secrets.token_bytes(32)).decode("ascii")


def _normalize_master_key(raw: str) -> bytes:
    """Coerce a user-supplied master key string into stable key bytes.

    Accepts arbitrary text; the returned bytes are used directly as Fernet key
    material when already valid, otherwise both backends derive from a stable
    hash of the input so that any non-empty string is a usable passphrase.
    """

    return raw.encode("utf-8")


class _Backend:
    """Encryption strategy interface."""

    name: str

    def encrypt(self, master_key: bytes, plaintext: str) -> dict[str, str]:
        raise NotImplementedError

    def decrypt(self, master_key: bytes, record: dict[str, str]) -> str:
        raise NotImplementedError


class _FernetBackend(_Backend):
    """AEAD backend backed by :class:`cryptography.fernet.Fernet`."""

    name = "fernet"

    @staticmethod
    def _fernet(master_key: bytes):
        from cryptography.fernet import Fernet

        # Fernet requires a 32-byte urlsafe-base64 key. If the provided key is
        # already a valid Fernet key, use it verbatim; otherwise derive a
        # deterministic 32-byte key from it so any passphrase works.
        try:
            return Fernet(master_key)
        except Exception:
            digest = hashlib.sha256(master_key).digest()
            return Fernet(base64.urlsafe_b64encode(digest))

    def encrypt(self, master_key: bytes, plaintext: str) -> dict[str, str]:
        token = self._fernet(master_key).encrypt(plaintext.encode("utf-8"))
        return {"backend": self.name, "ct": token.decode("ascii")}

    def decrypt(self, master_key: bytes, record: dict[str, str]) -> str:
        token = record.get("ct", "").encode("ascii")
        try:
            plaintext = self._fernet(master_key).decrypt(token)
        except Exception:  # noqa: BLE001 - InvalidToken or malformed token -> generic
            raise SecretStoreError("wrong key or corrupt data") from None
        return plaintext.decode("utf-8")


class _DevBackend(_Backend):
    """Pure-stdlib dev-grade backend.

    scrypt-derived per-record key -> HMAC-SHA256 keystream XOR -> HMAC tag.
    Authenticated (encrypt-then-MAC) but not a substitute for ``cryptography``.
    """

    name = "dev"

    @staticmethod
    def _derive(master_key: bytes, salt: bytes) -> bytes:
        return hashlib.scrypt(
            master_key,
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_SCRYPT_DKLEN,
        )

    @staticmethod
    def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
        """HMAC-SHA256 counter-mode keystream of ``length`` bytes."""

        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hmac.new(
                key,
                nonce + counter.to_bytes(8, "big"),
                hashlib.sha256,
            ).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encrypt(self, master_key: bytes, plaintext: str) -> dict[str, str]:
        salt = _secrets.token_bytes(_SALT_BYTES)
        nonce = _secrets.token_bytes(16)
        derived = self._derive(master_key, salt)
        enc_key, mac_key = derived[:16], derived[16:]
        data = plaintext.encode("utf-8")
        keystream = self._keystream(enc_key, nonce, len(data))
        ct = bytes(a ^ b for a, b in zip(data, keystream))
        tag = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        b64 = lambda b: base64.b64encode(b).decode("ascii")  # noqa: E731
        return {
            "backend": self.name,
            "salt": b64(salt),
            "nonce": b64(nonce),
            "ct": b64(ct),
            "tag": b64(tag),
        }

    def decrypt(self, master_key: bytes, record: dict[str, str]) -> str:
        try:
            salt = base64.b64decode(record["salt"])
            nonce = base64.b64decode(record["nonce"])
            ct = base64.b64decode(record["ct"])
            tag = base64.b64decode(record["tag"])
        except Exception:  # noqa: BLE001 - malformed record -> generic error
            raise SecretStoreError("wrong key or corrupt data") from None

        derived = self._derive(master_key, salt)
        enc_key, mac_key = derived[:16], derived[16:]
        expected = hmac.new(mac_key, nonce + ct, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, tag):
            raise SecretStoreError("wrong key or corrupt data")
        keystream = self._keystream(enc_key, nonce, len(ct))
        data = bytes(a ^ b for a, b in zip(ct, keystream))
        return data.decode("utf-8")


_BACKENDS: Final[dict[str, _Backend]] = {
    _FernetBackend.name: _FernetBackend(),
    _DevBackend.name: _DevBackend(),
}


def _backend_for(name: str) -> _Backend:
    backend = _BACKENDS.get(name)
    if backend is None:
        raise SecretStoreError(f"unknown encryption backend: {name!r}")
    if name == "fernet" and not _cryptography_available():
        raise SecretStoreError(
            "record requires the 'cryptography' backend which is not installed; "
            "install the [security] extra to read it"
        )
    return backend


class SecretStore:
    """Offline, file-backed encrypted key/value store for secrets.

    Parameters
    ----------
    directory:
        Directory holding the encrypted JSON file (and, if used, the key file).
        Defaults to :data:`DEFAULT_SECRETS_DIR`.
    master_key:
        Explicit master key. If ``None`` (default), the key is read from the
        ``FXSTACK_SECRET_KEY`` environment variable, then from a key file in
        ``directory`` (auto-generated on first use).
    backend:
        Force a specific backend (``"fernet"`` or ``"dev"``). Defaults to the
        best available backend. New records are written with this backend;
        existing records are decrypted with whatever backend they were sealed
        under.

    Notes
    -----
    The store is read from / written to disk on each mutating call so multiple
    :class:`SecretStore` instances over the same file stay consistent. Secret
    values are never logged and never appear in exception messages.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str] | None = None,
        *,
        master_key: str | None = None,
        backend: str | None = None,
    ) -> None:
        self._dir = Path(directory) if directory is not None else DEFAULT_SECRETS_DIR
        self._path = self._dir / _STORE_FILENAME
        self._key_path = self._dir / _KEY_FILENAME
        self._explicit_key = master_key
        if backend is None:
            backend = active_backend()
        if backend not in _BACKENDS:
            raise SecretStoreError(f"unknown encryption backend: {backend!r}")
        self._write_backend_name = backend

    # -- key resolution -------------------------------------------------

    def _resolve_master_key(self) -> bytes:
        if self._explicit_key:
            return _normalize_master_key(self._explicit_key)
        env_val = os.environ.get(ENV_SECRET_KEY)
        if env_val:
            return _normalize_master_key(env_val)
        return _normalize_master_key(self._read_or_create_key_file())

    def _read_or_create_key_file(self) -> str:
        if self._key_path.exists():
            return self._key_path.read_text(encoding="utf-8").strip()
        self._dir.mkdir(parents=True, exist_ok=True)
        key = generate_key()
        # Write atomically and restrict permissions where supported.
        tmp = self._key_path.with_suffix(self._key_path.suffix + ".tmp")
        tmp.write_text(key, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - non-POSIX best effort
            pass
        os.replace(tmp, self._key_path)
        return key

    # -- file I/O -------------------------------------------------------

    def _load_file(self) -> dict[str, dict[str, str]]:
        if not self._path.exists():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SecretStoreError(f"could not read secret store: {exc.__class__.__name__}") from None
        records = payload.get("records")
        if not isinstance(records, dict):
            return {}
        return records

    def _save_file(self, records: dict[str, dict[str, str]]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = {"schema": _FILE_SCHEMA, "records": records}
        text = json.dumps(payload, indent=2, sort_keys=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:  # pragma: no cover - non-POSIX best effort
            pass
        os.replace(tmp, self._path)

    # -- public API -----------------------------------------------------

    @property
    def path(self) -> Path:
        """Absolute path of the encrypted store file."""

        return self._path

    @property
    def backend(self) -> str:
        """Backend used when writing new records."""

        return self._write_backend_name

    def set(self, name: str, value: str) -> None:
        """Encrypt and persist ``value`` under ``name`` (overwriting any prior)."""

        key = self._validate_name(name)
        if not isinstance(value, str):
            raise SecretStoreError("secret value must be a string")
        master = self._resolve_master_key()
        backend = _backend_for(self._write_backend_name)
        record = backend.encrypt(master, value)
        records = self._load_file()
        records[key] = record
        self._save_file(records)

    def get(self, name: str) -> str | None:
        """Return the decrypted secret for ``name`` or ``None`` if absent."""

        key = self._validate_name(name)
        records = self._load_file()
        record = records.get(key)
        if record is None:
            return None
        backend = _backend_for(record.get("backend", self._write_backend_name))
        master = self._resolve_master_key()
        return backend.decrypt(master, record)

    def delete(self, name: str) -> bool:
        """Remove ``name`` from the store. Returns True if a secret was removed."""

        key = self._validate_name(name)
        records = self._load_file()
        if key not in records:
            return False
        del records[key]
        self._save_file(records)
        return True

    def names(self) -> list[str]:
        """Return the sorted list of stored secret names."""

        return sorted(self._load_file().keys())

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._load_file()

    # -- helpers --------------------------------------------------------

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise SecretStoreError("secret name must be a non-empty string")
        return name
