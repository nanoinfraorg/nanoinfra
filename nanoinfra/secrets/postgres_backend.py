"""Shared-Postgres secret storage -- the ``postgres`` provider.

Synchronous (psycopg v3 in its default sync mode), matching every other
store in this codebase (DiagramStore, this module's own local branch) being
synchronous file/DB I/O called from async tool ``execute()`` methods. Using
an async driver here would be the only async store in the codebase and
buys nothing, since nothing else in the call chain awaits it either.
"""

from __future__ import annotations

import os
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


class PostgresBackend:
    def __init__(self) -> None:
        dsn = os.environ.get(_ENV_VAR)
        if not dsn:
            raise PostgresSecretsNotConfiguredError
        self._dsn = dsn

    def _connect(self):  # noqa: ANN202 -- psycopg.Connection, imported lazily below
        import psycopg

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
        self.ensure_schema()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, name, kind, ciphertext, created_at, updated_at FROM {_TABLE}"
            ).fetchall()
        return [self._row_to_secret(row) for row in rows]

    def get(self, secret_id: str) -> Secret | None:
        self.ensure_schema()
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT id, name, kind, ciphertext, created_at, updated_at FROM {_TABLE} WHERE id = %s",
                (secret_id,),
            ).fetchone()
        return self._row_to_secret(row) if row else None

    def create(self, secret: Secret) -> None:
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

    def update(self, secret: Secret) -> None:
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

    def delete(self, secret_id: str) -> bool:
        self.ensure_schema()
        with self._connect() as conn:
            cursor = conn.execute(f"DELETE FROM {_TABLE} WHERE id = %s", (secret_id,))
            conn.commit()
            return cursor.rowcount > 0

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


__all__ = ["PostgresBackend", "PostgresSecretsNotConfiguredError"]
