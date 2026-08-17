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


def test_update_applies_what_it_was_sent_and_keeps_the_rest(tmp_path: Path):
    """A rename preserves everything it did not mention -- #94.

    This test used to run against a diagram with no nodes, so it could never notice that a rename
    erased them. It runs against content now, because that is the only version of this test that can
    fail when the defect returns.
    """
    store = DiagramStore(tmp_path)
    diagram = store.create({
        "name": "Original",
        "targets": ["prod-web-01"],
        "nodes": [{"id": "web", "label": "Web"}, {"id": "db", "label": "DB"}],
        "edges": [{"id": "e1", "source": "web", "target": "db"}],
    })

    updated = store.update(diagram.id, {"name": "Renamed"})

    assert updated is not None
    assert updated.id == diagram.id
    assert updated.name == "Renamed"
    assert [node.id for node in updated.nodes] == ["web", "db"]
    assert [edge.id for edge in updated.edges] == ["e1"]
    assert updated.targets == ["prod-web-01"]

    loaded = store.get(diagram.id)
    assert loaded is not None
    assert loaded.name == "Renamed"
    assert [node.id for node in loaded.nodes] == ["web", "db"]


def test_update_with_an_explicit_empty_list_still_empties(tmp_path: Path):
    """Sending ``[]`` is a decision, and sending nothing is not -- the other half of #94."""
    store = DiagramStore(tmp_path)
    diagram = store.create({
        "name": "Original",
        "nodes": [{"id": "web", "label": "Web"}],
        "edges": [],
    })

    updated = store.update(diagram.id, {"nodes": []})

    assert updated is not None
    assert updated.nodes == []
    assert updated.name == "Original", "and the field that was not sent is still unchanged"


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


def test_list_diagrams_reports_a_corrupt_file(tmp_path: Path):
    """It used to skip it, and the operator got no error at all -- #96.

    The file is still on disk, so an absence sends them looking for something they still have. The
    gallery says it cannot be read.
    """
    store = DiagramStore(tmp_path)
    good = store.create({"name": "Good"})

    diagrams_dir = tmp_path / "diagrams"
    (diagrams_dir / "corrupt.json").write_text("not json{", encoding="utf-8")

    summaries = store.list_diagrams()

    by_status = {s.status for s in summaries}
    assert by_status == {"ok", "unreadable"}
    assert next(s for s in summaries if s.status == "ok").id == good.id
    assert "corrupt.json" in next(s for s in summaries if s.status == "unreadable").name


def test_list_diagrams_reports_a_malformed_wrapper(tmp_path: Path):
    store = DiagramStore(tmp_path)
    good = store.create({"name": "Good"})

    diagrams_dir = tmp_path / "diagrams"
    (diagrams_dir / "malformed.json").write_text(json.dumps({"version": 1}), encoding="utf-8")
    (diagrams_dir / "not-an-object.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    summaries = store.list_diagrams()

    assert [s.id for s in summaries if s.status == "ok"] == [good.id]
    assert len([s for s in summaries if s.status == "unreadable"]) == 2


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


def test_a_revision_advances_with_every_write(tmp_path: Path):
    """A per-document revision, so an apply can carry what it read -- #93.

    The wrapper's ``version`` field is the *format* version and nothing read it (#104). There was no
    mtime, no etag and no compare anywhere in the apply path, so a stale payload silently replaced
    whatever the operator had saved in between.
    """
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "A", "nodes": [{"id": "web", "label": "Web"}]})

    first = store.revision(diagram.id)
    assert first is not None

    store.update(diagram.id, {"name": "B"})

    assert store.revision(diagram.id) == first + 1


def test_an_update_with_a_stale_revision_is_refused(tmp_path: Path):
    from nanoinfra.diagrams.store import DiagramConflictError

    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "A", "nodes": [{"id": "web", "label": "Web"}]})
    read_at = store.revision(diagram.id)

    # Somebody else saves in between.
    store.update(diagram.id, {"nodes": [{"id": "web", "label": "Web"}, {"id": "lb", "label": "LB"}]})

    with pytest.raises(DiagramConflictError) as caught:
        store.update(diagram.id, {"name": "Renamed"}, expected_revision=read_at)

    assert "changed" in str(caught.value).lower()
    loaded = store.get(diagram.id)
    assert loaded is not None
    assert [node.id for node in loaded.nodes] == ["web", "lb"], "the other write survives"


