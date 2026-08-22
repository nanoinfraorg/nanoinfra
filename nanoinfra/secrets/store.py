"""Workspace-scoped secret persistence, dispatching between two providers.

Mirrors nanoinfra/diagrams/store.py's DiagramStore shape (per-entity files,
atomic writes, validated ids) for the ``local`` provider. Every mutating or
lookup method here (``get``/``create``/``update``/``delete``/
``list_secrets``/``resolve_plaintext``) branches between local JSON files
and the shared Postgres backend (``postgres_backend.py``). ``create`` picks
the storage location from the incoming payload's ``providerId`` -- that is
the only point a location is ever chosen. Every other method dispatches on
where the secret *already* lives (looked up first via a local read or,
failing that, Postgres), never on a caller-supplied ``providerId``, since a
secret's storage location must not move just because a payload happens to
say something different.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.secrets import crypto
from nanoinfra.secrets.normalize import SecretValidationError, normalize_secret_input
from nanoinfra.secrets.postgres_backend import PostgresBackend, PostgresSecretsUnavailableError
from nanoinfra.secrets.types import Secret
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

_VALID_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SecretsStoreUnreadableError(RuntimeError):
    """The store exists and this process may not read it.

    Distinct from an empty store, and the distinction is the whole point. The container splits
    the credential store onto the executor account at mode 700 (see entrypoint.sh), so the agent
    process is refused by the kernel -- and `Path.glob` swallows that refusal, which turned a
    store the operator cannot read into a store the WebUI reported as empty. `entrypoint.sh`
    already records the same fault for the audit log: "Path.glob swallowed the PermissionError,
    and every latch cleared on every boot".

    A page that says "no secrets yet" about a store holding a credential is worse than an error.
    So the refusal travels.
    """


class SecretStore:
    """Persistent secrets for one workspace, dispatching by provider."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "secrets"

    def _path(self, secret_id: str) -> Path | None:
        if not _VALID_ID_RE.match(secret_id):
            return None
        return self.root / f"{secret_id}.json"

    def _read_local(self, secret_id: str) -> Secret | None:
        path = self._path(secret_id)
        if path is None:
            return None
        # The open decides whether the file exists, rather than `is_file()`. `is_file()` answers
        # False for a refusal as well as for an absence, so a store this process may not read
        # reported every record as missing -- the same swallowed refusal as the listing above.
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            # Not "no such secret". A 404 here would tell an operator the record is gone.
            raise SecretsStoreUnreadableError(
                f"the secret store at {self.root} exists and this process may not read it: {exc}"
            ) from exc
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable secret file {}: {}", path, exc)
            return None
        try:
            return Secret.from_storage_dict(data)
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed secret file {}", path)
            return None

    def list_secrets(self) -> list[Secret]:
        secrets = self._list_local()
        if self._postgres_configured():
            try:
                secrets.extend(PostgresBackend().list_secrets())
            except PostgresSecretsUnavailableError as exc:
                # Degrade gracefully: a temporarily-unreachable shared
                # Postgres must not break listing purely-local secrets.
                logger.warning(
                    "Postgres secrets backend unavailable, returning local secrets only: {}", exc
                )
        secrets.sort(key=lambda s: s.updated_at, reverse=True)
        return secrets

    def _list_local(self) -> list[Secret]:
        if not self.root.is_dir():
            return []
        # os.scandir rather than Path.glob: glob answers a refusal with an empty iterator, and an
        # empty answer here reads as "there are no secrets".
        try:
            with os.scandir(self.root) as entries:
                names = [entry.name for entry in entries if entry.name.endswith(".json")]
        except PermissionError as exc:
            raise SecretsStoreUnreadableError(
                f"the secret store at {self.root} exists and this process may not read it: {exc}"
            ) from exc
        result: list[Secret] = []
        for name in names:
            secret = self._read_local(name[: -len(".json")])
            if secret is not None:
                result.append(secret)
        return result

    @staticmethod
    def _postgres_configured() -> bool:
        return bool(os.environ.get("NANOINFRA_SECRETS_POSTGRES_DSN"))

    def get(self, secret_id: str) -> Secret | None:
        local = self._read_local(secret_id)
        if local is not None:
            return local
        if not _VALID_ID_RE.match(secret_id):
            # Ids are validated before touching any path or SQL parameter --
            # this also skips a pointless Postgres round-trip for every
            # name-based lookup miss.
            return None
        if self._postgres_configured():
            return PostgresBackend().get(secret_id)
        return None

    def _check_name_unique(self, name: str, *, exclude_id: str | None) -> None:
        """Names must be unique across BOTH providers -- a collision should
        be caught regardless of where the colliding secret lives, since a
        future name-based lookup would otherwise silently return whichever
        one happens to match first."""
        needle = name.lower()
        for existing in self.list_secrets():
            if exclude_id is not None and existing.id == exclude_id:
                continue
            if existing.name.lower() == needle:
                raise SecretValidationError(f"a secret named {name!r} already exists")

    def create(self, raw: dict[str, Any]) -> Secret:
        secret_id = uuid.uuid4().hex
        secret, plaintext = normalize_secret_input(raw, secret_id=secret_id)
        self._check_name_unique(secret.name, exclude_id=None)
        now = _now_iso()
        secret.ciphertext = crypto.encrypt(plaintext)
        secret.created_at = now
        secret.updated_at = now
        if secret.provider_id == "postgres":
            PostgresBackend().create(secret)
        else:
            self._write_local(secret)
        return secret

    def update(self, secret_id: str, raw: dict[str, Any]) -> Secret | None:
        existing = self.get(secret_id)
        if existing is None:
            return None
        secret, plaintext = normalize_secret_input(raw, secret_id=secret_id)
        self._check_name_unique(secret.name, exclude_id=secret_id)
        secret.ciphertext = crypto.encrypt(plaintext)
        secret.created_at = existing.created_at
        secret.updated_at = _now_iso()
        # A secret's storage location is fixed at creation and cannot move
        # on an edit -- pin provider_id to the existing (real) location,
        # discarding whatever providerId the payload happened to include.
        # This is not just about *this* call's dispatch: if the persisted
        # provider_id field itself were allowed to drift to the payload's
        # value, the NEXT update would read back that drifted value as
        # "existing" and genuinely misroute -- e.g. silently no-op'ing
        # against a Postgres row that was never created.
        secret.provider_id = existing.provider_id
        if existing.provider_id == "postgres":
            PostgresBackend().update(secret)
        else:
            self._write_local(secret)
        return secret

    def delete(self, secret_id: str) -> bool:
        local_path = self._path(secret_id)
        if local_path is not None and local_path.is_file():
            local_path.unlink()
            return True
        if not _VALID_ID_RE.match(secret_id):
            return False
        if self._postgres_configured():
            return PostgresBackend().delete(secret_id)
        return False

    def resolve_plaintext(self, secret_id: str) -> str | None:
        """Decrypt a secret's value. The ONLY method in this module that
        calls crypto.decrypt -- callers outside this class must never call
        crypto.decrypt directly; this is the single seam future execution
        code (Servers module) goes through. Secrets has no agent-facing
        tool at all, so this is never reached from a chat turn."""
        secret = self.get(secret_id)
        if secret is None:
            return None
        return crypto.decrypt(secret.ciphertext)

    def _write_local(self, secret: Secret) -> None:
        path = self._path(secret.id)
        if path is None:
            raise ValueError(f"Refusing to write secret with invalid id: {secret.id!r}")
        try:
            ensure_dir(self.root)
            # ensure_dir only mkdir()s -- it doesn't restrict permissions, so a
            # freshly-created secrets/ directory would otherwise inherit the
            # process umask (commonly world-readable/-executable).
            os.chmod(self.root, 0o700)
        except PermissionError as exc:
            # The container hands this directory to the executor account, so the agent process
            # cannot write here either. Without this the route answered a bare 500.
            raise SecretsStoreUnreadableError(
                f"the secret store at {self.root} belongs to another account, so this process "
                f"cannot write to it: {exc}"
            ) from exc
        _write_text_atomic(path, json.dumps(secret.to_storage_dict(), ensure_ascii=False, indent=2))
        # _write_text_atomic only preserves an *existing* file's permissions
        # -- a brand-new secret file would otherwise inherit the process
        # umask (commonly 0644, world-readable on a shared host).
        os.chmod(path, 0o600)


__all__ = ["SecretStore"]
