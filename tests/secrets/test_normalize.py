from __future__ import annotations

import pytest

from nanoinfra.secrets.normalize import SecretValidationError, normalize_secret_input


def test_normalize_extracts_name_kind_provider_and_value():
    secret, value = normalize_secret_input(
        {"name": "prod-db-password", "kind": "password", "providerId": "local", "value": "s3cr3t"},
        secret_id="a" * 32,
    )
    assert secret.id == "a" * 32
    assert secret.name == "prod-db-password"
    assert secret.kind == "password"
    assert secret.provider_id == "local"
    assert value == "s3cr3t"
    assert secret.ciphertext == b""  # crypto.py fills this in, not this module


def test_normalize_rejects_missing_name():
    with pytest.raises(SecretValidationError, match="name"):
        normalize_secret_input({"kind": "password", "providerId": "local", "value": "x"}, secret_id="a" * 32)


def test_normalize_rejects_unknown_kind():
    with pytest.raises(SecretValidationError, match="kind"):
        normalize_secret_input(
            {"name": "n", "kind": "not-a-real-kind", "providerId": "local", "value": "x"},
            secret_id="a" * 32,
        )


def test_normalize_rejects_unknown_provider():
    with pytest.raises(SecretValidationError, match="providerId"):
        normalize_secret_input(
            {"name": "n", "kind": "password", "providerId": "not-a-real-provider", "value": "x"},
            secret_id="a" * 32,
        )


def test_normalize_rejects_empty_value():
    with pytest.raises(SecretValidationError, match="value"):
        normalize_secret_input(
            {"name": "n", "kind": "password", "providerId": "local", "value": ""},
            secret_id="a" * 32,
        )
