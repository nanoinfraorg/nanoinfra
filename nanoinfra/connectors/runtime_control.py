"""Re-register the connectors' tools on request, without a restart (#194).

The same shape ``agent/tools/mcp.py`` uses for its own reload, and for the same reason: the
registry was built when the agent started, and config has changed since. Two halves that
disagree is the failure -- `connectors list` reads config fresh and says `active` while the
running agent has no such tool, and answers a calendar question by listing cron jobs.

The request travels as one inbound runtime-control message rather than as a method call, because
the caller is an HTTP route in another task and the registry belongs to the loop.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from loguru import logger

from nanoinfra.bus.events import (
    INBOUND_META_RUNTIME_CONTROL,
    RUNTIME_CONTROL_ACK,
    RUNTIME_CONTROL_CONNECTOR_RELOAD,
    InboundMessage,
)
from nanoinfra.bus.queue import MessageBus

#: A reload builds tools and touches no network, so it answers quickly or something is wrong.
RELOAD_TIMEOUT_S = 15.0


async def request_connector_reload(
    bus: MessageBus, *, timeout: float = RELOAD_TIMEOUT_S
) -> dict[str, Any]:
    """Ask the running loop to reconcile its connector tools. Returns what it did."""
    loop = asyncio.get_running_loop()
    ack: asyncio.Future[dict[str, Any]] = loop.create_future()
    await bus.publish_inbound(
        InboundMessage(
            channel="system",
            sender_id="webui-settings",
            chat_id="runtime",
            content=RUNTIME_CONTROL_CONNECTOR_RELOAD,
            metadata={
                INBOUND_META_RUNTIME_CONTROL: RUNTIME_CONTROL_CONNECTOR_RELOAD,
                RUNTIME_CONTROL_ACK: ack,
            },
        )
    )
    try:
        result = await asyncio.wait_for(ack, timeout=timeout)
    except asyncio.TimeoutError:
        return {
            "ok": False,
            "message": "the connector reload timed out. Restart nanoinfra to pick up changes.",
            "requires_restart": True,
        }
    if isinstance(cast(object, result), dict):
        return result
    return {
        "ok": False,
        "message": "the connector reload returned an unexpected response.",
        "requires_restart": True,
    }


async def handle_runtime_control(state: Any, msg: InboundMessage, registry: Any) -> bool:
    """Answer a connector-reload message, or decline it so the next handler sees it."""
    metadata = msg.metadata if isinstance(cast(object, msg.metadata), dict) else {}
    if metadata.get(INBOUND_META_RUNTIME_CONTROL) != RUNTIME_CONTROL_CONNECTOR_RELOAD:
        return False

    ack = metadata.get(RUNTIME_CONTROL_ACK)
    try:
        result = await asyncio.to_thread(_reload, state, registry)
    except Exception as exc:  # noqa: BLE001 -- one failed reload must not end the loop
        logger.exception("connector reload failed")
        result = {
            "ok": False,
            "message": f"the connector reload failed: {exc}",
            "requires_restart": True,
        }
    if isinstance(ack, asyncio.Future) and not ack.done():
        cast("asyncio.Future[dict[str, Any]]", ack).set_result(result)
    return True


def _reload(state: Any, registry: Any) -> dict[str, Any]:
    """Read config again and reconcile. Runs off the event loop: it imports and builds tools."""
    from nanoinfra.config.loader import load_config
    from nanoinfra.connectors.registration import reload_connector_tools

    config = load_config()
    # The live config, not the snapshot the loop was constructed with: the whole point is that
    # config changed since then.
    state._connectors_config = config.connectors
    ctx = state.build_tool_context()
    result = reload_connector_tools(ctx, registry, config.connectors)
    logger.info("connectors: {}", result["message"])
    return result


__all__ = ["RELOAD_TIMEOUT_S", "handle_runtime_control", "request_connector_reload"]
