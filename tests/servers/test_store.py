from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.servers.normalize import ServerValidationError
from nanoinfra.servers.store import ServerStore


def test_create_assigns_id_and_persists(tmp_path: Path):
    store = ServerStore(tmp_path)
    server = store.create({"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})

    assert server.id
    assert server.name == "prod-web-01"
    assert (tmp_path / "servers" / f"{server.id}.json").is_file()


def test_get_round_trips_full_server(tmp_path: Path):
    store = ServerStore(tmp_path)
    created = store.create(
        {"name": "n", "providerId": "ssh", "config": {"host": "h"}, "secretRef": "a" * 32, "tags": ["prod"]}
    )
    fetched = store.get(created.id)
    assert fetched == created


def test_get_returns_none_for_unknown_id(tmp_path: Path):
    store = ServerStore(tmp_path)
    assert store.get("0" * 32) is None


def test_list_servers_returns_summaries_sorted_by_recency(tmp_path: Path):
    store = ServerStore(tmp_path)
    first = store.create({"name": "first", "providerId": "ssh"})
    second = store.create({"name": "second", "providerId": "api"})
    summaries = store.list_servers()
    assert [s.id for s in summaries] == [second.id, first.id]


def test_update_changes_fields(tmp_path: Path):
    store = ServerStore(tmp_path)
    server = store.create({"name": "old", "providerId": "ssh", "config": {"host": "h1"}})
    updated = store.update(server.id, {"name": "new", "providerId": "ssh", "config": {"host": "h2"}})
    assert updated is not None
    assert updated.name == "new"
    assert updated.config == {"host": "h2"}


def test_update_unknown_id_returns_none(tmp_path: Path):
    store = ServerStore(tmp_path)
    assert store.update("0" * 32, {"name": "n", "providerId": "ssh"}) is None


def test_delete_removes_server(tmp_path: Path):
    store = ServerStore(tmp_path)
    server = store.create({"name": "n", "providerId": "ssh"})
    assert store.delete(server.id) is True
    assert store.get(server.id) is None


def test_delete_unknown_id_returns_false(tmp_path: Path):
    store = ServerStore(tmp_path)
    assert store.delete("0" * 32) is False


def test_create_rejects_invalid_payload(tmp_path: Path):
    store = ServerStore(tmp_path)
    with pytest.raises(ServerValidationError):
        store.create({"providerId": "ssh"})  # missing name


def test_list_servers_skips_corrupt_file(tmp_path: Path):
    store = ServerStore(tmp_path)
    store.create({"name": "good", "providerId": "ssh"})
    (tmp_path / "servers" / "deadbeefdeadbeefdeadbeefdeadbeef.json").write_text("not json", encoding="utf-8")
    summaries = store.list_servers()
    assert len(summaries) == 1
    assert summaries[0].name == "good"


def test_create_rejects_duplicate_name(tmp_path: Path):
    store = ServerStore(tmp_path)
    store.create({"name": "prod-web-01", "providerId": "ssh"})
    with pytest.raises(ServerValidationError, match="already exists"):
        store.create({"name": "prod-web-01", "providerId": "ssh"})


def test_create_rejects_duplicate_name_case_insensitively(tmp_path: Path):
    store = ServerStore(tmp_path)
    store.create({"name": "Prod-Web-01", "providerId": "ssh"})
    with pytest.raises(ServerValidationError, match="already exists"):
        store.create({"name": "prod-web-01", "providerId": "ssh"})


def test_update_rejects_name_collision_with_different_server(tmp_path: Path):
    store = ServerStore(tmp_path)
    store.create({"name": "one", "providerId": "ssh"})
    two = store.create({"name": "two", "providerId": "ssh"})

    with pytest.raises(ServerValidationError, match="already exists"):
        store.update(two.id, {"name": "one", "providerId": "ssh"})


def test_update_allows_renaming_to_its_own_current_name(tmp_path: Path):
    store = ServerStore(tmp_path)
    server = store.create({"name": "same", "providerId": "ssh"})

    updated = store.update(server.id, {"name": "same", "providerId": "api"})

    assert updated is not None
    assert updated.name == "same"
    assert updated.provider_id == "api"


def test_create_rejects_duplicate_name_after_truncation(tmp_path: Path):
    """Two names that only become identical after _MAX_NAME_LENGTH (120)
    truncation must still be caught -- the uniqueness check runs against
    the already-truncated name, not the raw payload."""
    store = ServerStore(tmp_path)
    base = "x" * 120
    store.create({"name": base, "providerId": "ssh"})
    with pytest.raises(ServerValidationError, match="already exists"):
        store.create({"name": base + "-extra-that-gets-truncated-away", "providerId": "ssh"})
