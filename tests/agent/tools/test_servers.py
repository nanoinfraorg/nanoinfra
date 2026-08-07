from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent.tools.loader import ToolLoader
from nanoinfra.agent.tools.servers import (
    CreateServerTool,
    DeleteServerTool,
    GetServerTool,
    ListServersTool,
    UpdateServerTool,
)
from nanoinfra.servers.store import ServerStore


def _decode(value: object) -> object:
    return json.loads(str(value))


def test_server_tools_are_discovered() -> None:
    names = {tool.__name__ for tool in ToolLoader().discover()}
    assert {
        "ListServersTool",
        "GetServerTool",
        "CreateServerTool",
        "UpdateServerTool",
        "DeleteServerTool",
    } <= names


@pytest.mark.asyncio
async def test_create_server_dry_run_does_not_persist(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    tool = CreateServerTool(store)

    result = await tool.execute(name="prod-web-01", providerId="ssh", config={"host": "10.0.1.5"})

    assert not getattr(result, "is_error", False)
    assert "Preview (not created)" in result
    assert store.list_servers() == []


@pytest.mark.asyncio
async def test_create_server_persists_when_dry_run_false(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    tool = CreateServerTool(store)

    result = await tool.execute(
        name="prod-web-01", providerId="ssh", config={"host": "10.0.1.5"}, dry_run=False
    )

    assert not getattr(result, "is_error", False)
    servers = store.list_servers()
    assert len(servers) == 1
    assert servers[0].name == "prod-web-01"


@pytest.mark.asyncio
async def test_list_servers(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    store.create({"name": "prod-web-01", "providerId": "ssh"})

    result = _decode(await ListServersTool(store).execute())
    assert isinstance(result, list)
    assert result[0]["name"] == "prod-web-01"


@pytest.mark.asyncio
async def test_get_server_by_name(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    store.create({"name": "prod-web-01", "providerId": "ssh", "secretRef": "a" * 32})

    result = _decode(await GetServerTool(store).execute(server_id_or_name="prod-web-01"))
    assert result["name"] == "prod-web-01"
    assert result["secretRef"] == "a" * 32


@pytest.mark.asyncio
async def test_get_server_unknown_returns_error(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    result = await GetServerTool(store).execute(server_id_or_name="ghost")
    assert result.is_error


@pytest.mark.asyncio
async def test_update_server_dry_run_then_confirm(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    server = store.create({"name": "old", "providerId": "ssh"})
    tool = UpdateServerTool(store)

    preview = await tool.execute(server_id=server.id, name="new", providerId="ssh")
    assert "Preview (not saved)" in preview
    assert store.get(server.id).name == "old"

    confirmed = await tool.execute(server_id=server.id, name="new", providerId="ssh", dry_run=False)
    assert "new" in confirmed or "Saved" in confirmed
    assert store.get(server.id).name == "new"


@pytest.mark.asyncio
async def test_delete_server_dry_run_then_confirm(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    server = store.create({"name": "n", "providerId": "ssh"})
    tool = DeleteServerTool(store)

    preview = await tool.execute(server_id=server.id)
    assert "Preview (not deleted)" in preview
    assert store.get(server.id) is not None

    confirmed = await tool.execute(server_id=server.id, dry_run=False)
    assert "Deleted" in confirmed
    assert store.get(server.id) is None


@pytest.mark.asyncio
async def test_create_server_rejects_unknown_provider(tmp_path: Path) -> None:
    store = ServerStore(tmp_path)
    tool = CreateServerTool(store)
    result = await tool.execute(name="n", providerId="telnet", dry_run=False)
    assert result.is_error
