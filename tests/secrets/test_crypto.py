from __future__ import annotations

import pytest

from nanoinfra.secrets import crypto


def test_is_configured_false_without_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)
    assert crypto.is_configured() is False


def test_is_configured_true_with_env_var(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    assert crypto.is_configured() is True


def test_encrypt_decrypt_round_trip(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    ciphertext = crypto.encrypt("s3cr3t-value")
    assert isinstance(ciphertext, bytes)
    assert b"s3cr3t-value" not in ciphertext
    assert crypto.decrypt(ciphertext) == "s3cr3t-value"


def test_encrypt_raises_when_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)
    with pytest.raises(crypto.SecretsNotConfiguredError):
        crypto.encrypt("anything")


def test_decrypt_raises_when_not_configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)
    with pytest.raises(crypto.SecretsNotConfiguredError):
        crypto.decrypt(b"anything")


def test_decrypt_with_wrong_key_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    ciphertext = crypto.encrypt("s3cr3t-value")
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    with pytest.raises(Exception):  # cryptography.fernet.InvalidToken
        crypto.decrypt(ciphertext)
