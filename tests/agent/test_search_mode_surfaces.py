"""`attach: "search"` on MCP servers and connectors, and the one `tool_search` that spans all three
surfaces (proposals/tool-search.md).

`mention` lets a *user* widen a hidden surface with `@name`; `search` lets the *model* widen it by
calling `tool_search`. The gate, the per-turn store, and the ceiling neither may widen past are the
same shape as the built-in tool-group case in test_tool_search.py -- these pin the MCP and
connector copies of it, and the aggregation across the three.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools import mcp as mcp_tools
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.tool_search import ToolSearchTool
from nanoinfra.config.schema import MCPServerConfig, ToolsConfig
from nanoinfra.connectors import attachment as connector_attachment
from nanoinfra.session.automation_turns import AUTOMATION_AGENT_META


class _Wrapper(mcp_tools._MCPWrapperBase):  # pyright: ignore[reportPrivateUsage]
    def __init__(self, server_name: str, tool_name: str) -> None:
        self._server_name = server_name
        self._name = tool_name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "a tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("not part of this test")


@pytest.fixture(autouse=True)
def _clean() -> Any:
    mcp_tools.set_server_attach_modes({})
    connector_attachment.set_connector_attachments({})
    groups.set_tool_groups({})
    groups.set_registered_tools([])
    yield
    mcp_tools.set_server_attach_modes({})
    connector_attachment.set_connector_attachments({})
    groups.set_tool_groups({})
    groups.set_registered_tools([])
    for tid in ("t1", "t2"):
        mcp_tools.reset_search_attached_servers(tid)
        connector_attachment.reset_search_attached_connectors(tid)
        groups.reset_search_attached(tid)


def _ctx(*, turn_id: str = "t1", metadata: dict[str, Any] | None = None) -> RequestContext:
    return RequestContext(
        channel="websocket", chat_id="c", turn_id=turn_id, metadata=metadata or {}
    )


def _ceiling(key: str, *names: str) -> dict[str, Any]:
    return {AUTOMATION_AGENT_META: {key: list(names)}}


# --- MCP -------------------------------------------------------------------------------------


def test_mcp_search_server_is_hidden_until_the_model_attaches_it():
    mcp_tools.set_server_attach_modes(
        {"github": MCPServerConfig(command="x", attach="search")}  # pyright: ignore[reportArgumentType]
    )
    tool = _Wrapper("github", "mcp_github_create_issue")
    with request_context(_ctx()):
        assert tool.available() is False
        mcp_tools.attach_server_for_turn("t1", "github")
        assert tool.available() is True


def test_mcp_search_respects_the_agent_ceiling():
    mcp_tools.set_server_attach_modes(
        {"github": MCPServerConfig(command="x", attach="search")}  # pyright: ignore[reportArgumentType]
    )
    tool = _Wrapper("github", "mcp_github_create_issue")
    with request_context(_ctx(metadata=_ceiling("mcp_servers", "slack"))):
        assert mcp_tools.search_servers("github") == []
        mcp_tools.attach_server_for_turn("t1", "github")
        assert tool.available() is False  # ceiling is asked first and is not negotiable


def test_mcp_search_matches_by_server_name():
    mcp_tools.set_server_attach_modes(
        {"github": MCPServerConfig(command="x", attach="search")}  # pyright: ignore[reportArgumentType]
    )
    with request_context(_ctx()):
        assert [m["name"] for m in mcp_tools.search_servers("github issues")] == ["github"]
        assert mcp_tools.search_servers("weather") == []


# --- connectors ------------------------------------------------------------------------------


def _connector(name: str, attach: str, kinds: frozenset[str] = frozenset()) -> None:
    connector_attachment.set_connector_attachments(
        {name: connector_attachment.ConnectorAttachment(name=name, attach=attach, kinds=kinds)}
    )


def test_connector_search_is_hidden_until_the_model_attaches_it():
    _connector("google-calendar", "search", frozenset({"calendar"}))
    with request_context(_ctx()):
        assert connector_attachment.is_attached("google-calendar") is False
        connector_attachment.attach_connector_for_turn("t1", "google-calendar")
        assert connector_attachment.is_attached("google-calendar") is True


def test_connector_search_respects_the_agent_ceiling():
    _connector("google-calendar", "search", frozenset({"calendar"}))
    with request_context(_ctx(metadata=_ceiling("connectors", "github"))):
        assert connector_attachment.search_connectors("calendar") == []
        connector_attachment.attach_connector_for_turn("t1", "google-calendar")
        assert connector_attachment.is_attached("google-calendar") is False


def test_connector_search_matches_by_name_or_kind():
    _connector("google-calendar", "search", frozenset({"calendar"}))
    with request_context(_ctx()):
        assert [m["name"] for m in connector_attachment.search_connectors("calendar")] == [
            "google-calendar"
        ]
        assert connector_attachment.search_connectors("database") == []


# --- the one tool_search spans all three -----------------------------------------------------


def test_tool_search_attaches_across_all_three_surfaces():
    groups.set_tool_groups(
        ToolsConfig.model_validate({"groups": {"diagrams": {"attach": "search"}}}).groups
    )
    groups.set_registered_tools(["create_diagram"])
    mcp_tools.set_server_attach_modes(
        {"diagrams_mcp": MCPServerConfig(command="x", attach="search")}  # pyright: ignore[reportArgumentType]
    )
    _connector("diagrams_conn", "search", frozenset({"diagram"}))
    mcp_wrapper = _Wrapper("diagrams_mcp", "mcp_diagrams_mcp_render")
    with request_context(_ctx()):
        # an empty query lists everything searchable, so all three attach
        import asyncio

        asyncio.run(ToolSearchTool().execute(query=""))
        assert groups.is_attached("create_diagram") is True
        assert mcp_wrapper.available() is True
        assert connector_attachment.is_attached("diagrams_conn") is True


def test_tool_search_available_only_when_a_surface_defers_by_search():
    assert ToolSearchTool().available() is False
    _connector("google-calendar", "search")
    assert ToolSearchTool().available() is True
