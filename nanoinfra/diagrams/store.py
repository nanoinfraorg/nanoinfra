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

from nanoinfra.diagrams.normalize import DiagramValidationError, normalize_diagram
from nanoinfra.diagrams.types import Diagram, DiagramSummary
from nanoinfra.utils.helpers import (
    _write_text_atomic,  # pyright: ignore[reportPrivateUsage]
    ensure_dir,
)

_STORE_VERSION = 1


#: What a ``kind: "secret"`` config field may hold: a reference to the secret store, never a value
#: (#98). The store this product ships is write-only by design -- no tool and no API returns a
#: decrypted value -- which is exactly the property a field the UI labels "stored in Secrets Manager"
#: needs. Nothing in this module referenced that store, so the label was a promise the code did not
#: keep: the value sat in plaintext at 0644 and reached the model in two separate paths.
SECRET_REF_PREFIX = "secret://"

#: What a reader sees where a raw value was already stored. Refusing to read the diagram would make
#: it vanish from the gallery, which is its own defect (#96), so it renders without the value.
SECRET_VALUE_WITHHELD = "[secret withheld: not a secret:// reference]"


class DiagramSecretPolicyError(DiagramValidationError):
    """A secret field was given a value instead of a reference (#98)."""


class DiagramConflictError(RuntimeError):
    """An update carried a revision that is no longer current (#93).

    The apply path had no mtime, no etag and no compare, so a payload the model read before an
    operator's save silently replaced that save. The only trace was a diff printed *after* the write,
    inside a tool result the model may not relay.
    """

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
        """Read a wrapper, and refuse a format this build does not know (#104).

        ``version`` was written and never read, so a file from a later format would have been parsed
        by these rules and then rewritten in this one -- losing whatever the newer format carried.
        Refusing is the honest answer: the operator can downgrade or migrate, and neither is possible
        after a silent rewrite.
        """
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
        stored_version = data.get("version")
        if isinstance(stored_version, int) and not isinstance(stored_version, bool):
            if stored_version > _STORE_VERSION:
                logger.warning(
                    "Skipping diagram file {}: it is format version {} and this build reads {}. "
                    "Rewriting it here would drop whatever the newer format carries.",
                    path,
                    stored_version,
                    _STORE_VERSION,
                )
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

    def revision(self, diagram_id: str) -> int | None:
        """The document's revision, which advances on every write (#93).

        Distinct from the wrapper's ``version``, which is the *format* version.
        """
        wrapper = self._read_wrapper(self._path(diagram_id))
        if wrapper is None:
            return None
        raw = wrapper.get("revision")
        return raw if isinstance(raw, int) and not isinstance(raw, bool) else 1

    def get(self, diagram_id: str) -> Diagram | None:
        path = self._path(diagram_id)
        wrapper = self._read_wrapper(path)
        if wrapper is None:
            return None
        try:
            diagram = normalize_diagram(wrapper["diagram"], diagram_id=diagram_id)
        except Exception:
            logger.warning("Skipping invalid diagram file {}", path)
            return None
        self._withhold_stored_secret_values(diagram)
        return diagram

    def _secret_field_keys(self) -> dict[tuple[str, str], set[str]]:
        """``(componentTypeId, providerId) -> the keys the catalog calls secret``.

        Read from the catalog on each call, the same no-cache choice ``load_catalog`` documents: these
        files are small and admin-edited.
        """
        from nanoinfra.diagrams.catalog import load_catalog

        keys: dict[tuple[str, str], set[str]] = {}
        for component in load_catalog(self.workspace_path):
            for provider in component.providers:
                secret_keys = {
                    field.key for field in provider.fields if field.kind == "secret"
                }
                if secret_keys:
                    keys[(component.id, provider.id)] = secret_keys
        return keys

    def _refuse_secret_values(self, diagram: Diagram) -> None:
        """Refuse a write that carries a secret value rather than a reference (#98)."""
        secret_keys = self._secret_field_keys()
        for node in diagram.nodes:
            wanted = secret_keys.get((node.data.component_type_id, node.data.provider_id))
            if not wanted:
                continue
            for key in sorted(wanted):
                value = node.data.config.get(key)
                if not isinstance(value, str) or not value:
                    continue
                if value.startswith(SECRET_REF_PREFIX) or value == SECRET_VALUE_WITHHELD:
                    continue
                # The message never echoes the value it refused.
                raise DiagramSecretPolicyError(
                    f"node {node.id!r} field {key!r} is a secret field and must hold a reference, "
                    f"not a value. Store the value in Secrets and put "
                    f"{SECRET_REF_PREFIX}<name> here."
                )

    def _withhold_stored_secret_values(self, diagram: Diagram) -> None:
        """Replace a raw value already on disk, so a reader never receives it (#98)."""
        secret_keys = self._secret_field_keys()
        for node in diagram.nodes:
            wanted = secret_keys.get((node.data.component_type_id, node.data.provider_id))
            if not wanted:
                continue
            for key in sorted(wanted):
                value = node.data.config.get(key)
                if not isinstance(value, str) or not value:
                    continue
                if value.startswith(SECRET_REF_PREFIX):
                    continue
                logger.warning(
                    "Diagram {} node {!r} field {!r} holds a stored secret value rather than a "
                    "{} reference. It is withheld from readers; move it into Secrets.",
                    diagram.id,
                    node.id,
                    key,
                    SECRET_REF_PREFIX,
                )
                node.data.config[key] = SECRET_VALUE_WITHHELD

    def create(self, raw: dict[str, Any]) -> Diagram:
        diagram_id = uuid.uuid4().hex
        diagram = normalize_diagram(raw, diagram_id=diagram_id)
        self._refuse_secret_values(diagram)
        now = _now_iso()
        self._write(diagram, created_at=now, updated_at=now)
        return diagram

    def update(
        self,
        diagram_id: str,
        raw: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> Diagram | None:
        """Apply the fields present in *raw*, and leave the rest as they are.

        A key that is absent means "unchanged" and never "empty" (#94). ``store.update(id, {"name":
        "Renamed"})`` used to erase nodes, edges **and** targets and answer HTTP 200, because
        ``normalize_diagram`` reads ``payload.get("nodes", [])`` and the route is a full replace. A
        caller that wants to empty a collection sends an empty list, which is a different fact from
        sending nothing.
        """
        existing = self._read_wrapper(self._path(diagram_id))
        if existing is None:
            return None
        stored_revision = existing.get("revision")
        stored_revision = (
            stored_revision
            if isinstance(stored_revision, int) and not isinstance(stored_revision, bool)
            else 1
        )
        if expected_revision is not None and expected_revision != stored_revision:
            raise DiagramConflictError(
                f"diagram {diagram_id} changed since it was read "
                f"(revision {stored_revision}, this update expected {expected_revision}). "
                "Read it again before applying."
            )
        current = cast(dict[str, Any], existing.get("diagram") or {})
        merged = {**current, **raw}
        diagram = normalize_diagram(merged, diagram_id=diagram_id)
        self._refuse_secret_values(diagram)
        created_at = str(existing.get("created_at") or _now_iso())
        self._write(
            diagram,
            created_at=created_at,
            updated_at=_now_iso(),
            revision=stored_revision + 1,
        )
        return diagram

    def delete(self, diagram_id: str) -> bool:
        path = self._path(diagram_id)
        if path is None or not path.is_file():
            return False
        path.unlink()
        return True

    def _write(
        self,
        diagram: Diagram,
        *,
        created_at: str,
        updated_at: str,
        revision: int = 1,
    ) -> None:
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
            "revision": revision,
            "diagram": diagram.to_dict(),
            "created_at": created_at,
            "updated_at": updated_at,
        }
        _write_text_atomic(path, json.dumps(wrapper, ensure_ascii=False, indent=2))
