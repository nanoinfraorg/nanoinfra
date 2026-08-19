"""The store announces its own writes, so the WebUI can follow them live."""

from __future__ import annotations

from pathlib import Path

from nanoinfra.diagrams.changes import DiagramChange, subscribe_diagram_changes
from nanoinfra.diagrams.store import DiagramStore


def _collect(recorded: list[DiagramChange]):
    return subscribe_diagram_changes(recorded.append)


def test_create_update_and_delete_are_each_announced(tmp_path: Path) -> None:
    recorded: list[DiagramChange] = []
    unsubscribe = _collect(recorded)
    try:
        store = DiagramStore(tmp_path)
        created = store.create({"name": "Example"})
        store.update(created.id, {"name": "Renamed"})
        store.delete(created.id)
    finally:
        unsubscribe()

    assert [(c.kind, c.diagram_id, c.revision) for c in recorded] == [
        ("created", created.id, 1),
        ("updated", created.id, 2),
        ("deleted", created.id, None),
    ]
    assert {c.workspace for c in recorded} == {tmp_path}


def test_a_write_that_does_not_happen_is_not_announced(tmp_path: Path) -> None:
    """A missing id answers ``None``/``False`` rather than announcing a change."""
    recorded: list[DiagramChange] = []
    unsubscribe = _collect(recorded)
    try:
        store = DiagramStore(tmp_path)
        assert store.update("0" * 32, {"name": "Nope"}) is None
        assert store.delete("0" * 32) is False
    finally:
        unsubscribe()

    assert recorded == []


def test_a_failing_listener_does_not_fail_the_write(tmp_path: Path) -> None:
    """The notification runs after a durable write, so it must not report an error.

    Otherwise a broken listener would make the caller retry -- or the model
    re-apply -- a save that already landed on disk.
    """
    def explode(_change: DiagramChange) -> None:
        raise RuntimeError("listener is broken")

    recorded: list[DiagramChange] = []
    unsubscribe_bad = subscribe_diagram_changes(explode)
    unsubscribe_good = _collect(recorded)
    try:
        store = DiagramStore(tmp_path)
        created = store.create({"name": "Example"})
    finally:
        unsubscribe_bad()
        unsubscribe_good()

    assert store.get(created.id) is not None
    # The healthy listener still heard it.
    assert [c.kind for c in recorded] == ["created"]


def test_unsubscribing_stops_the_notifications(tmp_path: Path) -> None:
    recorded: list[DiagramChange] = []
    unsubscribe = _collect(recorded)
    store = DiagramStore(tmp_path)
    created = store.create({"name": "Example"})
    unsubscribe()
    store.update(created.id, {"name": "Renamed"})

    assert [c.kind for c in recorded] == ["created"]


def test_the_workspace_is_the_store_that_wrote(tmp_path: Path) -> None:
    """A listener needs this to ignore a project it does not serve."""
    recorded: list[DiagramChange] = []
    unsubscribe = _collect(recorded)
    try:
        other = tmp_path / "other-project"
        other.mkdir()
        DiagramStore(tmp_path).create({"name": "Mine"})
        DiagramStore(other).create({"name": "Theirs"})
    finally:
        unsubscribe()

    assert [c.workspace for c in recorded] == [tmp_path, tmp_path / "other-project"]
