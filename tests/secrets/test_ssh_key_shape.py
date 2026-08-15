# tests/secrets/test_ssh_key_shape.py
"""A secret of kind `ssh_key` must hold a private key (found live, and it cost hours).

An operator stored their **public** key as an `ssh_key`, because the WebUI value field is a
single-line input and a private key cannot be pasted into one. Every remote action then failed with
`Permission denied`, and the agent guessed that the host had rotated its key.

Two things had to be wrong at once for that to happen. The form could not accept the right value,
and nothing refused the wrong one. This file covers the second half: the store refuses a value that
is not a private key, and the message says which half of the pair the operator pasted.
"""

from __future__ import annotations

import pytest

from nanoinfra.secrets.normalize import SecretValidationError, normalize_secret_input

_PRIVATE = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
_PUBLIC = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH9y8example alberto@BarraHome"


def _payload(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "barrahome",
        "kind": "ssh_key",
        "providerId": "local",
        "value": _PRIVATE,
    }
    base.update(over)
    return base


def test_a_private_key_is_accepted() -> None:
    secret, value = normalize_secret_input(_payload(), secret_id="s1")

    assert secret.kind == "ssh_key"
    assert value == _PRIVATE


def test_a_public_key_is_refused_and_the_message_says_which_half() -> None:
    """The exact mistake an operator made, and the message must name the fix."""
    with pytest.raises(SecretValidationError) as caught:
        normalize_secret_input(_payload(value=_PUBLIC), secret_id="s1")

    message = str(caught.value)
    assert "public" in message
    assert "private" in message


@pytest.mark.parametrize(
    "value",
    [
        "ssh-rsa AAAAB3NzaC1yc2Eexample user@host",
        "ecdsa-sha2-nistp256 AAAAE2VjZHNhexample user@host",
        "sk-ssh-ed25519@openssh.com AAAAexample user@host",
    ],
)
def test_every_public_key_shape_is_refused(value: str) -> None:
    with pytest.raises(SecretValidationError):
        normalize_secret_input(_payload(value=value), secret_id="s1")


def test_a_one_line_value_that_is_no_key_is_refused() -> None:
    """A single-line input collapses a pasted key, so the collapsed result must not save."""
    with pytest.raises(SecretValidationError) as caught:
        normalize_secret_input(_payload(value="b3BlbnNzaC1rZXktdjEAAAAABG5vbmU"), secret_id="s1")

    assert "private key" in str(caught.value)


def test_a_pem_key_is_accepted() -> None:
    """An RSA key in PEM form is a private key too, and this must not refuse it."""
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END RSA PRIVATE KEY-----\n"

    _, value = normalize_secret_input(_payload(value=pem), secret_id="s1")

    assert value == pem


def test_another_kind_keeps_its_freedom() -> None:
    """A password holds anything, including one line that looks like a public key."""
    _, value = normalize_secret_input(
        _payload(kind="password", value=_PUBLIC), secret_id="s1"
    )

    assert value == _PUBLIC
