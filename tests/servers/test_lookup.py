from __future__ import annotations

from pathlib import Path

from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.store import ServerStore


def test_resolve_by_exact_id(tmp_path: Path):
    store = ServerStore(tmp_path)
    server = store.create({"name": "prod-web-01", "providerId": "ssh"})
    assert resolve_server(store, server.id) == server


def test_resolve_by_exact_name_case_insensitive(tmp_path: Path):
    store = ServerStore(tmp_path)
    store.create({"name": "Prod-Web-01", "providerId": "ssh"})
    resolved = resolve_server(store, "prod-web-01")
    assert resolved is not None
    assert resolved.name == "Prod-Web-01"


def test_resolve_unknown_returns_none(tmp_path: Path):
    store = ServerStore(tmp_path)
    assert resolve_server(store, "ghost") is None
