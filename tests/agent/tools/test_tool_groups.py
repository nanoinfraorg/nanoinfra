"""A group of built-in tools can wait to be asked for (#210).

Measured on the demo: 31 built-in schemas cost 10,273 tokens of a 17,302-token greeting, and two
clusters were 3,857 of them -- diagrams 2,438, SSH servers 1,419 -- for capabilities the turn never
touched. `attach: "mention"` already existed for MCP servers and connectors, which have a name to
hang a mode on. A built-in tool had none.

The default matters as much as the mechanism, so it is pinned first: a group ships *defined* and
`always`, because a tool that vanished from a prompt because a release regrouped it would be a
behaviour change nobody asked for.
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
from nanoinfra.config.schema import ToolsConfig


@pytest.fixture(autouse=True)
def _clean_groups():
    """Module state, so a test that declares groups must not leak them into the next one."""
    groups.set_tool_groups({})
    yield
    groups.set_tool_groups({})


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
    return registry


class _Turn:
    """Bind a request the way a turn does, so the gate has something to read."""

    def __init__(self, *, text: str = "", metadata: dict[str, Any] | None = None) -> None:
        self._ctx = RequestContext(
            channel="websocket",
            chat_id="c",
            original_user_text=text,
            metadata=metadata or {},
        )

    def __enter__(self) -> None:
        self._token = bind_request_context(self._ctx)

    def __exit__(self, *_exc: object) -> None:
        reset_request_context(self._token)


# --- the default ----------------------------------------------------------------------------


def test_no_declared_groups_gates_nothing() -> None:
    assert groups.is_attached("create_diagram") is True
    assert groups.mention_only_groups() == []


def test_a_declared_group_defaults_to_always() -> None:
    """Upgrading to a release that defines `diagrams` must not remove the diagram tools."""
    _declare({"diagrams": {}})

    assert groups.is_attached("create_diagram") is True
    assert groups.mention_only_groups() == []


def test_a_builtin_group_does_not_need_its_tools_listed() -> None:
    """The whole point of shipping the definitions: saving 2,438 tokens is one line, not five
    tool names an operator has to keep in step with the code."""
    _declare({"diagrams": {"attach": "mention"}})

    for name in ("create_diagram", "update_diagram", "get_diagram", "list_diagrams"):
        assert groups.is_attached(name) is False, name


def test_a_group_may_name_its_own_tools() -> None:
    _declare({"mine": {"attach": "mention", "tools": ["exec"]}})

    assert groups.is_attached("exec") is False
    assert groups.is_attached("read_file") is True


def test_a_group_that_names_nothing_and_is_unknown_is_dropped() -> None:
    """It could gate nothing, and advertising it would offer the model a word that does nothing."""
    _declare({"typo": {"attach": "mention"}})

    assert groups.mention_only_groups() == []


# --- the gate -------------------------------------------------------------------------------


def test_naming_the_group_in_the_message_attaches_it() -> None:
    """Text, not a composer payload: the advertised line says "say `@diagrams`", and Telegram,
    Discord and the CLI have no composer to say it through."""
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(text="update the @diagrams for prod please"):
        assert groups.is_attached("create_diagram") is True


def test_an_undeclared_name_in_the_text_attaches_nothing() -> None:
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(text="ping @everyone and @servers"):
        assert groups.is_attached("create_diagram") is False


def test_an_automation_declares_the_group_in_metadata() -> None:
    """An unattended turn has nobody to type `@`, which is the case that makes a mention-mode
    group unusable without this."""
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(metadata={groups.ATTACHED_GROUPS_META: ["diagrams"]}):
        assert groups.is_attached("create_diagram") is True


def test_the_composer_object_form_is_read_too() -> None:
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(metadata={groups.ATTACHED_GROUPS_META: [{"name": "diagrams"}]}):
        assert groups.is_attached("create_diagram") is True


def test_a_tool_in_an_always_group_and_a_mention_group_stays_available() -> None:
    """Both are explicit operator statements, and the failure to avoid is a capability silently
    withdrawn -- which is harder to notice than a large bill."""
    _declare({
        "diagrams": {"attach": "mention"},
        "core": {"attach": "always", "tools": ["create_diagram"]},
    })

    assert groups.is_attached("create_diagram") is True
    assert groups.is_attached("update_diagram") is False


def test_no_bound_request_attaches_nothing() -> None:
    """A gate that read "no context" as "attached" would put the schemas back in every prompt
    built outside a turn."""
    _declare({"diagrams": {"attach": "mention"}})

    assert groups.is_attached("create_diagram") is False


def test_the_gating_group_can_be_named_for_a_report() -> None:
    _declare({"diagrams": {"attach": "mention"}})

    assert groups.group_of("update_diagram") == "diagrams"
    assert groups.group_of("exec") is None


# --- the registry ---------------------------------------------------------------------------


def test_the_schemas_leave_the_prompt() -> None:
    registry = _registry("exec", "create_diagram", "update_diagram")
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(text="hola"):
        names = [schema["function"]["name"] for schema in registry.get_definitions()]

    assert names == ["exec"]


def test_the_schemas_come_back_for_a_turn_that_names_the_group() -> None:
    registry = _registry("exec", "create_diagram", "update_diagram")
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(text="@diagrams show me prod"):
        names = sorted(schema["function"]["name"] for schema in registry.get_definitions())

    assert names == ["create_diagram", "exec", "update_diagram"]


def test_the_breakdown_counts_what_was_actually_sent() -> None:
    """`schema_breakdown` feeds the debug panel through `get_definitions`, so a panel that counted
    the withheld schemas would disagree with the bill."""
    registry = _registry("exec", "create_diagram")
    _declare({"diagrams": {"attach": "mention"}})

    with _Turn(text="hola"):
        breakdown = registry.schema_breakdown()

    tools = [row["name"] for entry in breakdown for row in entry["tools"]]
    assert tools == ["exec"]


# --- the advertisement ----------------------------------------------------------------------


def test_an_always_group_is_not_advertised() -> None:
    """Its schemas are already in the prompt; a line offering to load them would be noise."""
    registry = _registry("create_diagram")
    _declare({"diagrams": {}})

    assert groups.advertisement(registry) == ""


def test_a_mention_group_gets_one_line_naming_how_to_attach_it() -> None:
    registry = _registry("create_diagram", "update_diagram")
    _declare({"diagrams": {"attach": "mention"}})

    text = groups.advertisement(registry)

    assert "`diagrams`" in text
    assert "2 tools" in text
    assert "@diagrams" in text
    assert "infrastructure diagrams" in text


def test_a_group_whose_tools_are_all_absent_is_not_advertised() -> None:
    """Telling a user to say `@diagrams` when the attachment would load nothing is worse than
    silence."""
    registry = _registry("exec")
    _declare({"diagrams": {"attach": "mention"}})

    assert groups.advertisement(registry) == ""


def test_the_advertisement_counts_only_the_tools_this_deployment_registered() -> None:
    registry = _registry("create_diagram")
    _declare({"diagrams": {"attach": "mention"}})

    assert "1 tools" in groups.advertisement(registry)


def test_the_advertisement_is_cheap_next_to_the_schemas_it_replaces() -> None:
    """~50 tokens against 2,438. If it were not, the trade would not be worth making."""
    from nanoinfra.utils.helpers import count_text_tokens

    registry = _registry(*groups.BUILTIN_GROUPS["diagrams"][1])
    _declare({"diagrams": {"attach": "mention"}})

    assert count_text_tokens(groups.advertisement(registry)) < 200


# --- the mention normaliser -----------------------------------------------------------------


def test_a_client_cannot_name_a_group_into_existence() -> None:
    """Whether a group exists is config's answer; a mention only records that somebody asked."""
    _declare({"diagrams": {"attach": "mention"}})

    assert groups.normalize_group_mentions(["diagrams", "made-up"]) == [{"name": "diagrams"}]


def test_mentions_are_bounded_and_deduplicated() -> None:
    _declare({"diagrams": {"attach": "mention"}})

    assert groups.normalize_group_mentions(["diagrams", "DIAGRAMS"]) == [{"name": "diagrams"}]
    assert groups.normalize_group_mentions("diagrams") == []
