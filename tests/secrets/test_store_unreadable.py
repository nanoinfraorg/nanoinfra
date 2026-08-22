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


def test_metadata_lists_without_a_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The container's agent account runs without the key, and still has to list what exists.

    Nothing in `to_public_dict` is encrypted -- name, kind, provider and timestamps -- so a
    process holding only the ciphertext can answer the Secrets page and the Servers page. It
    cannot answer `resolve_plaintext`, and that refusal is the point.
    """
    store = SecretStore(tmp_path)
    # The normalizer checks the shape of an ssh_key, so the value has to look like one.
    store.create({
        "name": "web-key",
        "kind": "ssh_key",
        "providerId": "local",
        "value": "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n-----END OPENSSH PRIVATE KEY-----",
    })

    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)
    keyless = SecretStore(tmp_path)

    [listed] = keyless.list_secrets()
    assert (listed.name, listed.kind) == ("web-key", "ssh_key")
    assert "ciphertext" not in listed.to_public_dict()

    from nanoinfra.secrets.crypto import SecretsNotConfiguredError

    with pytest.raises(SecretsNotConfiguredError):
        keyless.resolve_plaintext(listed.id)


def test_writing_keeps_a_mode_the_deployment_already_set(
    tmp_path: Path,
) -> None:
    """The container sets a group read so the agent can list. A write must not take it away."""
    store = SecretStore(tmp_path)
    store.create({"name": "first", "kind": "password", "providerId": "local", "value": "x"})
    os.chmod(store.root, 0o2750)

    store.create({"name": "second", "kind": "password", "providerId": "local", "value": "y"})

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o2750
    assert len(store.list_secrets()) == 2


def test_a_fresh_store_is_created_private(tmp_path: Path) -> None:
    store = SecretStore(tmp_path)
    store.create({"name": "first", "kind": "password", "providerId": "local", "value": "x"})

    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