def test_an_update_with_the_current_revision_succeeds(tmp_path: Path):
    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "A"})

    updated = store.update(diagram.id, {"name": "B"}, expected_revision=store.revision(diagram.id))

    assert updated is not None
    assert updated.name == "B"


def test_a_stored_format_version_from_the_future_is_refused(tmp_path: Path):
    """The version field was write-only, so a later format would have been misread -- #104."""
    import json as _json

    store = DiagramStore(tmp_path)
    diagram = store.create({"name": "A", "nodes": [{"id": "web", "label": "Web"}]})
    path = next((tmp_path / "diagrams").glob("*.json"))
    wrapper = _json.loads(path.read_text(encoding="utf-8"))
    wrapper["version"] = 99
    path.write_text(_json.dumps(wrapper), encoding="utf-8")

    assert store.get(diagram.id) is None, "a format this build cannot read must not be guessed at"
    listed = next(s for s in store.list_diagrams() if s.id == diagram.id)
    assert listed.status == "unreadable", "and the operator is told, rather than left with a gap"


def _secret_node(value: str) -> dict:
    return {
        "id": "db",
        "position": {"x": 0, "y": 0},
        "data": {
            "label": "DB",
            "componentTypeId": "database",
            "providerId": "postgres",
            "config": {"image": "postgres:17", "password": value},
        },
    }


class TestASecretFieldHoldsAReference:
    """The UI promises "stored in Secrets Manager" and nothing made it true -- #98.

    The catalog declares `{"key":"password","kind":"secret"}`, the inspector renders it as a password
    field with that placeholder, and no file in `nanoinfra/diagrams/` referenced the secret store at
    all. Measured: the value landed in plaintext on disk at 0644, in the WebUI detail payload, in the
    `/infradiagrams` prompt block, and in the `get_diagram` tool result. So an operator types a
    production password into a field labelled "stored in Secrets Manager" and it leaves for the model
    provider.
    """

    def test_a_raw_value_is_refused(self, tmp_path: Path):
        store = DiagramStore(tmp_path)

        with pytest.raises(DiagramValidationError) as caught:
            store.create({"name": "Prod", "nodes": [_secret_node("hunter2")], "edges": []})

        message = str(caught.value)
        assert "password" in message
        assert "secret://" in message, "the refusal has to say what to write instead"
        assert "hunter2" not in message, "and it must not echo the value it refused"

    def test_a_reference_is_stored(self, tmp_path: Path):
        store = DiagramStore(tmp_path)

        diagram = store.create({
            "name": "Prod",
            "nodes": [_secret_node("secret://prod-db-password")],
            "edges": [],
        })

        assert diagram.nodes[0].data.config["password"] == "secret://prod-db-password"
        on_disk = next((tmp_path / "diagrams").glob("*.json")).read_text(encoding="utf-8")
        assert "secret://prod-db-password" in on_disk

    def test_an_empty_value_is_still_allowed(self, tmp_path: Path):
        store = DiagramStore(tmp_path)

        diagram = store.create({"name": "Prod", "nodes": [_secret_node("")], "edges": []})

        assert diagram.nodes[0].data.config["password"] == ""

    def test_a_value_already_on_disk_never_reaches_a_reader(self, tmp_path: Path):
        """An existing diagram keeps working, and stops leaking.

        Refusing to read it would make the diagram vanish from the gallery, which is the failure mode
        in #96. It renders, with the value replaced and a warning for the operator.
        """
        import json as _json

        store = DiagramStore(tmp_path)
        diagram = store.create({"name": "Prod", "nodes": [_secret_node("")], "edges": []})
        path = next((tmp_path / "diagrams").glob("*.json"))
        wrapper = _json.loads(path.read_text(encoding="utf-8"))
        wrapper["diagram"]["nodes"][0]["data"]["config"]["password"] = "hunter2"
        path.write_text(_json.dumps(wrapper), encoding="utf-8")

        loaded = store.get(diagram.id)

        assert loaded is not None
        assert loaded.nodes[0].data.config["password"] != "hunter2"
        assert "secret" in loaded.nodes[0].data.config["password"].lower()

    def test_a_field_the_catalog_does_not_call_secret_is_untouched(self, tmp_path: Path):
        store = DiagramStore(tmp_path)

        diagram = store.create({
            "name": "Prod",
            "nodes": [_secret_node("")],
            "edges": [],
        })

        assert diagram.nodes[0].data.config["image"] == "postgres:17"
