"""Register the active connectors' tools, where the default tools are registered.

Separate from ``setup.py`` on purpose: resolving what is active reads config and nothing else,
so the executor calls it too, and the executor must not import the agent's tool tree. This
module is the agent side, and the only one that knows about ``ToolRegistry``.

The activation problems are logged here as well as in the executor. Both processes read the
same config, and the operator reads the gateway's log: a connector that never appears has to
say why in the place somebody is looking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.connectors.attachment import (
    ConnectorAttachment,
    set_connector_attachments,
)
from nanoinfra.connectors.setup import resolve_active, startup_summary
from nanoinfra.connectors.tools import build_tools

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext
    from nanoinfra.agent.tools.registry import ToolRegistry


#: What the last registration in this process registered.
#:
#: Process-local and deliberately not a lookup service: it records what this module did, so a
#: payload can compare it against what config now says without reaching into the agent loop
#: from an HTTP route. The gap between the two is the answer to "do I need to reload", and that
#: question exists because `docker compose up -d` after a config edit answers "Running" and
#: changes nothing.
_REGISTERED: set[str] = set()


def registered_tool_names() -> set[str]:
    """The connector tools this process registered, as of the last registration or reload."""
    return set(_REGISTERED)


def register_connector_tools(
    ctx: ToolContext,
    registry: ToolRegistry,
    cfg: ConnectorRuntimeConfig | None,
    *,
    replace: bool = False,
) -> list[str]:
    """Register one tool per enabled operation of every active connector.

    Returns the tool names, so the boot log lists them beside the built-in ones. A connector
    that did not activate contributes no tools and one warning naming the key that fixes it: a
    half-registered connector would give the model a tool that always fails.

    ``replace`` overwrites a tool of the same name rather than skipping it, which is what a
    reload wants: the operation may now carry different defaults or a different gate answer, and
    the stale instance holds the old ones.
    """
    if cfg is None or not cfg.active:
        # Recorded as empty rather than left alone: "nothing is active" is an answer, and a
        # stale record here is what made a payload say a reload was unnecessary when the
        # registry held nothing.
        _REGISTERED.clear()
        set_connector_attachments({})
        return []

    from nanoinfra.agent.tools.server_execution import default_socket_path
    from nanoinfra.gates.executor.client import ExecutorClient

    active, problems = resolve_active(cfg)
    for problem in problems:
        logger.warning("connector not activated -- {}", problem)

    # The same socket the command tool uses, resolved the same way: one deployment describes
    # the executor once.
    socket_path = getattr(ctx, "executor_socket", None) or default_socket_path()
    client = ExecutorClient(socket_path)

    # Recorded from the same resolution that registers the tools, so `available()` cannot consult
    # a mode for a connector that is not there (#204).
    set_connector_attachments({
        entry.name: ConnectorAttachment(
            name=entry.name,
            attach=getattr(cfg.connectors.get(entry.name), "attach", "always") or "always",
            kinds=frozenset(mention.kind for mention in entry.plugin.mentions),
        )
        for entry in active
    })

    names: list[str] = []
    for entry in active:
        for tool in build_tools(entry.plugin, entry.operations, client=client, ctx=ctx):
            if registry.has(tool.name) and not replace:
                logger.warning(
                    "connector '{}' would register '{}', which already exists; skipping",
                    entry.name,
                    tool.name,
                )
                continue
            registry.register(tool)
            names.append(tool.name)

    if active or problems:
        logger.info(startup_summary(active, problems))
    # Replaced, not accumulated. This records what the *last* registration registered, so two
    # registrations in one process cannot leave a union nobody holds.
    _REGISTERED.clear()
    _REGISTERED.update(names)
    return names


def reload_connector_tools(
    ctx: ToolContext, registry: ToolRegistry, cfg: ConnectorRuntimeConfig | None
) -> dict[str, Any]:
    """Reconcile the live registry against what config says now.

    Registration runs once at boot, so activating a connector afterwards left the two halves
    disagreeing: `connectors list` read config fresh and said `active`, while the running agent
    had no such tool and answered a calendar question by listing cron jobs. Restarting was the
    only fix, and `docker compose up -d` does not restart when only the config inside the volume
    changed -- it says `Running` and changes nothing.

    Removals come first. A connector that lost an operation, or a ceiling that dropped one, must
    take the tool out of the context window rather than leave one that now refuses.
    """
    from nanoinfra.connectors.tools import ConnectorOperationTool

    live: set[str] = {
        name
        for name in registry.tool_names
        if isinstance(registry.get(name), ConnectorOperationTool)
    }
    wanted = register_connector_tools(ctx, registry, cfg, replace=True)
    removed = sorted(live - set(wanted))
    for name in removed:
        registry.unregister(name)
        _REGISTERED.discard(name)

    return {
        "ok": True,
        "registered": wanted,
        "removed": removed,
        "message": (
            f"{len(wanted)} connector tool(s) registered"
            + (f", {len(removed)} removed" if removed else "")
        ),
        "requires_restart": False,
    }


__all__ = [
    "register_connector_tools",
    "registered_tool_names",
    "reload_connector_tools",
]
