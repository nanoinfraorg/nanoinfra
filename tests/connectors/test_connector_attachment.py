"""A connector's operations can be advertised and attached on demand (#204).

The MCP side of this saved ~23K input tokens a turn on a real deployment. A connector is the same
shape of cost from a different source: every active connector's operations sit in every prompt,
whether or not the turn is about a calendar.

The rule worth testing carefully is the second way of naming one. `@google-calendar` is explicit,
but `@calendar:<id>` -- pinning a specific calendar -- also names the calendar connector, because
requiring both would be a rule nobody could guess from the UI.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.tools.base import Tool
from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.connectors.attachment import (
    ATTACHED_CONNECTORS_META,
    RESOURCE_MENTIONS_META,
    ConnectorAttachment,
    advertisement,
    is_attached,
    mention_only_connectors,
    normalize_connector_mentions,
    set_connector_attachments,
)


class _Operation(Tool):
    """One connector operation, which is all the advertisement counts."""

    def __init__(self, connector: str, name: str) -> None:
        self._connector = connector
        self._name = name

    @property
    def source(self) -> str:
        return f"connector:{self._connector}"

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "an operation"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> Any:  # pragma: no cover - never called here
        raise AssertionError("not part of this test")


def _attachment(name: str, attach: str, *kinds: str) -> ConnectorAttachment:
    return ConnectorAttachment(name=name, attach=attach, kinds=frozenset(kinds))


@pytest.fixture(autouse=True)
def _clean() -> Any:
    """Module state, so a leaked entry would decide another test's answer."""
    set_connector_attachments({})
    yield
    set_connector_attachments({})


def _turn(*, connectors: list[Any] | None = None, mentions: list[Any] | None = None) -> Any:
    metadata: dict[str, Any] = {}
    if connectors is not None:
        metadata[ATTACHED_CONNECTORS_META] = connectors
    if mentions is not None:
        metadata[RESOURCE_MENTIONS_META] = mentions
    return request_context(
        RequestContext(channel="websocket", chat_id="c", session_key="s", metadata=metadata)
    )


# --- what reaches the prompt ---------------------------------------------------------------


def test_an_always_connector_is_in_every_prompt() -> None:
    """The default, and every deployment that predates the field."""
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "always")})

    assert is_attached("google-calendar") is True


def test_a_mention_connector_is_in_no_prompt_that_did_not_ask() -> None:
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    assert is_attached("google-calendar") is False


def test_naming_the_connector_loads_it() -> None:
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    with _turn(connectors=[{"name": "google-calendar"}]):
        assert is_attached("google-calendar") is True


def test_pinning_one_of_its_objects_also_names_it() -> None:
    """`@calendar:<id>` is naming the calendar connector. Requiring `@google-calendar` as well
    would be a rule nobody could guess from a menu that offers the object."""
    set_connector_attachments({
        "google-calendar": _attachment("google-calendar", "mention", "calendar")
    })

    with _turn(mentions=[{"kind": "calendar", "id": "albertof@example.invalid"}]):
        assert is_attached("google-calendar") is True


def test_pinning_an_unrelated_kind_does_not_name_it() -> None:
    set_connector_attachments({
        "google-calendar": _attachment("google-calendar", "mention", "calendar")
    })

    with _turn(mentions=[{"kind": "server", "id": "db-01"}]):
        assert is_attached("google-calendar") is False


def test_naming_one_connector_does_not_load_another() -> None:
    """The saving is the connectors the turn is not about, so this is what measures it."""
    set_connector_attachments({
        "google-calendar": _attachment("google-calendar", "mention", "calendar"),
        "gmail": _attachment("gmail", "mention", "mailbox"),
    })

    with _turn(connectors=["google-calendar"]):
        assert is_attached("google-calendar") is True
        assert is_attached("gmail") is False


def test_an_unknown_connector_keeps_the_behaviour_that_predates_the_field() -> None:
    """A registered tool whose mode was never recorded is a bookkeeping bug, and the safe reading
    of a bug here is the old behaviour rather than a silently missing capability."""
    assert is_attached("never-registered") is True


def test_no_request_context_means_nothing_is_attached() -> None:
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    assert is_attached("google-calendar") is False


def test_the_real_operation_tool_asks_the_same_question() -> None:
    """The helper above is only worth anything if the class that ships the schemas consults it.

    Asserted through the class rather than through a built instance, because building one needs an
    executor client and a manifest -- and the thing under test is which function `available()` calls.
    """
    from nanoinfra.connectors.tools import ConnectorOperationTool

    class _Fake(ConnectorOperationTool):
        def __init__(self) -> None:
            self._plugin = type("_P", (), {"name": "google-calendar"})()

    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    assert _Fake().available() is False

    with _turn(connectors=["google-calendar"]):
        assert _Fake().available() is True


# --- the line that makes it honest ---------------------------------------------------------


def _registry(*tools: _Operation) -> ToolRegistry:
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)
    return registry


def test_the_advertisement_names_the_connector_the_count_and_both_ways_to_attach() -> None:
    set_connector_attachments({
        "google-calendar": _attachment("google-calendar", "mention", "calendar")
    })
    registry = _registry(
        _Operation("google-calendar", "google_calendar_list_events"),
        _Operation("google-calendar", "google_calendar_create_event"),
    )

    text = advertisement(registry)

    assert "`google-calendar`" in text
    assert "2 operations" in text
    assert "@google-calendar" in text
    assert "@calendar:<id>" in text


def test_an_always_connector_is_not_advertised_because_its_schemas_are_there() -> None:
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "always")})

    assert advertisement(_registry(_Operation("google-calendar", "x"))) == ""


def test_a_connector_that_registered_nothing_is_not_advertised() -> None:
    """Telling an operator to say `@google-calendar` when the attachment would do nothing is
    worse than silence."""
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    assert advertisement(_registry()) == ""


def test_nothing_is_added_to_the_prompt_when_no_connector_waits() -> None:
    assert advertisement(_registry()) == ""


def test_the_mention_only_list_is_stable_and_excludes_the_rest() -> None:
    set_connector_attachments({
        "gmail": _attachment("gmail", "mention"),
        "google-calendar": _attachment("google-calendar", "mention"),
        "jira": _attachment("jira", "always"),
    })

    assert mention_only_connectors() == ["gmail", "google-calendar"]


# --- what a client may claim ---------------------------------------------------------------


def test_a_client_cannot_name_a_connector_into_existence() -> None:
    """Whether a connector may be used is config's answer. This only records that somebody asked,
    so a name nobody activated is dropped rather than carried into the turn."""
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    assert normalize_connector_mentions(["google-calendar", "not-installed"]) == [
        {"name": "google-calendar"}
    ]


def test_names_are_deduplicated_and_lower_cased() -> None:
    set_connector_attachments({"google-calendar": _attachment("google-calendar", "mention")})

    assert normalize_connector_mentions(
        ["Google-Calendar", {"name": "google-calendar"}]
    ) == [{"name": "google-calendar"}]


def test_a_payload_that_is_not_a_list_is_no_attachment() -> None:
    assert normalize_connector_mentions({"name": "google-calendar"}) == []
