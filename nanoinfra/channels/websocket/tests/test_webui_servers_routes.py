"""End-to-end tests for the WebUI's /api/webui/servers* HTTP routes.

Mirrors test_webui_secrets_routes.py's harness exactly (which itself mirrors
test_webui_diagrams_routes.py). Unlike secrets, servers has a single storage
location and no "not configured" state, so there is no 409 case here.
"""

from __future__ import annotations

import asyncio
import json
import random
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.webui.gateway_services import GatewayServices, build_gateway_services

from .ws_test_client import InProcessHttpChannel
from .ws_test_client import http_get as _http_get


def _free_port() -> int:
    for _ in range(100):
        port = random.randint(30_000, 60_000)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("could not find a free localhost port")


def _make_handler(workspace_path: Path, bus: Any, port: int) -> GatewayServices:
    config = WebSocketConfig.model_validate(
        {
            "enabled": True,
            "allowFrom": ["*"],
            "host": "127.0.0.1",
            "port": port,
            "path": "/",
            "websocketRequiresToken": False,
        }
    )
    return build_gateway_services(
        config=config,
        bus=bus,
        session_manager=None,
        static_dist_path=None,
        workspace_path=workspace_path,
        default_restrict_to_workspace=False,
        runtime_model_name=None,
        runtime_surface="browser",
        runtime_capabilities_overrides=None,
    )


def _ch(bus: Any, workspace_path: Path, port: int) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    gateway = _make_handler(workspace_path, bus, port)
    return InProcessHttpChannel(cfg, bus, gateway=gateway)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_server_routes_require_token(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        for path in ("/api/webui/servers", "/api/webui/servers/create"):
            resp = await _http_get(f"{base_url}{path}")
            assert resp.status_code == 401, (path, resp.text)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_crud_round_trip(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        empty = await _http_get(f"{base_url}/api/webui/servers", headers=auth)
        assert empty.status_code == 200
        assert empty.json() == {"servers": []}

        created = await _http_get(
            f"{base_url}/api/webui/servers/create",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {
                        "name": "prod-web-01",
                        "providerId": "ssh",
                        "config": {"host": "10.0.1.5", "port": "22", "username": "deploy"},
                        "secretRef": "b" * 32,
                        "tags": ["prod", "web"],
                    }
                ),
            },
        )
        assert created.status_code == 200
        created_json = created.json()
        server_id = created_json["server"]["id"]
        assert server_id
        assert created_json["server"]["name"] == "prod-web-01"
        assert created_json["server"]["providerId"] == "ssh"
        assert created_json["server"]["config"] == {
            "host": "10.0.1.5",
            "port": "22",
            "username": "deploy",
        }
        assert created_json["server"]["secretRef"] == "b" * 32
        assert created_json["server"]["tags"] == ["prod", "web"]

        listed = await _http_get(f"{base_url}/api/webui/servers", headers=auth)
        assert listed.status_code == 200
        listed_json = listed.json()
        summaries = listed_json["servers"]
        assert [s["id"] for s in summaries] == [server_id]

        detail = await _http_get(f"{base_url}/api/webui/servers/{server_id}", headers=auth)
        assert detail.status_code == 200
        detail_json = detail.json()
        assert detail_json["server"]["providerId"] == "ssh"
        assert detail_json["server"]["config"] == {
            "host": "10.0.1.5",
            "port": "22",
            "username": "deploy",
        }
        assert detail_json["server"]["secretRef"] == "b" * 32
        assert detail_json["server"]["tags"] == ["prod", "web"]

        updated = await _http_get(
            f"{base_url}/api/webui/servers/{server_id}/update",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {
                        "name": "Renamed",
                        "providerId": "api",
                        "config": {"baseUrl": "https://example.com"},
                        "secretRef": None,
                        "tags": [],
                    }
                ),
            },
        )
        assert updated.status_code == 200
        updated_json = updated.json()
        assert updated_json["server"]["name"] == "Renamed"
        assert updated_json["server"]["providerId"] == "api"
        assert updated_json["server"]["config"] == {"baseUrl": "https://example.com"}
        assert updated_json["server"]["secretRef"] is None
        assert updated_json["server"]["tags"] == []

        detail_after_update = await _http_get(f"{base_url}/api/webui/servers/{server_id}", headers=auth)
        detail_after_update_json = detail_after_update.json()
        assert detail_after_update_json["server"]["name"] == "Renamed"
        assert detail_after_update_json["server"]["providerId"] == "api"

        deleted = await _http_get(f"{base_url}/api/webui/servers/{server_id}/delete", headers=auth)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}

        gone = await _http_get(f"{base_url}/api/webui/servers/{server_id}", headers=auth)
        assert gone.status_code == 404

        deleted_again = await _http_get(f"{base_url}/api/webui/servers/{server_id}/delete", headers=auth)
        assert deleted_again.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_detail_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/servers/{'a' * 32}", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_update_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/servers/{'a' * 32}/update",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {"name": "x", "providerId": "ssh", "config": {}, "secretRef": None, "tags": []}
                ),
            },
        )
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_delete_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/servers/{'a' * 32}/delete", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_create_rejects_missing_name(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/servers/create",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps({"providerId": "ssh", "config": {}}),
            },
        )
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_create_rejects_bad_provider_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/servers/create",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {"name": "x", "providerId": "not-a-provider", "config": {}}
                ),
            },
        )
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_create_rejects_missing_payload(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/servers/create", headers=auth)
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_update_rejects_bad_provider_id_on_existing_server(bus: MagicMock, tmp_path: Path) -> None:
    """update() checks existence before validating the new payload, so this
    needs a real server id to reach the 400 path instead of 404."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        created = await _http_get(
            f"{base_url}/api/webui/servers/create",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {"name": "x", "providerId": "ssh", "config": {}}
                ),
            },
        )
        server_id = created.json()["server"]["id"]

        resp = await _http_get(
            f"{base_url}/api/webui/servers/{server_id}/update",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {"name": "x", "providerId": "not-a-provider", "config": {}}
                ),
            },
        )
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_server_update_rejects_missing_payload(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        created = await _http_get(
            f"{base_url}/api/webui/servers/create",
            headers={
                **auth,
                "X-Nanoinfra-Server-Values": json.dumps(
                    {"name": "x", "providerId": "ssh", "config": {}}
                ),
            },
        )
        server_id = created.json()["server"]["id"]
        resp = await _http_get(f"{base_url}/api/webui/servers/{server_id}/update", headers=auth)
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unknown_server_route_returns_404(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/servers/oops/nonsense", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task
