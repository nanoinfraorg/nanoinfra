from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.diagrams.normalize import DiagramValidationError
from nanoinfra.diagrams.store import DiagramStore


def test_create_assigns_id_and_persists(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Example"})

    assert diagram.id
    assert diagram.name == "Example"
    assert (tmp_path / "diagrams" / f"{diagram.id}.json").is_file()


def test_get_round_trips_full_diagram(tmp_path: Path):
    store = DiagramStore(tmp_path)
    raw = {
        "name": "Web app",
        "targets": ["prod-web-01"],
        "nodes": [{"id": "web", "position": {"x": 1, "y": 2}, "data": {"label": "Web", "componentTypeId": "web_server", "providerId": "nginx"}}],
        "edges": [],
    }
    created = store.create(raw)

    loaded = store.get(created.id)
    assert loaded is not None
    assert loaded.name == "Web app"
    assert loaded.targets == ["prod-web-01"]
    assert [n.id for n in loaded.nodes] == ["web"]


def test_get_missing_returns_none(tmp_path: Path):
    store = DiagramStore(tmp_path)
    assert store.get("does-not-exist") is None


def test_list_diagrams_empty_when_no_directory(tmp_path: Path):
    store = DiagramStore(tmp_path)
    assert store.list_diagrams() == []


def test_list_diagrams_returns_summaries_sorted_by_recency(tmp_path: Path):
    store = DiagramStore(tmp_path)
    first = store.create({"name": "First"})
    second = store.create({"name": "Second"})

    summaries = store.list_diagrams()
    ids = [s.id for s in summaries]
    assert set(ids) == {first.id, second.id}
    # Most-recently-updated first.
    assert summaries[0].updated_at >= summaries[1].updated_at


def test_list_diagrams_reflects_node_count(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create(
        {
            "name": "Two nodes",
            "nodes": [
                {"id": "a", "position": {}, "data": {}},
                {"id": "b", "position": {}, "data": {}},
            ],
        }
    )
    summary = next(s for s in store.list_diagrams() if s.id == diagram.id)
    assert summary.node_count == 2


def test_update_replaces_content_and_keeps_id(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Original"})

    updated = store.update(diagram.id, {"name": "Renamed"})
    assert updated is not None
    assert updated.id == diagram.id
    assert updated.name == "Renamed"

    loaded = store.get(diagram.id)
    assert loaded is not None
    assert loaded.name == "Renamed"


def test_update_missing_returns_none(tmp_path: Path):
    store = DiagramStore(tmp_path)
    assert store.update("does-not-exist", {"name": "x"}) is None


def test_update_preserves_created_at(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Original"})
    path = tmp_path / "diagrams" / f"{diagram.id}.json"
    original_created_at = json.loads(path.read_text())["created_at"]

    store.update(diagram.id, {"name": "Renamed"})
    updated_created_at = json.loads(path.read_text())["created_at"]
    assert updated_created_at == original_created_at


def test_update_rejects_invalid_payload(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Original"})
    with pytest.raises(DiagramValidationError):
        store.update(diagram.id, {"nodes": "not-a-list"})


def test_delete_removes_file(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "Temp"})
    assert store.delete(diagram.id) is True
    assert store.get(diagram.id) is None
    assert not (tmp_path / "diagrams" / f"{diagram.id}.json").is_file()


def test_delete_missing_returns_false(tmp_path: Path):
    store = DiagramStore(tmp_path)
    assert store.delete("does-not-exist") is False


def test_list_diagrams_skips_corrupt_file(tmp_path: Path):
    store = DiagramStore(tmp_path)
    good = store.create({"name": "Good"})

    diagrams_dir = tmp_path / "diagrams"
    (diagrams_dir / "corrupt.json").write_text("not json{", encoding="utf-8")

    summaries = store.list_diagrams()
    assert [s.id for s in summaries] == [good.id]


def test_list_diagrams_skips_malformed_wrapper(tmp_path: Path):
    store = DiagramStore(tmp_path)
    good = store.create({"name": "Good"})

    diagrams_dir = tmp_path / "diagrams"
    (diagrams_dir / "malformed.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    (diagrams_dir / "not-an-object.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    summaries = store.list_diagrams()
    assert [s.id for s in summaries] == [good.id]


def test_get_returns_none_for_corrupt_file(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagrams_dir = tmp_path / "diagrams"
    diagrams_dir.mkdir(parents=True)
    (diagrams_dir / "broken.json").write_text("not json{", encoding="utf-8")
    assert store.get("broken") is None


def test_atomic_write_leaves_no_tmp_file_behind(tmp_path: Path):
    store = DiagramStore(tmp_path)
    store.create({"name": "Example"})
    leftovers = list((tmp_path / "diagrams").glob("*.tmp"))
    assert leftovers == []


@pytest.mark.parametrize(
    "malicious_id",
    [
        "../../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "not-a-uuid",
        "",
        "a" * 32 + ";rm -rf",
    ],
)
def test_rejects_path_traversal_and_malformed_ids(tmp_path: Path, malicious_id: str):
    store = DiagramStore(tmp_path)
    # None of these must ever resolve to a path outside the diagrams root.
    assert store.get(malicious_id) is None
    assert store.update(malicious_id, {"name": "x"}) is None
    assert store.delete(malicious_id) is False
    # No file should have been created/touched anywhere on disk.
    assert not (tmp_path / "diagrams").exists() or list((tmp_path / "diagrams").glob("*")) == []
