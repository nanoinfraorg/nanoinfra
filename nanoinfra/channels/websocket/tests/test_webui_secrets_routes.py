"""End-to-end tests for the WebUI's /api/webui/secrets* HTTP routes.

Mirrors test_webui_diagrams_routes.py's harness exactly. In addition to the
usual 401/404/400 coverage, these tests assert -- at the HTTP-response
level, not just via secrets_api.py's own unit tests -- that a "value" or
"ciphertext" key never appears anywhere in a create/list/detail JSON body.
That property (Secret.to_public_dict() only, never to_storage_dict()) is
the single most important thing this module protects.
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
from nanoinfra.secrets.crypto import generate_key_for_setup
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


def _no_secret_leak(payload: Any) -> None:
    """Recursively assert no dict anywhere in ``payload`` has a "value" or
    "ciphertext" key -- the invariant secrets_api.py's docstring promises."""
    if isinstance(payload, dict):
        assert "value" not in payload, payload
        assert "ciphertext" not in payload, payload
        for v in payload.values():
            _no_secret_leak(v)
    elif isinstance(payload, list):
        for item in payload:
            _no_secret_leak(item)


def _assert_no_plaintext_leak(resp: Any, *plaintext_values: str) -> None:
    """Assert none of the given plaintext values appear literally anywhere
    in the raw response body -- not just under the "value"/"ciphertext"
    keys ``_no_secret_leak`` checks. Catches a leak under some other key
    name, or a value echoed verbatim into a 400 error message."""
    for plaintext in plaintext_values:
        assert plaintext not in resp.text, (plaintext, resp.text)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.fixture(autouse=True)
def secrets_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Every test in this file needs a configured store, or create/update
    would fail with 409 before there's anything left to test."""
    key = generate_key_for_setup()
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", key)
    return key


@pytest.mark.asyncio
async def test_secret_routes_require_token(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        for path in ("/api/webui/secrets", "/api/webui/secrets/create"):
            resp = await _http_get(f"{base_url}{path}")
            assert resp.status_code == 401, (path, resp.text)
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_crud_round_trip(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        empty = await _http_get(f"{base_url}/api/webui/secrets", headers=auth)
        assert empty.status_code == 200
        assert empty.json() == {"secrets": []}

        created = await _http_get(
            f"{base_url}/api/webui/secrets/create",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {
                        "name": "Example API Key",
                        "kind": "api_key",
                        "providerId": "local",
                        "value": "sk-super-secret-value",
                    }
                ),
            },
        )
        assert created.status_code == 200
        _assert_no_plaintext_leak(created, "sk-super-secret-value")
        created_json = created.json()
        _no_secret_leak(created_json)
        secret_id = created_json["secret"]["id"]
        assert secret_id
        assert created_json["secret"]["name"] == "Example API Key"

        listed = await _http_get(f"{base_url}/api/webui/secrets", headers=auth)
        assert listed.status_code == 200
        _assert_no_plaintext_leak(listed, "sk-super-secret-value")
        listed_json = listed.json()
        _no_secret_leak(listed_json)
        summaries = listed_json["secrets"]
        assert [s["id"] for s in summaries] == [secret_id]

        detail = await _http_get(f"{base_url}/api/webui/secrets/{secret_id}", headers=auth)
        assert detail.status_code == 200
        _assert_no_plaintext_leak(detail, "sk-super-secret-value")
        detail_json = detail.json()
        _no_secret_leak(detail_json)
        assert detail_json["secret"]["kind"] == "api_key"

        updated = await _http_get(
            f"{base_url}/api/webui/secrets/{secret_id}/update",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {
                        "name": "Renamed",
                        "kind": "api_key",
                        "providerId": "local",
                        "value": "sk-new-secret-value",
                    }
                ),
            },
        )
        assert updated.status_code == 200
        _assert_no_plaintext_leak(updated, "sk-super-secret-value", "sk-new-secret-value")
        updated_json = updated.json()
        _no_secret_leak(updated_json)
        assert updated_json["secret"]["name"] == "Renamed"

        detail_after_update = await _http_get(f"{base_url}/api/webui/secrets/{secret_id}", headers=auth)
        assert detail_after_update.json()["secret"]["name"] == "Renamed"

        deleted = await _http_get(f"{base_url}/api/webui/secrets/{secret_id}/delete", headers=auth)
        assert deleted.status_code == 200
        assert deleted.json() == {"deleted": True}

        gone = await _http_get(f"{base_url}/api/webui/secrets/{secret_id}", headers=auth)
        assert gone.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_detail_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/secrets/{'a' * 32}", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_update_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/secrets/{'a' * 32}/update",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {"name": "x", "kind": "password", "providerId": "local", "value": "v"}
                ),
            },
        )
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_delete_404_for_unknown_id(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/secrets/{'a' * 32}/delete", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_create_rejects_missing_name(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/secrets/create",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {"kind": "password", "providerId": "local", "value": "value-should-not-leak-missing-name"}
                ),
            },
        )
        assert resp.status_code == 400
        _assert_no_plaintext_leak(resp, "value-should-not-leak-missing-name")
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_create_rejects_bad_kind(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/secrets/create",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {
                        "name": "x",
                        "kind": "not-a-kind",
                        "providerId": "local",
                        "value": "value-should-not-leak-bad-kind",
                    }
                ),
            },
        )
        assert resp.status_code == 400
        _assert_no_plaintext_leak(resp, "value-should-not-leak-bad-kind")
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_create_rejects_missing_payload(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/secrets/create", headers=auth)
        assert resp.status_code == 400
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_update_rejects_bad_kind_on_existing_secret(bus: MagicMock, tmp_path: Path) -> None:
    """update() checks existence before validating the new payload, so this
    needs a real secret id to reach the 400 path instead of 404."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        created = await _http_get(
            f"{base_url}/api/webui/secrets/create",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {
                        "name": "x",
                        "kind": "password",
                        "providerId": "local",
                        "value": "value-should-not-leak-existing",
                    }
                ),
            },
        )
        secret_id = created.json()["secret"]["id"]

        resp = await _http_get(
            f"{base_url}/api/webui/secrets/{secret_id}/update",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {
                        "name": "x",
                        "kind": "not-a-kind",
                        "providerId": "local",
                        "value": "value-should-not-leak-update-bad-kind",
                    }
                ),
            },
        )
        assert resp.status_code == 400
        _assert_no_plaintext_leak(resp, "value-should-not-leak-existing", "value-should-not-leak-update-bad-kind")
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_secret_create_returns_409_when_key_not_configured(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one negative-path test for the "store not configured" branch --
    override the autouse fixture's key by deleting it for this test only."""
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(
            f"{base_url}/api/webui/secrets/create",
            headers={
                **auth,
                "X-Nanoinfra-Secret-Values": json.dumps(
                    {"name": "x", "kind": "password", "providerId": "local", "value": "value-should-not-leak-409"}
                ),
            },
        )
        assert resp.status_code == 409
        _assert_no_plaintext_leak(resp, "value-should-not-leak-409")
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_unknown_secret_route_returns_404(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}
        resp = await _http_get(f"{base_url}/api/webui/secrets/oops/nonsense", headers=auth)
        assert resp.status_code == 404
    finally:
        await channel.stop()
        await server_task
