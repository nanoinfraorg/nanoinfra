"""Let the model load a deferred tool group by topic (proposals/tool-search.md).

`attach: "mention"` already lets a *user* widen a hidden group by typing `@group`. This is the
*model-driven* counterpart: a group set to `attach: "search"` sends no schemas and no per-group
advertised line -- only a single pointer telling the model this tool exists -- and the model calls
`tool_search` with a topic to load the matching group for the rest of the turn.

Why this and not just `mention`: the per-group advertised line is a per-item cost that grows with
how much you defer. One `tool_search` schema plus one pointer is flat, so a deployment can defer
far more than the two measured clusters without the advertisement becoming the new tax. The trade
is that the *model* decides when the tokens are worth spending instead of asking a person to.

The gate stays in `groups`: this tool only records what it found (`attach_group_for_turn`), and
`ToolRegistry._is_available` -> `groups.is_attached` does the rest -- so the newly-attached
schemas appear on the next provider call of the same turn (the runner asks for definitions fresh
each iteration). `search_groups` filters every candidate through the acting agent's ceiling, so a
tool outside that agent's contract is never even a search result, let alone attached.
"""

from __future__ import annotations

from typing import Any, cast

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools.base import Tool, ToolResult
from nanoinfra.agent.tools.context import current_request_context
from nanoinfra.agent.tools.schema import StringSchema, tool_parameters_schema

_PARAMETERS = tool_parameters_schema(
    query=StringSchema(
        "A topic, capability, service or action to find tools for -- for example 'draw the "
        "network', 'run a command over ssh', or 'generate an image'. Matched against the hidden "
        "groups' names, descriptions and tool names."
    ),
    required=["query"],
)


class ToolSearchTool(Tool):
    """Discover and load a deferred (`attach: "search"`) tool group by topic."""

    capability_class = "read"

    @property
    def name(self) -> str:
        return groups.TOOL_SEARCH_TOOL_NAME

    @property
    def description(self) -> str:
        return (
            "Load installed tools that are not in this prompt, by topic. Some tool groups are "
            "hidden to save context; if you need a capability you have no tool for, call this with "
            "the topic and the matching tools become available for the rest of this turn -- then "
            "call them directly. Do not ask the user to attach anything; search first."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return _PARAMETERS

    @property
    def read_only(self) -> bool:
        # It changes only what this turn can see, never anything in the world. Safe to parallelize
        # and correctly classed `read`.
        return True

    def available(self) -> bool:
        # Present only when some surface actually defers by `search`. A deployment that defers
        # nothing this way pays nothing for a tool it could never use.
        from nanoinfra.agent.tools import mcp as mcp_tools
        from nanoinfra.connectors import attachment as connector_attachment

        return bool(
            groups.search_mode_groups()
            or mcp_tools.search_mode_servers()
            or connector_attachment.search_mode_connectors()
        )

    async def execute(  # pyright: ignore[reportIncompatibleMethodOverride]
        self,
        query: str = "",
        **_extra: Any,
    ) -> ToolResult:
        from nanoinfra.agent.tools import mcp as mcp_tools
        from nanoinfra.connectors import attachment as connector_attachment

        ctx = current_request_context()
        turn_id = ctx.turn_id if ctx is not None else None
        q = str(query or "")
        lines: list[str] = []

        # Built-in tool groups: named tools reappear, so report them.
        for match in groups.search_groups(q, registry=None):
            name = str(match.get("name", ""))
            if name:
                groups.attach_group_for_turn(turn_id, name)
            raw_tools = match.get("tools")
            tools: list[object] = (
                list(cast("list[object]", raw_tools))
                if isinstance(raw_tools, (list, tuple))
                else []
            )
            tool_list = ", ".join(str(t) for t in tools)
            desc = f" — {match.get('description')}" if match.get("description") else ""
            lines.append(f"- group `{name}`{desc}: {tool_list}")

        # MCP servers and connectors expand into their own tool sets on the next call; naming the
        # surface is enough for the model to know it can now use it.
        for match in mcp_tools.search_servers(q):
            name = str(match.get("name", ""))
            if name:
                mcp_tools.attach_server_for_turn(turn_id, name)
                lines.append(f"- MCP server `{name}`: its tools are now loaded")
        for match in connector_attachment.search_connectors(q):
            name = str(match.get("name", ""))
            if name:
                connector_attachment.attach_connector_for_turn(turn_id, name)
                lines.append(f"- connector `{name}`: its operations are now loaded")

        if lines:
            return ToolResult(
                "Loaded these for this turn — call them directly now:\n\n" + "\n".join(lines)
            )

        catalogue = _searchable_catalogue(mcp_tools, connector_attachment)
        if not catalogue:
            return ToolResult(
                f"Nothing is searchable, so nothing matched '{query}'. There is no tool for that; "
                "tell the user if the request needs one."
            )
        return ToolResult(
            f"No tools matched '{query}'. What can be searched:\n\n{catalogue}\n\n"
            "If none of these fits the request, say there is no tool for it rather than "
            "substituting a different tool."
        )


def _searchable_catalogue(mcp_tools: Any, connector_attachment: Any) -> str:
    """Everything the model could have found, for a no-match reply -- across all three surfaces."""
    lines: list[str] = []
    for group in groups.searchable_groups(registry=None):
        desc = f" — {group.get('description')}" if group.get("description") else ""
        lines.append(f"- group `{group.get('name')}`{desc}")
    for match in mcp_tools.search_servers(""):
        lines.append(f"- MCP server `{match.get('name')}`")
    for match in connector_attachment.search_connectors(""):
        lines.append(f"- connector `{match.get('name')}`")
    return "\n".join(lines)
