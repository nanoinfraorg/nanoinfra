"""Register the active connectors' tools, where the default tools are registered.

Separate from ``setup.py`` on purpose: resolving what is active reads config and nothing else,
so the executor calls it too, and the executor must not import the agent's tool tree. This
module is the agent side, and the only one that knows about ``ToolRegistry``.

The activation problems are logged here as well as in the executor. Both processes read the
same config, and the operator reads the gateway's log: a connector that never appears has to
say why in the place somebody is looking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.connectors.setup import resolve_active, startup_summary
from nanoinfra.connectors.tools import build_tools

if TYPE_CHECKING:
    from nanoinfra.agent.tools.context import ToolContext
    from nanoinfra.agent.tools.registry import ToolRegistry


def register_connector_tools(
    ctx: ToolContext, registry: ToolRegistry, cfg: ConnectorRuntimeConfig | None
) -> list[str]:
    """Register one tool per enabled operation of every active connector.

    Returns the tool names, so the boot log lists them beside the built-in ones. A connector
    that did not activate contributes no tools and one warning naming the key that fixes it: a
    half-registered connector would give the model a tool that always fails.
    """
    if cfg is None or not cfg.active:
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

    names: list[str] = []
    for entry in active:
        for tool in build_tools(entry.plugin, entry.operations, client=client, ctx=ctx):
            if registry.has(tool.name):
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
    return names


__all__ = ["register_connector_tools"]
