"""End-to-end tests for POST /api/triggers/{id}/fire.

The route that lets something which can reach a port but cannot run a local command -- a monitor,
a CI job, a backup -- start an automation (nanoinfraorg/nanoinfra#162).

The security claims are the tests, not the docstring: a wrong key and a wrong id return the same
401 so the endpoint is not a trigger directory; no gateway token opens it; an oversized message is
refused rather than truncated; a caller's retry does not fire twice.

The handler's whole job is `store.enqueue`, so HTTP ingress inherits the retry, dead-letter, crash
recovery and run records the CLI path already had. Every test here asserts against the queue rather
than against a turn, because that is where the route stops.
"""

from __future__ import annotations

import asyncio
import random
import socket
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.channels.websocket.runtime import WebSocketChannel, WebSocketConfig
from nanoinfra.triggers.local_store import LocalTriggerStore
from nanoinfra.utils.backoff import BackoffPolicy
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
    store: LocalTriggerStore,
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
        local_trigger_store=store,
    )


def _ch(bus: Any, workspace_path: Path, port: int, store: LocalTriggerStore) -> WebSocketChannel:
    cfg: dict[str, Any] = {
        "enabled": True,
        "allowFrom": ["*"],
        "host": "127.0.0.1",
        "port": port,
        "path": "/",
        "websocketRequiresToken": False,
    }
    return InProcessHttpChannel(cfg, bus, gateway=_make_handler(workspace_path, bus, port, store))


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_inbound = AsyncMock()
    return b



def _dead_letter(tmp_path: Path, store: LocalTriggerStore, trigger_id: str, content: str) -> str:
    """Burn a delivery's attempts until it dead-letters, and return its id.

    Burns through a zero-delay view over the same workspace. The fixture's store carries the
    production backoff, so a retried delivery is not claimable again for seconds and the loop
    would exit with the delivery still sitting in the inbox.
    """
    delivery = store.enqueue(trigger_id, content)
    fast = LocalTriggerStore(tmp_path, backoff=BackoffPolicy(base_delay_ms=0, max_delay_ms=0))
    for _ in range(20):
        claimed = fast.claim_deliveries()
        if not claimed or not fast.retry_delivery(claimed[0], "downstream unavailable"):
            break
    return delivery.id

def _queued(store: LocalTriggerStore) -> int:
    return len(list(store.inbox_dir.glob("*.json")))


class _Fixture:
    def __init__(self, tmp_path: Path, bus: MagicMock) -> None:
        self.store = LocalTriggerStore(tmp_path)
        self.trigger = self.store.create(
            name="CI review",
            channel="websocket",
            chat_id="chat-1",
            session_key="websocket:chat-1",
        )
        key = self.store.issue_key(self.trigger.id)
        assert key is not None
        self.key = key
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.channel = _ch(bus, tmp_path, self.port, self.store)

    def url(self, trigger_id: str | None = None) -> str:
        return f"{self.base_url}/api/triggers/{trigger_id or self.trigger.id}/fire"

    def headers(self, *, key: str | None = None, message: str = "CI failed") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {key if key is not None else self.key}",
            "X-Nanoinfra-Trigger-Message": message,
        }


