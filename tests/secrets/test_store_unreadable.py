# tests/secrets/test_store_unreadable.py
"""An unreadable store is not an empty store.

The container hands `<workspace>/secrets` to the executor account at mode 700, so the agent
process -- which is the one serving the WebUI -- is refused by the kernel. `Path.glob` answers
that refusal with an empty iterator, so the Secrets page said "No secrets yet" about a store
holding an SSH key. `entrypoint.sh` already records the same fault for the audit log.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretsStoreUnreadableError, SecretStore


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    store = SecretStore(tmp_path)
    store.create({"name": "web-key", "kind": "password", "providerId": "local", "value": "x"})
    return store


def _make_unreadable(root: Path) -> None:
    os.chmod(root, 0o000)


def _restore(root: Path) -> None:
    os.chmod(root, stat.S_IRWXU)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_listing_an_unreadable_store_raises_instead_of_returning_empty(store: SecretStore) -> None:
    assert len(store.list_secrets()) == 1
    _make_unreadable(store.root)
    try:
        with pytest.raises(SecretsStoreUnreadableError, match="may not read it"):
            store.list_secrets()
    finally:
        _restore(store.root)
    # And the same store answers again once it can be read, so the error is about access and not
    # about the records.
    assert len(store.list_secrets()) == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_reading_one_secret_from_an_unreadable_store_is_not_a_missing_secret(
    store: SecretStore,
) -> None:
    """A 404 would tell an operator the record is gone."""
    secret_id = store.list_secrets()[0].id
    _make_unreadable(store.root)
    try:
        with pytest.raises(SecretsStoreUnreadableError):
            store.get(secret_id)
    finally:
        _restore(store.root)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")
def test_writing_into_a_store_this_process_does_not_own_names_the_reason(
    store: SecretStore,
) -> None:
    """The route answered a bare 500 for this, which said nothing about the cause.

    The refusal arrives on the read that `create` does first, to check the name is unique. That
    is the honest order: a process that cannot read the store cannot tell whether the name is
    taken, so it must not write either.
    """
    _make_unreadable(store.root)
    try:
        with pytest.raises(SecretsStoreUnreadableError, match=str(store.root)):
            store.create(
                {"name": "second", "kind": "password", "providerId": "local", "value": "y"}
            )
    finally:
        _restore(store.root)


def test_a_store_that_was_never_created_is_still_empty_and_not_an_error(tmp_path: Path) -> None:
    """The distinction has to hold in both directions, or a fresh install reads as broken."""
    assert SecretStore(tmp_path / "fresh").list_secrets() == []
