from __future__ import annotations

import os
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nanoinfra.secrets import crypto  # noqa: E402
from nanoinfra.secrets.store import SecretStore  # noqa: E402

_DSN = os.environ.get("NANOINFRA_TEST_POSTGRES_DSN")


def _skip_if_no_postgres() -> None:
    if not _DSN:
        pytest.skip("NANOINFRA_TEST_POSTGRES_DSN not set -- skipping Postgres-backed secrets tests")
    try:
        with psycopg.connect(_DSN, connect_timeout=2):
            pass
    except Exception as exc:
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.fixture(autouse=True)
def _setup(monkeypatch: pytest.MonkeyPatch):
    _skip_if_no_postgres()
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.setenv("NANOINFRA_SECRETS_POSTGRES_DSN", _DSN or "")
    # Clean slate: drop the table so each test starts fresh.
    with psycopg.connect(_DSN) as conn:  # type: ignore[arg-type]
        conn.execute("DROP TABLE IF EXISTS nanoinfra_secrets")
        conn.commit()
    yield


def test_create_postgres_secret_persists_and_lists(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "shared-prod-key", "kind": "api_key", "providerId": "postgres", "value": "abc123"})
    assert secret.provider_id == "postgres"

    listed = store.list_secrets()
    assert any(s.id == secret.id and s.name == "shared-prod-key" for s in listed)


def test_get_and_resolve_plaintext_for_postgres_secret(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "n", "kind": "token", "providerId": "postgres", "value": "xyz"})
    assert store.get(secret.id) is not None
    assert store.resolve_plaintext(secret.id) == "xyz"


def test_update_and_delete_postgres_secret(tmp_path: Path):
    store = SecretStore(tmp_path)
    secret = store.create({"name": "old", "kind": "token", "providerId": "postgres", "value": "1"})
    updated = store.update(secret.id, {"name": "new", "kind": "token", "providerId": "postgres", "value": "2"})
    assert updated is not None
    assert store.resolve_plaintext(secret.id) == "2"
    assert store.delete(secret.id) is True
    assert store.get(secret.id) is None


def test_list_secrets_merges_local_and_postgres(tmp_path: Path):
    store = SecretStore(tmp_path)
    store.create({"name": "local-one", "kind": "password", "providerId": "local", "value": "1"})
    store.create({"name": "pg-one", "kind": "password", "providerId": "postgres", "value": "2"})
    names = {s.name for s in store.list_secrets()}
    assert names == {"local-one", "pg-one"}


def test_postgres_ops_fail_cleanly_when_dsn_not_configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)
    store = SecretStore(tmp_path)
    from nanoinfra.secrets.postgres_backend import PostgresSecretsNotConfiguredError

    with pytest.raises(PostgresSecretsNotConfiguredError):
        store.create({"name": "n", "kind": "password", "providerId": "postgres", "value": "1"})
