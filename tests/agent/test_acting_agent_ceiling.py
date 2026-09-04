"""The agent answering a turn is the ceiling -- on a person's turn too (#266).

This is the half that was missing. The ceiling is read from the turn's own metadata, because the
tool filter runs inside a tool call and has no route to config, and only the cron and trigger
runners ever wrote it. So an agent's `toolGroups`, `skills`, `mcpServers` and `connectors` bound a
scheduled run and bound nothing at all when a person chose that agent in the composer: the prompt
narrowed, and every tool schema, every skill in the catalogue and every MCP server stayed exactly
where they were.

The deployment that found it said what it cost: *if every MCP server and every skill I have
installed loads, one conversation spends the context.*

The other rule these tests hold is the three states. `None` is nothing declared -- every agent
nobody narrowed -- and an empty list is a declared ceiling that admits nothing. They were one state
for a while, and while they were, a deployment could not write down "this agent loads no MCP
server", which is the one sentence the context problem is solved with.
"""

from __future__ import annotations

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.mcp import within_agent_mcp_ceiling
from nanoinfra.config.schema import AgentDefaults, NamedAgentConfig
from nanoinfra.connectors.attachment import within_agent_connector_ceiling
from nanoinfra.session.automation_turns import (
    AUTOMATION_AGENT_META,
    acting_agent_binding_metadata,
    acting_agent_connectors,
    acting_agent_mcp_servers,
    automation_agent_tool_groups,
)


def _ctx(metadata: dict[str, object]) -> RequestContext:
    return RequestContext(channel="webui", chat_id="c1", metadata=metadata)


# --- what the turn carries --------------------------------------------------------------------


def test_a_list_nobody_declared_is_absent_from_the_metadata() -> None:
    """Not written as an empty list, because every reader checks whether the key is there."""
    binding = acting_agent_binding_metadata(tool_groups=None, mcp_servers=None, connectors=None)

    assert binding == {AUTOMATION_AGENT_META: {}}


def test_a_declared_empty_list_is_written_as_one() -> None:
    """The state that did not exist. An agent that reaches no MCP server has to be expressible."""
    binding = acting_agent_binding_metadata(mcp_servers=[])

    assert binding[AUTOMATION_AGENT_META] == {"mcp_servers": []}
    assert acting_agent_mcp_servers(binding) == ()
    assert automation_agent_tool_groups(binding) is None


def test_each_ceiling_is_read_back_as_it_was_written() -> None:
    binding = acting_agent_binding_metadata(
        tool_groups=["servers"], mcp_servers=["playwright"], connectors=["github"]
    )

    assert automation_agent_tool_groups(binding) == ("servers",)
    assert acting_agent_mcp_servers(binding) == ("playwright",)
    assert acting_agent_connectors(binding) == ("github",)


# --- what it caps -----------------------------------------------------------------------------


def test_an_mcp_server_outside_the_agents_list_loads_no_schema() -> None:
    """Whatever the server's attach mode. A `mention` server widens on request; this caps."""
    binding = acting_agent_binding_metadata(mcp_servers=["playwright"])

    with request_context(_ctx(dict(binding))):
        assert within_agent_mcp_ceiling("playwright") is True
        assert within_agent_mcp_ceiling("linear") is False


def test_a_declared_empty_mcp_list_admits_no_server_at_all() -> None:
    with request_context(_ctx(dict(acting_agent_binding_metadata(mcp_servers=[])))):
        assert within_agent_mcp_ceiling("playwright") is False


def test_nothing_declared_admits_every_server() -> None:
    """Which is every deployment that has narrowed nobody, and has to stay unchanged."""
    with request_context(_ctx({})):
        assert within_agent_mcp_ceiling("playwright") is True
        assert within_agent_connector_ceiling("github") is True


def test_a_connector_outside_the_agents_list_is_not_attached() -> None:
    binding = acting_agent_binding_metadata(connectors=["github"])

    with request_context(_ctx(dict(binding))):
        assert within_agent_connector_ceiling("github") is True
        assert within_agent_connector_ceiling("google-calendar") is False


def test_a_tool_outside_the_agents_groups_is_not_offered() -> None:
    """Through the same `is_attached` the registry asks, so the ceiling and the schema agree."""
    groups.set_tool_groups({})
    try:
        binding = acting_agent_binding_metadata(tool_groups=["diagrams"])
        with request_context(_ctx(dict(binding))):
            assert groups.is_attached("create_diagram") is True
            assert groups.is_attached("execute_on_server") is False
            # A tool in no group is kept: groups cover surfaces, not the whole tool set.
            assert groups.is_attached("read_file") is True
    finally:
        groups.set_tool_groups({})


def test_a_declared_empty_group_list_keeps_only_the_ungrouped_tools() -> None:
    """How a coordinator is expressed: it may reach no grouped surface, so it must ask a peer."""
    groups.set_tool_groups({})
    try:
        with request_context(_ctx(dict(acting_agent_binding_metadata(tool_groups=[])))):
            assert groups.is_attached("read_file") is True
            assert groups.is_attached("execute_on_server") is False
            assert groups.is_attached("create_diagram") is False
    finally:
        groups.set_tool_groups({})


# --- the schema holds the three states --------------------------------------------------------


def test_an_agent_that_declared_nothing_holds_none_rather_than_an_empty_list() -> None:
    agent = NamedAgentConfig()

    assert agent.tool_groups is None
    assert agent.skills is None
    assert agent.connectors is None
    assert agent.mcp_servers is None


def test_the_deployments_own_agent_holds_the_same_fields() -> None:
    """It is one more agent; the only thing that makes it different is that it cannot be deleted."""
    defaults = AgentDefaults.model_validate({
        "skills": [],
        "mcpServers": ["playwright"],
        "connectors": [],
        "delegates": [],
    })

    assert defaults.skills == []
    assert defaults.mcp_servers == ["playwright"]
    assert defaults.connectors == []
    assert defaults.tool_groups is None
