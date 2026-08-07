"""Workspace-scoped server inventory persistence — one JSON file per server.

Mirrors nanoinfra/diagrams/store.py's DiagramStore exactly: per-entity
files under <workspace>/servers/<uuid4hex>.json, atomic writes, id
validated before touching the filesystem.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanoinfra.servers.normalize import ServerValidationError, normalize_server_input
from nanoinfra.servers.types import Server, ServerSummary
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

_VALID_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ServerStore:
    """Persistent server inventory for one workspace."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "servers"

    def _path(self, server_id: str) -> Path | None:
        if not _VALID_ID_RE.match(server_id):
            return None
        return self.root / f"{server_id}.json"

    def _read(self, path: Path) -> dict[str, Any] | None:
        try:
            with open(path, encoding="utf-8") as f:
                raw_data = json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable server file {}: {}", path, exc)
            return None
        return cast(dict[str, Any], raw_data) if isinstance(raw_data, dict) else None

    def list_servers(self) -> list[ServerSummary]:
        if not self.root.is_dir():
            return []
        summaries: list[ServerSummary] = []
        for path in self.root.glob("*.json"):
            data = self._read(path)
            if data is None:
                continue
            try:
                server = Server.from_dict(data)
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping malformed server file {}", path)
                continue
            summaries.append(
                ServerSummary(
                    id=server.id,
                    name=server.name,
                    provider_id=server.provider_id,
                    tags=server.tags,
                    updated_at=server.updated_at,
                )
            )
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    def get(self, server_id: str) -> Server | None:
        path = self._path(server_id)
        if path is None:
            return None
        data = self._read(path)
        if data is None:
            return None
        try:
            return Server.from_dict(data)
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed server file {}", path)
            return None

    def _check_name_unique(self, name: str, *, exclude_id: str | None) -> None:
        """Names are the de facto foreign key a future name-based lookup
        (get_server, the Diagrams target picker) relies on -- a collision
        should be caught here rather than left to silently return whichever
        record happens to match first."""
        needle = name.lower()
        for existing in self.list_servers():
            if exclude_id is not None and existing.id == exclude_id:
                continue
            if existing.name.lower() == needle:
                raise ServerValidationError(f"a server named {name!r} already exists")

    def create(self, raw: dict[str, Any]) -> Server:
        server_id = uuid.uuid4().hex
        server = normalize_server_input(raw, server_id=server_id)
        self._check_name_unique(server.name, exclude_id=None)
        now = _now_iso()
        server.created_at = now
        server.updated_at = now
        self._write(server)
        return server

    def update(self, server_id: str, raw: dict[str, Any]) -> Server | None:
        existing = self.get(server_id)
        if existing is None:
            return None
        server = normalize_server_input(raw, server_id=server_id)
        self._check_name_unique(server.name, exclude_id=server_id)
        server.created_at = existing.created_at
        server.updated_at = _now_iso()
        self._write(server)
        return server

    def delete(self, server_id: str) -> bool:
        path = self._path(server_id)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def _write(self, server: Server) -> None:
        path = self._path(server.id)
        if path is None:
            raise ValueError(f"Refusing to write server with invalid id: {server.id!r}")
        ensure_dir(self.root)
        _write_text_atomic(path, json.dumps(server.to_dict(), ensure_ascii=False, indent=2))


__all__ = ["ServerStore"]
