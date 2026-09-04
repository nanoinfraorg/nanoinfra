"""A `search`-mode tool group waits for the *model* to ask for it (proposals/tool-search.md).

`mention` (tested in test_tool_groups.py) lets a user widen a hidden group with `@group`. `search`
is the model-driven counterpart: the schemas stay hidden and a single pointer replaces the
per-group advertised line, and the model loads a group by calling `tool_search`. These tests pin
the gate, the per-turn store, the ceiling that neither mode may widen past, and the pointer's
suppression.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.tools import groups
from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.context import (
    RequestContext,
    bind_request_context,
    reset_request_context,
)
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.agent.tools.tool_search import ToolSearchTool
from nanoinfra.config.schema import ToolsConfig
from nanoinfra.session.automation_turns import AUTOMATION_AGENT_META


@pytest.fixture(autouse=True)
def _clean_groups():
    groups.set_tool_groups({})
    groups.set_registered_tools([])
    yield
    groups.set_tool_groups({})
    groups.set_registered_tools([])
    groups.reset_search_attached("t1")
    groups.reset_search_attached("t2")


def _declare(payload: dict[str, Any]) -> None:
    groups.set_tool_groups(ToolsConfig.model_validate({"groups": payload}).groups)


class _Fake(Tool):
    capability_class = "read"

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return f"the {self._name} tool"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **_kwargs: Any) -> Any:
        return "ok"


def _registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(_Fake(name))
    groups.set_registered_tools(names)
    return registry


class _Turn:
    def __init__(
        self, *, turn_id: str = "t1", metadata: dict[str, Any] | None = None
    ) -> None:
        self._ctx = RequestContext(
            channel="websocket",
            chat_id="c",
            turn_id=turn_id,
            metadata=metadata or {},
        )

    def __enter__(self) -> None:
        self._token = bind_request_context(self._ctx)

    def __exit__(self, *_exc: object) -> None:
        reset_request_context(self._token)


def _ceiling(*group_names: str) -> dict[str, Any]:
    return {AUTOMATION_AGENT_META: {"tool_groups": list(group_names)}}


# --- the gate --------------------------------------------------------------------------------


def test_search_group_is_hidden_until_the_model_attaches_it():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram", "update_diagram")
    with _Turn():
        assert groups.is_attached("create_diagram") is False
        groups.attach_group_for_turn("t1", "diagrams")
        assert groups.is_attached("create_diagram") is True


def test_tool_search_execute_loads_the_matching_group_for_the_turn():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram", "update_diagram")
    with _Turn():
        assert groups.is_attached("create_diagram") is False
        result = _run(ToolSearchTool(), query="draw a diagram")
        assert "create_diagram" in str(result)
        # The very next availability check on the same turn sees the schemas.
        assert groups.is_attached("create_diagram") is True


def test_a_mention_group_is_untouched_by_search():
    _declare({"diagrams": {"attach": "mention"}})
    _registry("create_diagram")
    with _Turn():
        # A mention group is not a search result, and searching does not attach it.
        assert groups.search_groups("diagram") == []
        _run(ToolSearchTool(), query="diagram")
        assert groups.is_attached("create_diagram") is False


# --- the ceiling neither mode may widen past -------------------------------------------------


def test_search_never_surfaces_a_group_outside_the_agent_ceiling():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram")
    # The acting agent is capped to `servers`; diagrams is off its contract entirely.
    with _Turn(metadata=_ceiling("servers")):
        assert groups.search_groups("diagram") == []
        # Even a direct attach cannot widen past the ceiling: is_attached asks it first.
        groups.attach_group_for_turn("t1", "diagrams")
        assert groups.is_attached("create_diagram") is False


def test_search_surfaces_a_group_within_the_ceiling():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram")
    with _Turn(metadata=_ceiling("diagrams")):
        assert [m["name"] for m in groups.search_groups("diagram")] == ["diagrams"]


# --- the per-turn store ----------------------------------------------------------------------


def test_an_attach_does_not_leak_into_the_next_turn():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram")
    with _Turn(turn_id="t1"):
        groups.attach_group_for_turn("t1", "diagrams")
        assert groups.is_attached("create_diagram") is True
    groups.reset_search_attached("t1")
    with _Turn(turn_id="t2"):
        assert groups.is_attached("create_diagram") is False


def test_a_concurrent_turn_does_not_see_another_turns_attach():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram")
    groups.attach_group_for_turn("t1", "diagrams")
    with _Turn(turn_id="t2"):
        assert groups.is_attached("create_diagram") is False


# --- the pointer and the tool's presence -----------------------------------------------------


def test_pointer_and_tool_absent_when_nothing_is_search_mode():
    _declare({"diagrams": {"attach": "mention"}})
    registry = _registry("create_diagram")
    assert groups.search_advertisement(registry) == ""
    assert ToolSearchTool().available() is False


def test_pointer_and_tool_present_when_a_group_is_search_mode():
    _declare({"diagrams": {"attach": "search"}})
    registry = _registry("create_diagram")
    assert "tool_search" in groups.search_advertisement(registry)
    assert ToolSearchTool().available() is True


def test_pointer_suppressed_when_the_search_group_has_no_present_tool():
    _declare({"diagrams": {"attach": "search"}})
    registry = _registry("something_else")  # none of the group's tools are installed
    assert groups.search_advertisement(registry) == ""


# --- the search tool is never itself deferrable ----------------------------------------------


def test_tool_search_is_never_gated_even_if_a_config_groups_it():
    _declare({"weird": {"attach": "search", "tools": ["tool_search", "x"]}})
    _registry("tool_search", "x")
    with _Turn():
        assert groups.is_attached("tool_search") is True  # the guard overrides the grouping
        assert groups.is_attached("x") is False


# --- no match tells the model what it could have found ---------------------------------------


def test_no_match_returns_the_considered_catalogue():
    _declare({"diagrams": {"attach": "search"}})
    _registry("create_diagram")
    with _Turn():
        result = _run(ToolSearchTool(), query="something with no match at all")
        assert "diagrams" in str(result)
        assert groups.is_attached("create_diagram") is False


def _run(tool: Tool, **kwargs: Any) -> Any:
    import asyncio

    return asyncio.run(tool.execute(**kwargs))
