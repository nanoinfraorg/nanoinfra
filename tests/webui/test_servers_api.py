from __future__ import annotations

from pathlib import Path

from nanoinfra.servers.store import ServerStore
from nanoinfra.webui.servers_api import (
    create_webui_server,
    delete_webui_server,
    update_webui_server,
    webui_server_detail_payload,
    webui_servers_payload,
)


def test_create_and_list(tmp_path: Path):
    store = ServerStore(tmp_path)
    created = create_webui_server(store, {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "h"}})
    assert created["server"]["name"] == "prod-web-01"

    listing = webui_servers_payload(store)
    assert listing["servers"][0]["name"] == "prod-web-01"


def test_detail_payload_none_for_missing(tmp_path: Path):
    store = ServerStore(tmp_path)
    assert webui_server_detail_payload(store, "0" * 32) is None


def test_update_and_delete(tmp_path: Path):
    store = ServerStore(tmp_path)
    created = create_webui_server(store, {"name": "old", "providerId": "ssh"})
    server_id = created["server"]["id"]

    updated = update_webui_server(store, server_id, {"name": "new", "providerId": "ssh"})
    assert updated is not None
    assert updated["server"]["name"] == "new"

    assert delete_webui_server(store, server_id) is True
    assert delete_webui_server(store, server_id) is False
