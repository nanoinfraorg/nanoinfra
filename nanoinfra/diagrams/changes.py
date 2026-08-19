"""In-process notification that a saved diagram changed.

``DiagramStore`` announces every write it performs here, and the gateway turns
those into ``diagram_updated`` websocket frames so an open Diagrams view
reflects a change it did not make itself — the agent's ``update_diagram`` tool,
another browser tab, or the REST route.

Deliberately process-local, in the shape of ``nanoinfra/config/watcher.py``'s
callback but without its filesystem watch: the notification is raised by the
writer, so it covers every path that goes *through the store* and only those. A
hand-edited ``diagrams/<id>.json`` or a ``git checkout`` never calls the store
and so is not announced — the gallery still surfaces that on the next read, as
``status: "modified_outside"`` (``DiagramStore.list_diagrams``).

The registry is module-level rather than per-``DiagramStore`` because callers
build a store wherever they need one (``ws_http.py``, each diagram tool, and
per-turn workspace scoping in ``_WorkspaceScopedDiagramTool.store``), so a
listener attached to one instance would miss writes made through another. Each
notification carries its workspace so a listener can ignore writes to a
workspace it does not serve.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from loguru import logger

DiagramChangeKind = Literal["created", "updated", "deleted"]


@dataclass(frozen=True)
class DiagramChange:
    """One write the store performed."""

    workspace: Path
    diagram_id: str
    kind: DiagramChangeKind
    #: The revision now on disk, or ``None`` for a delete.
    revision: int | None = None


DiagramChangeListener = Callable[[DiagramChange], None]

_listeners: list[DiagramChangeListener] = []


def subscribe_diagram_changes(listener: DiagramChangeListener) -> Callable[[], None]:
    """Register *listener*; returns the callable that unregisters it."""
    _listeners.append(listener)

    def unsubscribe() -> None:
        try:
            _listeners.remove(listener)
        except ValueError:
            pass

    return unsubscribe


def notify_diagram_change(change: DiagramChange) -> None:
    """Announce *change* to every listener.

    A listener that raises is logged and skipped: this runs *after* a durable
    write, so letting a notification failure propagate would report a save that
    actually happened as an error, and the caller would retry a write it has
    already made.
    """
    for listener in list(_listeners):
        try:
            listener(change)
        except Exception:
            logger.exception(
                "Diagram change listener failed for {} ({})", change.diagram_id, change.kind
            )


__all__ = [
    "DiagramChange",
    "DiagramChangeKind",
    "DiagramChangeListener",
    "notify_diagram_change",
    "subscribe_diagram_changes",
]
