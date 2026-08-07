"""End-to-end tests for the WebUI's /api/webui/diagrams* HTTP routes."""

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
async def test_diagram_routes_require_token(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        for path in ("/api/webui/diagrams", "/api/webui/diagrams/catalog", "/api/webui/diagrams/create"):
            resp = await _http_get(f"{base_url}{path}")
            assert resp.status_code == 401, (path, resp.text)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_crud_round_trip(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        # A brand-new workspace gets auto-seeded with example diagrams (see
        # seed_example_diagram_if_new_workspace) -- this test goes through the
        # same real startup path as the gateway, so it sees them too. Compare
        # against this baseline rather than asserting an empty list.
        initial = await _http_get(f"{base_url}/api/webui/diagrams", headers=auth)
        assert initial.status_code == 200
        baseline_ids = {s["id"] for s in initial.json()["diagrams"]}

        created = await _http_get(
            f"{base_url}/api/webui/diagrams/create",
            headers={
                **auth,
                "X-Nanoinfra-Diagram-Values": json.dumps(
                    {
                        "name": "Example",
                        "targets": ["prod-web-01"],
                        "nodes": [{"id": "a", "position": {"x": 0, "y": 0}, "data": {"label": "A"}}],
                        "edges": [],
                    }
                ),
            },
        )
        assert created.status_code == 200
        diagram_id = created.json()["diagram"]["id"]
        assert diagram_id
        assert created.json()["diagram"]["name"] == "Example"

        listed = await _http_get(f"{base_url}/api/webui/diagrams", headers=auth)
        assert listed.status_code == 200
        summaries = listed.json()["diagrams"]
        new_ids = [s["id"] for s in summaries if s["id"] not in baseline_ids]
        assert new_ids == [diagram_id]
        new_summary = next(s for s in summaries if s["id"] == diagram_id)
        assert new_summary["nodeCount"] == 1

        detail = await _http_get(f"{base_url}/api/webui/diagrams/{diagram_id}", headers=auth)
        assert detail.status_code == 200
        assert detail.json()["diagram"]["targets"] == ["prod-web-01"]

        updated = await _http_get(
            f"{base_url}/api/webui/diagrams/{diagram_id}/update",
            headers={**auth, "X-Nanoinfra-Diagram-Values": json.dumps({"name": "Renamed"})},
        )
        assert updated.status_code == 200
        assert updated.json()["diagram"]["name"] == "Renamed"

        detail_after_update = await _http_get(f"{base_url}/api/webui/diagrams/{diagram_id}", headers=auth)
        assert detail_after_update.json()["diagram"]["name"] == "Renamed"

        deleted = await _http_get(f"{base_url}/api/webui/diagrams/{diagram_id}/delete", headers=auth)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}

        gone = await _http_get(f"{base_url}/api/webui/diagrams/{diagram_id}", headers=auth)
        assert gone.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_detail_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/diagrams/{'a' * 32}", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_delete_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/diagrams/{'a' * 32}/delete", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_create_rejects_invalid_payload(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/diagrams/create",
            headers={**auth, "X-Nanoinfra-Diagram-Values": json.dumps({"nodes": "not-a-list"})},
        )
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_create_rejects_missing_payload(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/diagrams/create", headers=auth)
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_catalog_route_returns_builtin_types(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/diagrams/catalog", headers=auth)
        assert resp.status_code == 200
        component_types = resp.json()["componentTypes"]
        ids = {t["id"] for t in component_types}
        assert "dns" in ids
        assert any(t.get("isGroup") for t in component_types)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_diagram_catalog_route_reflects_workspace_addition(bus: MagicMock, tmp_path: Path) -> None:
    catalog_dir = tmp_path / "diagrams" / "catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "powerdns.json").write_text(
        json.dumps(
            {
                "componentTypeId": "dns",
                "providers": [{"id": "powerdns", "label": "PowerDNS", "kind": "api"}],
            }
        ),
        encoding="utf-8",
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/diagrams/catalog", headers=auth)
        assert resp.status_code == 200
        dns = next(t for t in resp.json()["componentTypes"] if t["id"] == "dns")
        assert "powerdns" in {p["id"] for p in dns["providers"]}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unknown_diagram_route_returns_404_not_spa(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/diagrams/oops/nonsense", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task
