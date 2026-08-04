"""Workspace-scoped diagram persistence — one JSON file per diagram.

Each diagram is its own file (``<workspace>/diagrams/<id>.json``), unlike
``LocalTriggerStore``'s single shared ``triggers.json`` — no cross-operation
file lock is needed here since every operation touches exactly one file, and
atomicity comes from ``_write_text_atomic`` per write.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from loguru import logger

from nanoinfra.diagrams.normalize import normalize_diagram
from nanoinfra.diagrams.types import Diagram, DiagramSummary
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

_STORE_VERSION = 1

# Ids are always minted by create() as uuid4().hex, but get()/update()/delete()
# take a caller-supplied id (e.g. straight from a URL path segment) that
# becomes a filename component (_path()) — reject anything that isn't that
# exact shape before touching the filesystem, so a value like "../../etc" can
# never be joined into a path outside self.root.
_VALID_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class DiagramStore:
    """Persistent infra diagrams for one workspace."""

    def __init__(self, workspace_path: Path) -> None:
        self.workspace_path = Path(workspace_path)
        self.root = self.workspace_path / "diagrams"

    def _path(self, diagram_id: str) -> Path | None:
        if not _VALID_ID_RE.match(diagram_id):
            return None
        return self.root / f"{diagram_id}.json"

    def _read_wrapper(self, path: Path | None) -> dict[str, Any] | None:
        if path is None:
            return None
        try:
            with open(path, encoding="utf-8") as f:
                raw_data = json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Skipping unreadable diagram file {}: {}", path, exc)
            return None
        if not isinstance(raw_data, dict):
            logger.warning("Skipping malformed diagram file {}", path)
            return None
        data = cast(dict[str, Any], raw_data)
        if not isinstance(data.get("diagram"), dict):
            logger.warning("Skipping malformed diagram file {}", path)
            return None
        return data

    def list_diagrams(self) -> list[DiagramSummary]:
        if not self.root.is_dir():
            return []
        summaries: list[DiagramSummary] = []
        for path in self.root.glob("*.json"):
            wrapper = self._read_wrapper(path)
            if wrapper is None:
                continue
            diagram_id = path.stem
            try:
                diagram = normalize_diagram(wrapper["diagram"], diagram_id=diagram_id)
            except Exception:
                logger.warning("Skipping invalid diagram file {}", path)
                continue
            updated_at = str(wrapper.get("updated_at") or wrapper.get("created_at") or "")
            summaries.append(
                DiagramSummary(
                    id=diagram.id,
                    name=diagram.name,
                    targets=diagram.targets,
                    node_count=len(diagram.nodes),
                    updated_at=updated_at,
                )
            )
        summaries.sort(key=lambda s: s.updated_at, reverse=True)
        return summaries

    def get(self, diagram_id: str) -> Diagram | None:
        path = self._path(diagram_id)
        wrapper = self._read_wrapper(path)
        if wrapper is None:
            return None
        try:
            return normalize_diagram(wrapper["diagram"], diagram_id=diagram_id)
        except Exception:
            logger.warning("Skipping invalid diagram file {}", path)
            return None

    def create(self, raw: dict[str, Any]) -> Diagram:
        diagram_id = uuid.uuid4().hex
        diagram = normalize_diagram(raw, diagram_id=diagram_id)
        now = _now_iso()
        self._write(diagram, created_at=now, updated_at=now)
        return diagram

    def update(self, diagram_id: str, raw: dict[str, Any]) -> Diagram | None:
        existing = self._read_wrapper(self._path(diagram_id))
        if existing is None:
            return None
        diagram = normalize_diagram(raw, diagram_id=diagram_id)
        created_at = str(existing.get("created_at") or _now_iso())
        self._write(diagram, created_at=created_at, updated_at=_now_iso())
        return diagram

    def delete(self, diagram_id: str) -> bool:
        path = self._path(diagram_id)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def _write(self, diagram: Diagram, *, created_at: str, updated_at: str) -> None:
        path = self._path(diagram.id)
        if path is None:
            # diagram.id is always either a fresh uuid4().hex (create) or an
            # id already validated by _path() earlier in update() — reaching
            # this means a caller bypassed that, which is a bug, not bad
            # external input.
            raise ValueError(f"Refusing to write diagram with invalid id: {diagram.id!r}")
        ensure_dir(self.root)
        wrapper = {
            "version": _STORE_VERSION,
            "diagram": diagram.to_dict(),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        _write_text_atomic(path, json.dumps(wrapper, ensure_ascii=False, indent=2))
