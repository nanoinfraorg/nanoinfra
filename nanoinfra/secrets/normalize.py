"""Validation for untrusted Secret creation/update payloads.

Deliberately does not import crypto.py -- this module's only job is
shape validation, so it stays testable without an encryption key
configured. The caller (store.py) encrypts ``value`` and fills in
``ciphertext``/timestamps after this returns.
"""

from __future__ import annotations

from typing import Any, cast

from nanoinfra.secrets.types import Secret

_VALID_KINDS = {"password", "api_key", "ssh_key", "token"}
_VALID_PROVIDERS = {"local", "postgres"}
_MAX_NAME_LENGTH = 120


class SecretValidationError(ValueError):
    """Raised when a secret payload has a structural problem the client must fix."""


def normalize_secret_input(raw: Any, *, secret_id: str) -> tuple[Secret, str]:
    """Validate an untrusted payload; returns a ``Secret`` (ciphertext=b"") plus the plaintext value."""
    if not isinstance(raw, dict):
        raise SecretValidationError("secret payload must be an object")
    payload = cast(dict[str, Any], raw)

    name = str(payload.get("name") or "").strip()
    if not name:
        raise SecretValidationError("name is required")
    name = name[:_MAX_NAME_LENGTH]

    kind = str(payload.get("kind") or "")
    if kind not in _VALID_KINDS:
        raise SecretValidationError(f"kind must be one of {sorted(_VALID_KINDS)}, got {kind!r}")

    provider_id = str(payload.get("providerId") or "")
    if provider_id not in _VALID_PROVIDERS:
        raise SecretValidationError(f"providerId must be one of {sorted(_VALID_PROVIDERS)}, got {provider_id!r}")

    value = payload.get("value")
    if not isinstance(value, str) or not value:
        raise SecretValidationError("value is required and must be a non-empty string")

    secret = Secret(
        id=secret_id,
        name=name,
        kind=kind,
        provider_id=provider_id,
        ciphertext=b"",
        created_at="",
        updated_at="",
    )
    return secret, value


__all__ = ["SecretValidationError", "normalize_secret_input"]
