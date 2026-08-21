from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nanoinfra.bus.events import InboundMessage
from nanoinfra.command.builtin import build_help_text, register_builtin_commands
from nanoinfra.command.router import CommandContext, CommandRouter
from nanoinfra.session.keys import UNIFIED_SESSION_KEY
from nanoinfra.triggers.local_store import LocalTriggerStore


@pytest.mark.asyncio
async def test_trigger_command_creates_session_bound_local_trigger(tmp_path: Path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    store = LocalTriggerStore(tmp_path)
    loop = SimpleNamespace(workspace=tmp_path, local_trigger_store=store)
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content="/trigger@nanoinfra_bot PR review",
        metadata={"webui": True},
    )
    ctx = CommandContext(
        msg=msg,
        session=None,
        key="websocket:chat-1",
        raw="/trigger@nanoinfra_bot PR review",
        loop=loop,
    )

    assert router.is_dispatchable_command("/trigger@nanoinfra_bot PR review") is True
    response = await router.dispatch(ctx)

    assert response is not None
    assert "Trigger created: PR review" in response.content
    trigger = store.list_for_session("websocket:chat-1")[0]
    assert trigger.name == "PR review"
    assert trigger.channel == "websocket"
    assert trigger.chat_id == "chat-1"
    assert trigger.session_key == "websocket:chat-1"
    assert f"nanoinfra trigger {trigger.id} \"message\"" in response.content


@pytest.mark.asyncio
async def test_trigger_command_stores_mentions_as_references_not_as_context_blocks(
    tmp_path: Path,
) -> None:
    """`/trigger` writes to a store, so the same rule as cron applies to its origin metadata."""
    from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META, RuntimeContextBlock

    router = CommandRouter()
    register_builtin_commands(router)
    store = LocalTriggerStore(tmp_path)
    loop = SimpleNamespace(workspace=tmp_path, local_trigger_store=store)
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content="/trigger uptime watch",
        metadata={
            "webui": True,
            RUNTIME_CONTEXT_INPUT_META: [
                RuntimeContextBlock(source="resource_mentions", content="[Runtime Context]")
            ],
            "resource_mentions": [{"kind": "server", "id": "srv-1", "name": "barrahome"}],
        },
    )
    ctx = CommandContext(
        msg=msg,
        session=None,
        key="websocket:chat-1",
        raw="/trigger uptime watch",
        loop=loop,
    )

    response = await router.dispatch(ctx)

    assert response is not None
    trigger = store.list_for_session("websocket:chat-1")[0]
    assert trigger.references == [{"kind": "server", "id": "srv-1"}]
    assert RUNTIME_CONTEXT_INPUT_META not in trigger.origin_metadata
    assert "resource_mentions" not in trigger.origin_metadata
    # Reload from disk: the store had to be able to write it.
    assert LocalTriggerStore(tmp_path).list_for_session("websocket:chat-1")[0].references == [
        {"kind": "server", "id": "srv-1"}
    ]


@pytest.mark.asyncio
async def test_trigger_command_binds_inbound_session_when_unified_session_is_active(
    tmp_path: Path,
) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    store = LocalTriggerStore(tmp_path)
    loop = SimpleNamespace(workspace=tmp_path, local_trigger_store=store)
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content="/trigger PR review",
        session_key_override="websocket:chat-1:thread-a",
    )
    ctx = CommandContext(
        msg=msg,
        session=None,
        key=UNIFIED_SESSION_KEY,
        raw="/trigger PR review",
        loop=loop,
    )

    response = await router.dispatch(ctx)

    assert response is not None
    trigger = store.list_for_session("websocket:chat-1:thread-a")[0]
    assert trigger.session_key == "websocket:chat-1:thread-a"
    assert store.list_for_session(UNIFIED_SESSION_KEY) == []


@pytest.mark.asyncio
async def test_trigger_command_without_name_returns_usage_only(tmp_path: Path) -> None:
    router = CommandRouter()
    register_builtin_commands(router)
    store = LocalTriggerStore(tmp_path)
    loop = SimpleNamespace(workspace=tmp_path, local_trigger_store=store)
    msg = InboundMessage(
        channel="websocket",
        sender_id="user",
        chat_id="chat-1",
        content="/trigger@nanoinfra_bot",
        metadata={"webui": True},
    )
    ctx = CommandContext(
        msg=msg,
        session=None,
        key="websocket:chat-1",
        raw="/trigger@nanoinfra_bot",
        loop=loop,
    )

    response = await router.dispatch(ctx)

    assert response is not None
    assert "Usage: /trigger <name>" in response.content
    assert store.list_for_session("websocket:chat-1") == []


def test_trigger_command_is_in_help_text() -> None:
    assert "/trigger <name>" in build_help_text()
