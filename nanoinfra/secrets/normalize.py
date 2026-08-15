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

    if kind == "ssh_key":
        _check_private_key(value)

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


# A public key line names its algorithm first. None of these is a private key, and an operator who
# pastes one gets `Permission denied` from every host, because the backend has no key to sign with.
_PUBLIC_KEY_PREFIXES = ("ssh-", "ecdsa-", "sk-", "sk-ssh-")


def _check_private_key(value: str) -> None:
    """Refuse a value that is not a private key, and say which mistake it is.

    Two mistakes reached a real operator, one after the other. The first stored a public key,
    because the WebUI value field was a single-line input and the public half is the only SSH key
    material that fits on one line. The second stored the private key with every newline replaced
    by a space, because that same input collapsed the paste. Neither value can sign anything, and
    both produced `Permission denied` from the host, which reads as a server problem.

    So the store refuses both, and each message names the fix rather than the symptom.
    """
    text = value.strip()
    first_word = text.split()[0] if text.split() else ""
    if first_word.startswith(_PUBLIC_KEY_PREFIXES):
        raise SecretValidationError(
            "this is a public key, and an ssh_key secret holds the private half. A public key "
            "signs nothing, so every host answers 'Permission denied'. Paste the private key "
            "instead, from '-----BEGIN' to the last '-----'."
        )
    if not text.startswith("-----BEGIN"):
        raise SecretValidationError(
            "an ssh_key secret holds a private key, and this value starts with "
            f"{first_word[:24]!r}. A private key starts with '-----BEGIN'."
        )
    if "\n" not in text:
        raise SecretValidationError(
            "this private key holds no line breaks, so no ssh client can parse it. A single-line "
            "input replaces each newline with a space when it takes a paste. Paste the key into a "
            "multi-line field, or read it from the file."
        )


__all__ = ["SecretValidationError", "normalize_secret_input"]
