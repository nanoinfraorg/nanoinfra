"""One chokepoint for a diagram write.

``if dry_run:`` was the whole gate, and ``dry_run`` is a parameter the **model** supplies in its own
call (#95). Nothing recorded that a preview happened, nothing compared the applied payload against the
previewed one, and nothing represented an operator's answer:

- preview one payload and apply a different one -- accepted with no complaint
- a first-and-only call with ``dry_run=false`` emptied a diagram completely

``capability_class = "mutate.local"`` is not in ``_GATED_CLASSES``, so the capability gate never saw a
diagram write, and the audit viewer's ``mutate.local`` filter had nothing to show.

This reuses the shape the executor already uses for remote execution rather than inventing another one:
a digest binds an answer to the exact bytes it was given. The digest lives on the server, keyed by
diagram id, so a model cannot assert that a preview happened.

What it does not do: stop ``write_file`` from touching ``diagrams/*.json``. That writer is ungated and
``exec`` can ``rm``, so prevention is not available from here (#96). The store detects a write it did
not make, and the gallery reports it.
"""

from __future__ import annotations

import hashlib
import json
import time
from enum import Enum
from pathlib import Path
from typing import Any, cast

from loguru import logger

#: Where a preview record lives. Beside the workspace's diagrams rather than inside ``diagrams/``, so
#: it never appears in the gallery or in a listing the model reads as content.
_PREVIEW_DIR_NAME = ".diagram-previews"

#: A confirmation from hours ago is not a confirmation of what is on disk now.
_PREVIEW_TTL_SECONDS = 3600.0


class PreviewOutcome(Enum):
    """Why an apply may proceed, or may not."""

    OK = "ok"
    NO_PREVIEW = "no_preview"
    MISMATCH = "mismatch"
    STALE = "stale"


def payload_digest(payload: dict[str, Any]) -> str:
    """A digest of what will be written, stable across key order.

    The model does not control field order, so order must not decide whether an apply is allowed.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _preview_path(workspace: Path, diagram_id: str) -> Path:
    safe = hashlib.sha256(diagram_id.encode("utf-8")).hexdigest()
    return Path(workspace) / _PREVIEW_DIR_NAME / f"{safe}.json"


def record_preview(workspace: Path, diagram_id: str, digest: str, *, revision: int | None) -> None:
    """Remember that this exact payload was shown, and what it was based on.

    The record holds a digest and never the payload: it sits in the workspace, which the agent's
    filesystem tools can read, and a copy of the payload there would be a second source of truth.
    """
    path = _preview_path(workspace, diagram_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "diagram_id": diagram_id,
            "digest": digest,
            "revision": revision,
            "created_at": time.time(),
        }),
        encoding="utf-8",
    )


def preview_revision(workspace: Path, diagram_id: str) -> int | None:
    """The revision the recorded preview was built from, if there is one."""
    record = _read_preview(workspace, diagram_id)
    if record is None:
        return None
    value = record.get("revision")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _read_preview(workspace: Path, diagram_id: str) -> dict[str, Any] | None:
    path = _preview_path(workspace, diagram_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    record = cast(dict[str, Any], raw)
    if record.get("diagram_id") != diagram_id:
        return None
    return record


def consume_preview(workspace: Path, diagram_id: str, digest: str) -> PreviewOutcome:
    """Check an apply against the preview that authorized it, and spend that preview.

    Spent once on purpose: one answer authorizes one write, so a second apply previews again.
    """
    record = _read_preview(workspace, diagram_id)
    if record is None:
        return PreviewOutcome.NO_PREVIEW
    created_at = record.get("created_at")
    age = time.time() - float(created_at) if isinstance(created_at, (int, float)) else None
    if age is None or age > _PREVIEW_TTL_SECONDS:
        _forget_preview(workspace, diagram_id)
        return PreviewOutcome.STALE
    if record.get("digest") != digest:
        # Kept rather than spent: the model may re-preview, and dropping it here would turn a
        # mismatch into "no preview" on the next attempt, which names the wrong problem.
        return PreviewOutcome.MISMATCH
    _forget_preview(workspace, diagram_id)
    return PreviewOutcome.OK


def _forget_preview(workspace: Path, diagram_id: str) -> None:
    with_suppress = _preview_path(workspace, diagram_id)
    try:
        with_suppress.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove spent diagram preview {}", with_suppress)


def refusal_message(outcome: PreviewOutcome, diagram_id: str) -> str:
    """What the model is told, in terms it can act on."""
    if outcome is PreviewOutcome.NO_PREVIEW:
        return (
            f"Not saved -- no preview of this change was shown for diagram {diagram_id!r}. "
            "Call update_diagram with dry_run=true first, show the user the diff, and apply the "
            "same payload only after they confirm."
        )
    if outcome is PreviewOutcome.MISMATCH:
        return (
            "Not saved -- this payload is not the one that was previewed. The user confirmed the "
            "preview, so apply exactly that, or preview this new payload and ask again."
        )
    return (
        "Not saved -- the preview of this change is too old to stand as a confirmation. "
        "Preview it again and ask the user."
    )


def _audit_store() -> Any:
    """The audit store a diagram write records into.

    The same store the gates use, and for the same reason it lives outside the workspace: an audit log
    the agent's filesystem tools can edit is not an audit log.
    """
    from nanoinfra.config.paths import get_data_dir
    from nanoinfra.gates.audit import AuditStore

    return AuditStore(get_data_dir() / "gates")


def record_diagram_write(*, diagram_id: str, tool: str, summary: str) -> None:
    """Write one audit record for a diagram change.

    A failure here is logged and does not stop the write. For a *gated* action the opposite rule
    holds -- a missing record turns a refusal into a pass -- and a diagram write is not gated, so the
    risk runs the other way: losing an edit the operator confirmed because a log could not be written
    would be the worse outcome.
    """
    try:
        _audit_store().record(
            decision="allow",
            capability_class="mutate.local",
            execution_context="interactive",
            tool=tool,
            command=f"diagram {diagram_id}: {summary}",
            reason="diagram write",
        )
    except Exception:
        logger.warning("Could not record the audit entry for a diagram write", exc_info=True)
