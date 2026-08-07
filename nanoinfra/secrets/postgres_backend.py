"""Shared-Postgres secret storage -- the ``postgres`` provider.

Synchronous (psycopg v3 in its default sync mode), matching every other
store in this codebase (DiagramStore, this module's own local branch) being
synchronous file/DB I/O called from async tool ``execute()`` methods. Using
an async driver here would be the only async store in the codebase and
buys nothing, since nothing else in the call chain awaits it either.
"""

from __future__ import annotations

import os
from types import ModuleType
from typing import Any

from nanoinfra.secrets.types import Secret

_ENV_VAR = "NANOINFRA_SECRETS_POSTGRES_DSN"
_TABLE = "nanoinfra_secrets"


class PostgresSecretsNotConfiguredError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            f"Postgres secrets backend is not configured: set the {_ENV_VAR} "
            "environment variable to a Postgres connection string to enable "
            "providerId='postgres' secrets. 'local' secrets are unaffected."
        )


class PostgresSecretsUnavailableError(RuntimeError):
    """Raised when Postgres IS configured but a database operation failed.

    Distinct from ``PostgresSecretsNotConfiguredError`` ("never configured")
    -- this is "configured but currently broken" (e.g. the database is
    temporarily unreachable). Deliberately does NOT include the underlying
    driver exception's text: libpq's own error message for a malformed or
    unreachable connection string can include the DSN itself, password and
    all, and any exception that escapes a tool call gets rendered into the
    agent's transcript -- so the raw driver message must never be
    interpolated into this one.
    """

    def __init__(self) -> None:
        super().__init__(
            "Postgres secrets backend is temporarily unavailable (connection "
            "or query failed). Check that the database is reachable."
        )


def _import_psycopg() -> ModuleType:
    try:
        import psycopg
    except ImportError as exc:
        raise RuntimeError(
            "Postgres secrets backend requires the 'secrets-postgres' extra, "
            "which is not installed. Install it with "
            "`pip install nanoinfra[secrets-postgres]` or "
            "`nanoinfra plugins enable secrets-postgres`."
        ) from exc
    return psycopg


class PostgresBackend:
    def __init__(self) -> None:
        dsn = os.environ.get(_ENV_VAR)
        if not dsn:
            raise PostgresSecretsNotConfiguredError
        self._dsn = dsn

    def _connect(self):  # noqa: ANN202 -- psycopg.Connection, imported lazily below
        psycopg = _import_psycopg()
        return psycopg.connect(self._dsn)

    def ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    ciphertext BYTEA NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def list_secrets(self) -> list[Secret]:
        psycopg = _import_psycopg()
        try:
            self.ensure_schema()
            with self._connect() as conn:
                rows = conn.execute(
                    f"SELECT id, name, kind, ciphertext, created_at, updated_at FROM {_TABLE}"
                ).fetchall()
        except psycopg.Error as exc:
            raise PostgresSecretsUnavailableError() from exc
        return [self._row_to_secret(row) for row in rows]

    def get(self, secret_id: str) -> Secret | None:
        psycopg = _import_psycopg()
        try:
            self.ensure_schema()
            with self._connect() as conn:
                row = conn.execute(
                    f"SELECT id, name, kind, ciphertext, created_at, updated_at FROM {_TABLE} WHERE id = %s",
                    (secret_id,),
                ).fetchone()
        except psycopg.Error as exc:
            raise PostgresSecretsUnavailableError() from exc
        return self._row_to_secret(row) if row else None

    def create(self, secret: Secret) -> None:
        psycopg = _import_psycopg()
        try:
            self.ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {_TABLE} (id, name, kind, ciphertext, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (secret.id, secret.name, secret.kind, secret.ciphertext, secret.created_at, secret.updated_at),
                )
                conn.commit()
        except psycopg.Error as exc:
            raise PostgresSecretsUnavailableError() from exc

    def update(self, secret: Secret) -> None:
        psycopg = _import_psycopg()
        try:
            self.ensure_schema()
            with self._connect() as conn:
                conn.execute(
                    f"""
                    UPDATE {_TABLE}
                    SET name = %s, kind = %s, ciphertext = %s, updated_at = %s
                    WHERE id = %s
                    """,
                    (secret.name, secret.kind, secret.ciphertext, secret.updated_at, secret.id),
                )
                conn.commit()
        except psycopg.Error as exc:
            raise PostgresSecretsUnavailableError() from exc

    def delete(self, secret_id: str) -> bool:
        psycopg = _import_psycopg()
        try:
            self.ensure_schema()
            with self._connect() as conn:
                cursor = conn.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (secret_id,))
                conn.commit()
                return cursor.rowcount > 0
        except psycopg.Error as exc:
            raise PostgresSecretsUnavailableError() from exc

    @staticmethod
    def _row_to_secret(row: tuple[Any, ...]) -> Secret:
        secret_id, name, kind, ciphertext, created_at, updated_at = row
        return Secret(
            id=secret_id,
            name=name,
            kind=kind,
            provider_id="postgres",
            ciphertext=bytes(ciphertext),
            created_at=created_at,
            updated_at=updated_at,
        )


__all__ = ["PostgresBackend", "PostgresSecretsNotConfiguredError", "PostgresSecretsUnavailableError"]
