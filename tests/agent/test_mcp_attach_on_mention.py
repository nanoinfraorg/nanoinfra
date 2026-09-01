"""An MCP server can be advertised in one line and attached on demand (#204).

Measured on a real deployment: a "hola" cost 31K input tokens, of which ~23K was tool schemas --
three GitHub servers narrowed to the *same* fifteen tools, so the same schemas three times. The
triplication is structural in MCP (one server per token, unique names), so `enabledTools` cannot
reach it and pausing costs the capability entirely.

This is the third state: the server stays connected, the prompt carries one line saying it exists
and how to reach it, and the schemas arrive only for a turn that names it. The line is the whole
argument -- a model that cannot see a capability cannot say "I can do that if you attach it", and a
silently worse answer is harder to notice than a large bill.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.tools import mcp as mcp_tools
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.config.schema import MCPServerConfig


class _Wrapper(mcp_tools._MCPWrapperBase):  # pyright: ignore[reportPrivateUsage]
    """One MCP tool bound to a server, which is all `available()` reads."""

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

    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not part of this test")


@pytest.fixture(autouse=True)
def _clean_modes() -> Any:
    """The mode map is module state, so a leaked entry would decide another test's answer."""
    mcp_tools.set_server_attach_modes({})
    yield
    mcp_tools.set_server_attach_modes({})


def _servers(**modes: str) -> dict[str, MCPServerConfig]:
    return {
        name: MCPServerConfig(command="npx", attach=mode)  # pyright: ignore[reportArgumentType]
        for name, mode in modes.items()
    }


def _registry(*tools: _Wrapper) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def _turn(*attached: object) -> Any:
    return request_context(
        RequestContext(
            channel="websocket",
            chat_id="c",
            session_key="s",
            metadata={mcp_tools.ATTACHED_PRESETS_META: list(attached)},
        )
    )


# --- what reaches the prompt ---------------------------------------------------------------


def test_an_always_server_is_in_every_prompt() -> None:
    """The default, and every deployment that predates the field."""
    mcp_tools.set_server_attach_modes(_servers(github="always"))

    assert _Wrapper("github", "mcp_github_list_issues").available() is True


def test_a_mention_server_is_in_no_prompt_that_did_not_ask() -> None:
    mcp_tools.set_server_attach_modes(_servers(github="mention"))

    assert _Wrapper("github", "mcp_github_list_issues").available() is False


def test_naming_the_server_brings_its_schemas_in() -> None:
    mcp_tools.set_server_attach_modes(_servers(github="mention"))
    tool = _Wrapper("github", "mcp_github_list_issues")

    with _turn({"name": "github"}):
        assert tool.available() is True


def test_naming_one_server_does_not_attach_another() -> None:
    """The saving is the *other* servers, so this is the assertion that measures it."""
    mcp_tools.set_server_attach_modes(_servers(github="mention", jira="mention"))

    with _turn({"name": "github"}):
        assert _Wrapper("github", "mcp_github_x").available() is True
        assert _Wrapper("jira", "mcp_jira_x").available() is False


def test_an_automation_declares_plain_names_rather_than_mention_objects() -> None:
    """A cron job has no composer, so it writes the same key with strings."""
    mcp_tools.set_server_attach_modes(_servers(github="mention"))

    with _turn("github"):
        assert _Wrapper("github", "mcp_github_x").available() is True


def test_an_unknown_server_keeps_the_behaviour_that_predates_the_field() -> None:
    """A registered, connected tool whose mode was never recorded is a bookkeeping bug, and the
    safe reading of a bug here is the old behaviour rather than a silently missing capability."""
    assert _Wrapper("ghost", "mcp_ghost_x").available() is True


def test_no_request_context_means_nothing_is_attached() -> None:
    """A background call with no bound turn must not inherit somebody else's attachment."""
    mcp_tools.set_server_attach_modes(_servers(github="mention"))

    assert _Wrapper("github", "mcp_github_x").available() is False


# --- the registry only offers what is available --------------------------------------------


def test_the_definitions_drop_a_mention_server_until_it_is_named() -> None:
    """The end-to-end claim: this is what actually leaves the process."""
    mcp_tools.set_server_attach_modes(_servers(github="mention"))
    registry = _registry(_Wrapper("github", "mcp_github_list_issues"))

    assert registry.get_definitions() == []

    with _turn({"name": "github"}):
        assert len(registry.get_definitions()) == 1


# --- the line that makes it honest ---------------------------------------------------------


def test_the_advertisement_names_the_server_the_count_and_how_to_attach() -> None:
    mcp_tools.set_server_attach_modes(_servers(github="mention"))
    registry = _registry(_Wrapper("github", "mcp_github_a"), _Wrapper("github", "mcp_github_b"))

    text = mcp_tools.advertisement(registry)

    assert "`github`" in text
    assert "2 tools" in text
    assert "@github" in text


def test_an_always_server_is_not_advertised_because_its_schemas_are_there() -> None:
    mcp_tools.set_server_attach_modes(_servers(github="always"))
    registry = _registry(_Wrapper("github", "mcp_github_a"))

    assert mcp_tools.advertisement(registry) == ""


def test_a_server_that_failed_to_connect_is_not_advertised() -> None:
    """Advertising a capability nobody can attach is worse than silence: the model would keep
    telling the operator to say `@github` and the attachment would do nothing."""
    mcp_tools.set_server_attach_modes(_servers(github="mention"))

    assert mcp_tools.advertisement(_registry()) == ""


def test_nothing_is_added_to_the_prompt_when_no_server_waits() -> None:
    """The stable prefix must not grow for the deployments that opted into none of this."""
    assert mcp_tools.advertisement(_registry()) == ""
