"""Encryption for stored secret values.

Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` package) keyed
by the ``NANOINFRA_SECRETS_KEY`` environment variable. The key is never
generated or written to disk by this module -- the operator sets it
themselves (e.g. in the gateway's systemd unit or `.env`), the same "you
own this, we never touch it" rule the design spec applies to
``NANOINFRA_SECRETS_POSTGRES_DSN``.

Both storage providers (local JSON files, Postgres rows) call through
this single module -- only the *persistence* of the resulting ciphertext
differs between them, never how it's produced.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet

_ENV_VAR = "NANOINFRA_SECRETS_KEY"


class SecretsNotConfiguredError(RuntimeError):
    """Raised by encrypt/decrypt when NANOINFRA_SECRETS_KEY is unset."""

    def __init__(self) -> None:
        super().__init__(
            f"Secrets module is not configured: set the {_ENV_VAR} environment "
            "variable to enable it. The gateway runs fine without it -- only "
            "secret storage is unavailable until it's set."
        )


def generate_key_for_setup() -> str:
    """Generate a fresh key an operator can copy into NANOINFRA_SECRETS_KEY.

    Not called anywhere in the app's own startup path -- this exists so a
    human (or this test suite) can produce a valid key value, not so the
    app can silently generate and use one itself.
    """
    return Fernet.generate_key().decode("ascii")


def is_configured() -> bool:
    return bool(os.environ.get(_ENV_VAR))


def _fernet() -> Fernet:
    key = os.environ.get(_ENV_VAR)
    if not key:
        raise SecretsNotConfiguredError
    return Fernet(key.encode("ascii"))


def encrypt(plaintext: str) -> bytes:
    return _fernet().encrypt(plaintext.encode("utf-8"))


def decrypt(ciphertext: bytes) -> str:
    return _fernet().decrypt(ciphertext).decode("utf-8")


__all__ = ["SecretsNotConfiguredError", "decrypt", "encrypt", "generate_key_for_setup", "is_configured"]
