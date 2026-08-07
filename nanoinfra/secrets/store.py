"""Workspace-scoped secret persistence — one JSON file per local secret.

Mirrors nanoinfra/diagrams/store.py's DiagramStore shape exactly (per-entity
files, atomic writes, validated ids) for the ``local`` provider. Postgres
dispatch is added in Task 4 -- this file's ``create``/``get``/``update``/
``delete`` already branch on ``provider_id`` so that addition only fills in
the other branch, it doesn't restructure anything here.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from nanoinfra.secrets import crypto
from nanoinfra.secrets.normalize import normalize_secret_input
from nanoinfra.secrets.types import Secret
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

_VALID_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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
        if path is None or not path.is_file():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable secret file {}: {}", path, exc)
            return None
        try:
            return Secret.from_storage_dict(data)
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed secret file {}", path)
            return None

    def list_secrets(self) -> list[Secret]:
        if not self.root.is_dir():
            return []
        secrets: list[Secret] = []
        for path in self.root.glob("*.json"):
            secret = self._read_local(path.stem)
            if secret is not None:
                secrets.append(secret)
        secrets.sort(key=lambda s: s.updated_at, reverse=True)
        return secrets

    def get(self, secret_id: str) -> Secret | None:
        return self._read_local(secret_id)

    def create(self, raw: dict[str, Any]) -> Secret:
        secret_id = uuid.uuid4().hex
        secret, plaintext = normalize_secret_input(raw, secret_id=secret_id)
        if secret.provider_id != "local":
            raise NotImplementedError(f"provider {secret.provider_id!r} not supported yet")
        now = _now_iso()
        secret.ciphertext = crypto.encrypt(plaintext)
        secret.created_at = now
        secret.updated_at = now
        self._write_local(secret)
        return secret

    def update(self, secret_id: str, raw: dict[str, Any]) -> Secret | None:
        existing = self._read_local(secret_id)
        if existing is None:
            return None
        secret, plaintext = normalize_secret_input(raw, secret_id=secret_id)
        secret.ciphertext = crypto.encrypt(plaintext)
        secret.created_at = existing.created_at
        secret.updated_at = _now_iso()
        self._write_local(secret)
        return secret

    def delete(self, secret_id: str) -> bool:
        path = self._path(secret_id)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def resolve_plaintext(self, secret_id: str) -> str | None:
        """Decrypt a secret's value. The ONLY method in this module that
        calls crypto.decrypt -- callers outside this class (REST routes,
        agent tools) must never call crypto.decrypt directly; this is the
        single seam future execution code (Servers module) goes through."""
        secret = self._read_local(secret_id)
        if secret is None:
            return None
        return crypto.decrypt(secret.ciphertext)

    def _write_local(self, secret: Secret) -> None:
        path = self._path(secret.id)
        if path is None:
            raise ValueError(f"Refusing to write secret with invalid id: {secret.id!r}")
        ensure_dir(self.root)
        _write_text_atomic(path, json.dumps(secret.to_storage_dict(), ensure_ascii=False, indent=2))


__all__ = ["SecretStore"]
