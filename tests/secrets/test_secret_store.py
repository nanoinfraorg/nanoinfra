# tests/secrets/test_secret_store.py
from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.secrets import crypto
from nanoinfra.secrets.normalize import SecretValidationError
from nanoinfra.secrets.store import SecretStore


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def test_create_assigns_id_and_persists_local(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "db-password", "kind": "password", "providerId": "local", "value": "s3cr3t"})

    assert secret.id
    assert secret.name == "db-password"
    assert (tmp_path / "secrets" / f"{secret.id}.json").is_file()


def test_created_secret_never_holds_plaintext_in_its_own_object(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "n", "kind": "password", "providerId": "local", "value": "s3cr3t"})
    assert b"s3cr3t" not in secret.ciphertext  # ciphertext must not equal/contain the plaintext bytes


def test_resolve_plaintext_round_trips(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "n", "kind": "password", "providerId": "local", "value": "s3cr3t"})
    assert store.resolve_plaintext(secret.id) == "s3cr3t"


def test_get_returns_none_for_unknown_id(tmp_path: Path):
    store = SecretStore(tmp_path)
    assert store.get("0" * 32) is None


def test_list_secrets_reflects_created(tmp_path: Path):
    store = SecretStore(tmp_path)
    store.create({"name": "a", "kind": "password", "providerId": "local", "value": "1"})
    store.create({"name": "b", "kind": "token", "providerId": "local", "value": "2"})
    names = {s.name for s in store.list_secrets()}
    assert names == {"a", "b"}


def test_update_changes_name_and_value(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "old", "kind": "password", "providerId": "local", "value": "1"})
    updated = store.update(secret.id, {"name": "new", "kind": "password", "providerId": "local", "value": "2"})
    assert updated is not None
    assert updated.name == "new"
    assert store.resolve_plaintext(secret.id) == "2"


def test_update_unknown_id_returns_none(tmp_path: Path):
    store = SecretStore(tmp_path)
    assert store.update("0" * 32, {"name": "n", "kind": "password", "providerId": "local", "value": "1"}) is None


def test_delete_removes_secret(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "n", "kind": "password", "providerId": "local", "value": "1"})
    assert store.delete(secret.id) is True
    assert store.get(secret.id) is None


def test_delete_unknown_id_returns_false(tmp_path: Path):
    store = SecretStore(tmp_path)
    assert store.delete("0" * 32) is False


def test_create_rejects_invalid_payload(tmp_path: Path):
    store = SecretStore(tmp_path)
    with pytest.raises(SecretValidationError):
        store.create({"kind": "password", "providerId": "local", "value": "1"})  # missing name


def test_operations_fail_cleanly_when_key_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)
    store = SecretStore(tmp_path)
    with pytest.raises(crypto.SecretsNotConfiguredError):
        store.create({"name": "n", "kind": "password", "providerId": "local", "value": "1"})
