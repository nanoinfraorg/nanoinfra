from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.webui.secrets_api import (
    create_webui_secret,
    delete_webui_secret,
    update_webui_secret,
    webui_secret_detail_payload,
    webui_secrets_payload,
)


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def test_create_and_list_never_include_value_or_ciphertext(tmp_path: Path):
    store = SecretStore(tmp_path)
    created = create_webui_secret(store, {"name": "n", "kind": "password", "providerId": "local", "value": "s3cr3t"})
    assert "value" not in created["secret"]
    assert "ciphertext" not in created["secret"]

    listing = webui_secrets_payload(store)
    assert listing["secrets"][0]["name"] == "n"
    assert "value" not in listing["secrets"][0]
    assert "ciphertext" not in listing["secrets"][0]


def test_detail_payload_none_for_missing(tmp_path: Path):
    store = SecretStore(tmp_path)
    assert webui_secret_detail_payload(store, "0" * 32) is None


def test_update_and_delete(tmp_path: Path):
    store = SecretStore(tmp_path)
    created = create_webui_secret(store, {"name": "old", "kind": "password", "providerId": "local", "value": "1"})
    secret_id = created["secret"]["id"]

    updated = update_webui_secret(store, secret_id, {"name": "new", "kind": "password", "providerId": "local", "value": "2"})
    assert updated is not None
    assert updated["secret"]["name"] == "new"

    assert delete_webui_secret(store, secret_id) is True
    assert delete_webui_secret(store, secret_id) is False
