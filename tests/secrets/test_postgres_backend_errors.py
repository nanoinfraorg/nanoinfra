"""Error-handling tests for the Postgres secrets backend that do NOT need a
real, reachable Postgres -- unlike test_postgres_backend.py (which skips
entirely without one), these mock the network/database layer directly.
They still need the ``psycopg`` package itself importable (its real
exception classes are what PostgresBackend's ``except psycopg.Error``
actually catches), so this module still needs the ``secrets-postgres``
extra installed -- it just never needs a reachable Postgres server.

Covers:
- Fix 3: a missing ``psycopg`` install must raise a clear RuntimeError, not
  a raw ModuleNotFoundError.
- Fix 4: a driver error (OperationalError/ProgrammingError/etc.) must be
  re-raised as PostgresSecretsUnavailableError, whose message never
  includes the underlying driver exception's text (which may contain the
  DSN, password and all) -- and SecretStore.list_secrets() must degrade
  gracefully (local secrets only) instead of failing outright.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from nanoinfra.secrets import crypto
from nanoinfra.secrets.postgres_backend import (
    PostgresBackend,
    PostgresSecretsUnavailableError,
)
from nanoinfra.secrets.store import SecretStore

_SENSITIVE_DETAIL = (
    "connection to server failed: FATAL: password authentication failed for "
    "user \"nanoinfra\" using connection string "
    "postgresql://nanoinfra:s3cr3t-db-password@db.internal:5432/app"
)


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    monkeypatch.setenv("NANOINFRA_SECRETS_POSTGRES_DSN", "postgresql://nanoinfra:s3cr3t-db-password@db.internal/app")


def test_connect_raises_clear_error_when_psycopg_not_installed(monkeypatch: pytest.MonkeyPatch):
    """Simulate psycopg missing via sys.modules, without needing to actually
    uninstall it. `import psycopg` inside `_connect` must surface as a
    clear RuntimeError pointing at the secrets-postgres extra, not a raw
    ModuleNotFoundError."""
    monkeypatch.setitem(sys.modules, "psycopg", None)
    backend = PostgresBackend()
    with pytest.raises(RuntimeError, match="secrets-postgres") as excinfo:
        backend._connect()  # noqa: SLF001 -- exercising the wrapping directly
    assert not isinstance(excinfo.value, ModuleNotFoundError)


@pytest.mark.parametrize(
    "method_call",
    [
        lambda backend: backend.list_secrets(),
        lambda backend: backend.get("a" * 32),
        lambda backend: backend.create(_secret()),
        lambda backend: backend.update(_secret()),
        lambda backend: backend.delete("a" * 32),
    ],
    ids=["list_secrets", "get", "create", "update", "delete"],
)
def test_driver_error_is_wrapped_without_leaking_dsn(monkeypatch: pytest.MonkeyPatch, method_call) -> None:
    backend = PostgresBackend()

    def _boom() -> None:
        raise psycopg.OperationalError(_SENSITIVE_DETAIL)

    monkeypatch.setattr(backend, "ensure_schema", _boom)

    with pytest.raises(PostgresSecretsUnavailableError) as excinfo:
        method_call(backend)

    message = str(excinfo.value)
    assert "s3cr3t-db-password" not in message
    assert _SENSITIVE_DETAIL not in message


def _secret():
    from nanoinfra.secrets.types import Secret

    return Secret(
        id="a" * 32,
        name="n",
        kind="password",
        provider_id="postgres",
        ciphertext=b"x",
        created_at="t",
        updated_at="t",
    )


def test_list_secrets_degrades_gracefully_when_postgres_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path)
    store.create({"name": "local-one", "kind": "password", "providerId": "local", "value": "1"})

    def _boom(self: PostgresBackend) -> list:  # noqa: ANN001
        raise PostgresSecretsUnavailableError()

    monkeypatch.setattr(PostgresBackend, "list_secrets", _boom)

    secrets = store.list_secrets()
    assert [s.name for s in secrets] == ["local-one"]


def test_get_raises_typed_error_for_postgres_secret_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path)

    def _boom(self: PostgresBackend, secret_id: str):  # noqa: ANN001
        raise PostgresSecretsUnavailableError()

    monkeypatch.setattr(PostgresBackend, "get", _boom)

    with pytest.raises(PostgresSecretsUnavailableError):
        store.get("a" * 32)


def test_delete_raises_typed_error_for_postgres_secret_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path)

    def _boom(self: PostgresBackend, secret_id: str):  # noqa: ANN001
        raise PostgresSecretsUnavailableError()

    monkeypatch.setattr(PostgresBackend, "delete", _boom)

    with pytest.raises(PostgresSecretsUnavailableError):
        store.delete("a" * 32)


def test_create_raises_typed_error_for_postgres_secret_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SecretStore(tmp_path)

    def _boom(self: PostgresBackend, secret) -> None:  # noqa: ANN001
        raise PostgresSecretsUnavailableError()

    monkeypatch.setattr(PostgresBackend, "create", _boom)

    with pytest.raises(PostgresSecretsUnavailableError):
        store.create({"name": "n", "kind": "password", "providerId": "postgres", "value": "1"})
