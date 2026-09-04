"""The named-agent routes over a real socket (#255, #256).

Two properties, and the second is the one worth a test of its own: the route needs a token like
every other WebUI route, and it answers **names and descriptions only**. What an agent may reach
is the authorization model, and a browser that could enumerate it from the composer's mention menu
would be reading config's authority out of a picker.
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
from nanoinfra.config.schema import Config
from nanoinfra.webui.gateway_services import GatewayServices, build_gateway_services

from .ws_test_client import InProcessHttpChannel
from .ws_test_client import http_get as _http_get

ROSTER = {
    "agents": {
        "named": {
            "sre-prod": {
                "description": "Hands-on checks on production hosts",
                "modelPreset": "kimi-general",
                "toolGroups": ["servers"],
                "skills": ["servers"],
                "addendum": "Prefer read-only checks.",
            },
            "manager": {"description": "Plans work", "delegates": ["sre-prod"]},
        }
    }
}


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
    config = WebSocketConfig.model_validate({
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    })
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
    return InProcessHttpChannel(cfg, bus, gateway=_make_handler(workspace_path, bus, port))


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b


@pytest.mark.asyncio
async def test_the_roster_route_needs_a_token(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        resp = await _http_get(f"http://127.0.0.1:{port}/api/webui/agents/named")
        assert resp.status_code == 401, resp.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_roster_route_answers_names_and_descriptions_only(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nanoinfra.webui.ws_http.load_config",
        lambda *args, **kwargs: Config.model_validate(ROSTER),
    )
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/webui/agents/named",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        body = json.loads(resp.text)
        assert body == {
            "agents": [
                {"name": "sre-prod", "description": "Hands-on checks on production hosts"},
                {"name": "manager", "description": "Plans work"},
            ]
        }
        # The bindings are the authorization model. Asserted against the response *text* rather
        # than the parsed keys, so a payload that grew a nested field would still fail here.
        for leaked in ("kimi-general", "servers", "read-only", "delegates"):
            assert leaked not in resp.text, leaked
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_roster_route_is_empty_when_no_agent_is_named(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which is every deployment today, and what makes the composer's `agent:` prefix disappear
    rather than open an empty menu."""
    monkeypatch.setattr(
        "nanoinfra.webui.ws_http.load_config", lambda *args, **kwargs: Config()
    )
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/webui/agents/named",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert json.loads(resp.text) == {"agents": []}
    finally:
        await channel.stop()
        await server_task


# --- the prompt composition (#256) ------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_prompt_route_needs_a_token(bus: MagicMock, tmp_path: Path) -> None:
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/webui/agents/prompt?agent=sre-prod"
        )
        assert resp.status_code == 401, resp.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_prompt_route_names_every_section_and_its_permission(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "nanoinfra.webui.ws_http.load_config",
        lambda *args, **kwargs: Config.model_validate(ROSTER),
    )
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/webui/agents/prompt?agent=sre-prod",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        body = json.loads(resp.text)
        names = [section["name"] for section in body["sections"]]
        assert names, "a prompt is made of sections and the panel lists them"
        assert all("permission" in section for section in body["sections"])
        assert body["addendum"] == "Prefer read-only checks."
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_prompt_route_answers_404_for_an_agent_that_does_not_exist(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not an empty payload: a panel rendering "no sections" for a typo would be describing a
    prompt that does not exist."""
    monkeypatch.setattr(
        "nanoinfra.webui.ws_http.load_config",
        lambda *args, **kwargs: Config.model_validate(ROSTER),
    )
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/webui/agents/prompt?agent=ghost",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 404, resp.text
    finally:
        await channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_config_replacing_a_fixed_section_is_refused_here_too(
    bus: MagicMock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tool contract and the safety notes cannot be replaced. Surfacing it on this read means
    an operator learns it from the Prompt tab rather than from a turn that fails to assemble."""
    from nanoinfra.agent.prompt_sections import SECTION_PERMISSIONS, SectionPermission

    fixed = next(
        name
        for name, permission in SECTION_PERMISSIONS.items()
        if permission is SectionPermission.FIXED
    )
    monkeypatch.setattr(
        "nanoinfra.webui.ws_http.load_config",
        lambda *args, **kwargs: Config.model_validate({
            "agents": {"named": {"sre-prod": {"promptSections": {fixed: "mine now"}}}}
        }),
    )
    port = _free_port()
    channel = _ch(bus, tmp_path, port)
    server_task = asyncio.create_task(channel.start())
    try:
        token = channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"http://127.0.0.1:{port}/api/webui/agents/prompt?agent=sre-prod",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 400, resp.text
        assert fixed in resp.text
    finally:
        await channel.stop()
        await server_task