@pytest.mark.asyncio
async def test_a_valid_key_queues_a_delivery(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(fx.url(), headers=fx.headers())

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["queued"] is True
        assert body["delivery_id"]
        assert _queued(fx.store) == 1
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_no_key_is_rejected(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(
            fx.url(), headers={"X-Nanoinfra-Trigger-Message": "CI failed"}
        )

        assert resp.status_code == 401, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_wrong_key_and_a_wrong_id_are_indistinguishable(
    bus: MagicMock, tmp_path: Path
) -> None:
    """Otherwise the endpoint is a trigger directory: probe ids until the error changes."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        wrong_key = await _http_get(fx.url(), headers=fx.headers(key="ntk_nope"))
        wrong_id = await _http_get(fx.url("does-not-exist"), headers=fx.headers())

        assert wrong_key.status_code == 401
        assert wrong_id.status_code == 401
        assert wrong_key.text == wrong_id.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_gateway_token_does_not_open_this_route(bus: MagicMock, tmp_path: Path) -> None:
    """A key authorises one trigger. The gateway's own token authorises every other route, and
    this one must not accept it."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(fx.url(), headers=fx.headers(key=token))

        assert resp.status_code == 401, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_another_triggers_key_does_not_work(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    other = fx.store.create(
        name="Backup check",
        channel="websocket",
        chat_id="chat-1",
        session_key="websocket:chat-1",
    )
    other_key = fx.store.issue_key(other.id)
    assert other_key is not None

    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(fx.url(), headers=fx.headers(key=other_key))

        assert resp.status_code == 401, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_revoked_key_stops_working(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        assert (await _http_get(fx.url(), headers=fx.headers())).status_code == 200
        assert fx.store.revoke_key(fx.trigger.id) is True

        resp = await _http_get(fx.url(), headers=fx.headers())

        assert resp.status_code == 401, resp.text
        assert _queued(fx.store) == 1
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_missing_message_is_a_400(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(fx.url(), headers={"Authorization": f"Bearer {fx.key}"})

        assert resp.status_code == 400, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_an_oversized_message_is_refused_not_truncated(
    bus: MagicMock, tmp_path: Path
) -> None:
    """A caller told its payload was rejected can adapt. One whose alert was silently cut in half
    cannot -- and the cap sits under MAX_LINE_LENGTH so the answer is a 413 and not a socket the
    transport closed with no status code at all."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(fx.url(), headers=fx.headers(message="x" * 7_000))

        assert resp.status_code == 413, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_repeated_idempotency_key_does_not_fire_twice(
    bus: MagicMock, tmp_path: Path
) -> None:
    """A monitor retrying a timed-out POST is the normal case, not the exceptional one."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        headers = {**fx.headers(), "X-Nanoinfra-Trigger-Idempotency-Key": "alert-42"}

        first = await _http_get(fx.url(), headers=headers)
        second = await _http_get(fx.url(), headers=headers)

        assert first.json() == {"queued": True, "delivery_id": first.json()["delivery_id"]}
        # 200 and not 409: the caller asked for exactly-once and got it, and an error would make
        # it retry again.
        assert second.status_code == 200, second.text
        assert second.json() == {"queued": False, "duplicate": True}
        assert _queued(fx.store) == 1
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_firing_twice_in_a_second_is_rate_limited(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        assert (await _http_get(fx.url(), headers=fx.headers())).status_code == 200
        second = await _http_get(fx.url(), headers=fx.headers(message="again"))

        assert second.status_code == 429, second.text
        assert _queued(fx.store) == 1
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_disabled_trigger_reports_a_conflict(bus: MagicMock, tmp_path: Path) -> None:
    """Distinguished from a bad key on purpose: the caller is authorised, the trigger is off."""
    fx = _Fixture(tmp_path, bus)
    assert fx.store.enable(fx.trigger.id, enabled=False) is not None
    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(fx.url(), headers=fx.headers())

        assert resp.status_code == 409, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_queued_delivery_is_the_one_the_cli_would_write(
    bus: MagicMock, tmp_path: Path
) -> None:
    """One delivery path, not two: this is why HTTP ingress gets the retry and dead-letter free."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        await _http_get(fx.url(), headers=fx.headers(message="CI failed on main"))

        claimed = fx.store.claim_deliveries()
        assert len(claimed) == 1
        assert claimed[0].trigger_id == fx.trigger.id
        assert claimed[0].content == "CI failed on main"
        assert claimed[0].attempts == 0
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_issuing_a_key_needs_an_operator_token(bus: MagicMock, tmp_path: Path) -> None:
    """Firing is authorised by the trigger's key. Minting one is an operator action."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        url = f"{fx.base_url}/api/webui/automations/{fx.trigger.id}/key"

        deny = await _http_get(url)
        assert deny.status_code == 401, deny.text
        # A trigger key must not mint another trigger key.
        with_trigger_key = await _http_get(url, headers={"Authorization": f"Bearer {fx.key}"})
        assert with_trigger_key.status_code == 401, with_trigger_key.text
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_issue_route_returns_a_working_key_once(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        issued = await _http_get(
            f"{fx.base_url}/api/webui/automations/{fx.trigger.id}/key", headers=auth
        )
        assert issued.status_code == 200, issued.text
        key = issued.json()["key"]
        assert key.startswith("ntk_")

        # It works, and it replaced the one the fixture issued.
        assert (await _http_get(fx.url(), headers=fx.headers(key=key))).status_code == 200
        assert fx.store.verify_key(fx.trigger.id, fx.key) is False
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_the_automations_payload_never_carries_the_key(
    bus: MagicMock, tmp_path: Path
) -> None:
    """It reports that a key exists, which an operator needs, and nothing more."""
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"{fx.base_url}/api/webui/automations",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 200, resp.text
        assert fx.key not in resp.text
        entry = next(job for job in resp.json()["jobs"] if job["id"] == fx.trigger.id)
        assert entry["has_key"] is True
        assert "key" not in entry
        assert "key_hash" not in entry
        assert "keyHash" not in resp.text
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_revoking_through_the_route_closes_the_trigger(
    bus: MagicMock, tmp_path: Path
) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        revoked = await _http_get(
            f"{fx.base_url}/api/webui/automations/{fx.trigger.id}/key/revoke", headers=auth
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json() == {"id": fx.trigger.id, "revoked": True}

        assert (await _http_get(fx.url(), headers=fx.headers())).status_code == 401
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_key_routes_404_for_an_unknown_automation(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        issued = await _http_get(f"{fx.base_url}/api/webui/automations/ghost/key", headers=auth)
        revoked = await _http_get(
            f"{fx.base_url}/api/webui/automations/ghost/key/revoke", headers=auth
        )

        assert issued.status_code == 404, issued.text
        assert revoked.status_code == 404, revoked.text
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_dead_letter_can_be_listed_and_replayed(bus: MagicMock, tmp_path: Path) -> None:
    """A delivery in failed/ is a JSON file describing exactly what to do, and until #163 the only
    way to act on it was to read it and retype the command."""
    fx = _Fixture(tmp_path, bus)
    delivery_id = _dead_letter(tmp_path, fx.store, fx.trigger.id, "CI failed")

    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        auth = {"Authorization": f"Bearer {token}"}

        deny = await _http_get(f"{fx.base_url}/api/webui/automations/failed")
        assert deny.status_code == 401, deny.text

        listed = await _http_get(f"{fx.base_url}/api/webui/automations/failed", headers=auth)
        assert listed.status_code == 200, listed.text
        entries = listed.json()["deliveries"]
        assert [item["id"] for item in entries] == [delivery_id]
        assert entries[0]["content"] == "CI failed"
        assert entries[0]["last_error"] == "downstream unavailable"

        replayed = await _http_get(
            f"{fx.base_url}/api/webui/automations/failed/{delivery_id}/replay", headers=auth
        )
        assert replayed.status_code == 200, replayed.text
        body = replayed.json()
        assert body["queued"] is True
        assert body["replay_of"] == delivery_id
        assert body["delivery_id"] != delivery_id
        assert _queued(fx.store) == 1
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_replaying_an_unknown_delivery_is_a_404(bus: MagicMock, tmp_path: Path) -> None:
    fx = _Fixture(tmp_path, bus)
    server_task = asyncio.create_task(fx.channel.start())
    try:
        token = fx.channel.gateway.tokens.issue_api_token(300)
        resp = await _http_get(
            f"{fx.base_url}/api/webui/automations/failed/tdl_nope/replay",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert resp.status_code == 404, resp.text
    finally:
        await fx.channel.stop()
        await server_task


@pytest.mark.asyncio
async def test_a_trigger_key_cannot_replay(bus: MagicMock, tmp_path: Path) -> None:
    """Firing is what a key authorises. Replaying is an operator decision about history."""
    fx = _Fixture(tmp_path, bus)
    delivery_id = _dead_letter(tmp_path, fx.store, fx.trigger.id, "CI failed")

    server_task = asyncio.create_task(fx.channel.start())
    try:
        resp = await _http_get(
            f"{fx.base_url}/api/webui/automations/failed/{delivery_id}/replay",
            headers={"Authorization": f"Bearer {fx.key}"},
        )

        assert resp.status_code == 401, resp.text
        assert _queued(fx.store) == 0
    finally:
        await fx.channel.stop()
        await server_task
