"""End-to-end tests for /api/webui/automations/{id}/state and .../state/reset.

Mirrors test_webui_secrets_routes.py's harness. The property these protect is the one prose state
could never offer: an operator can see what an automation believes and can tell it to forget
(nanoinfraorg/nanoinfra#158). The 404 coverage matters as much as the happy path -- a state
document outlives the automation that wrote it if a delete ever misses, so existence is checked
against the automation rather than against the file.
"""

from __future__ import annotations

import asyncio
import random
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.automations.state import AutomationStateStore
from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.cron.service import CronService
from nanoinfra.cron.types import CronSchedule
from nanoinfra.triggers.local_store import LocalTriggerStore
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


def _make_handler(
    workspace_path: Path,
    bus: Any,
    port: int,
    *,
    cron_service: CronService | None,
    local_trigger_store: LocalTriggerStore | None,
) -> GatewayServices:
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
        cron_service=cron_service,
        local_trigger_store=local_trigger_store,
    )


def _ch(
    bus: Any,
    workspace_path: Path,
    port: int,
    *,
    cron_service: CronService | None = None,
    local_trigger_store: LocalTriggerStore | None = None,
) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    gateway = _make_handler(
        workspace_path,
        bus,
        port,
        cron_service=cron_service,
        local_trigger_store=local_trigger_store,
    )
    return InProcessHttpChannel(cfg, bus, gateway=gateway)


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_automation_state_is_readable_and_resettable(
    bus: MagicMock, tmp_path: Path
) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cron = CronService(tmp_path / "cron" / "jobs.json")
    job = cron.add_job(
        name="Blockers",
        schedule=CronSchedule(kind="cron", expr="0 9 * * 1", tz="UTC"),
        message="Check blockers",
        session_key="websocket:abc",
        origin_channel="websocket",
        origin_chat_id="abc",
    )
    AutomationStateStore(tmp_path).set(job.id, "reported", [47, 51])

    channel = _ch(bus, tmp_path, port, cron_service=cron)
    server_task = asyncio.create_task(channel.start())
    try:
        deny = await _http_get(f"{base_url}/api/webui/automations/{job.id}/state")
        assert deny.status_code == 401, deny.text

        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(
            f"{base_url}/api/webui/automations/{job.id}/state", headers=auth
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"id": job.id, "values": {"reported": [47, 51]}}

        reset = await _http_get(
            f"{base_url}/api/webui/automations/{job.id}/state/reset", headers=auth
        )
        assert reset.status_code == 200, reset.text
        assert reset.json() == {"id": job.id, "cleared": True}

        after = await _http_get(
            f"{base_url}/api/webui/automations/{job.id}/state", headers=auth
        )
        assert after.json() == {"id": job.id, "values": {}}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_reset_requires_a_token(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cron = CronService(tmp_path / "cron" / "jobs.json")
    job = cron.add_job(
        name="Blockers",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check blockers",
        session_key="websocket:abc",
        origin_channel="websocket",
        origin_chat_id="abc",
    )
    AutomationStateStore(tmp_path).set(job.id, "reported", [47])

    channel = _ch(bus, tmp_path, port, cron_service=cron)
    server_task = asyncio.create_task(channel.start())
    try:
        deny = await _http_get(f"{base_url}/api/webui/automations/{job.id}/state/reset")
        assert deny.status_code == 401, deny.text
        # The unauthorised call must not have cleared anything.
        assert AutomationStateStore(tmp_path).snapshot(job.id) == {"reported": [47]}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_an_unknown_automation_is_a_404_even_with_a_state_document(
    bus: MagicMock, tmp_path: Path
) -> None:
    """A stale document must not make a deleted automation look alive."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cron = CronService(tmp_path / "cron" / "jobs.json")
    AutomationStateStore(tmp_path).set("ghost", "reported", [47])

    channel = _ch(bus, tmp_path, port, cron_service=cron)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(f"{base_url}/api/webui/automations/ghost/state", headers=auth)
        assert resp.status_code == 404, resp.text

        reset = await _http_get(
            f"{base_url}/api/webui/automations/ghost/state/reset", headers=auth
        )
        assert reset.status_code == 404, reset.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_trigger_carries_state_through_the_same_route(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Cron jobs and triggers are one surface, so they are one route."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    triggers = LocalTriggerStore(tmp_path)
    trigger = triggers.create(
        name="CI review",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )
    AutomationStateStore(tmp_path).set(trigger.id, "last_sha", "abc123")

    channel = _ch(bus, tmp_path, port, local_trigger_store=triggers)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        resp = await _http_get(
            f"{base_url}/api/webui/automations/{trigger.id}/state", headers=auth
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["values"] == {"last_sha": "abc123"}
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_resetting_state_that_was_never_written_is_not_an_error(
    bus: MagicMock, tmp_path: Path
) -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    cron = CronService(tmp_path / "cron" / "jobs.json")
    job = cron.add_job(
        name="Blockers",
        schedule=CronSchedule(kind="every", every_ms=3_600_000),
        message="Check blockers",
        session_key="websocket:abc",
        origin_channel="websocket",
        origin_chat_id="abc",
    )

    channel = _ch(bus, tmp_path, port, cron_service=cron)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        reset = await _http_get(
            f"{base_url}/api/webui/automations/{job.id}/state/reset", headers=auth
        )
        assert reset.status_code == 200, reset.text
        assert reset.json() == {"id": job.id, "cleared": False}
    finally:
        await channel.stop()
        await server_task
