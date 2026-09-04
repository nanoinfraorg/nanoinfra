"""When a connector's tool schemas reach the prompt (#204).

The same trade MCP servers got, for the same measured reason: every active connector's operations
are in every prompt on every turn, whether or not the turn is about a calendar. A connector in
`attach: "mention"` mode contributes one advertised line instead, and its schemas only for a turn
that names it.

Two things count as naming it, and the second is the one worth stating: an explicit `@<name>`, or a
`@<kind>:<id>` object mention of a kind the connector declares. Pinning a specific calendar *is*
naming the calendar connector, and requiring both would be a rule nobody could guess.

Module state rather than a field on each tool, for the reason the MCP side has the same shape: a
connector's mode changes in config while its tool instances are live, and four of them disagreeing
about it is the failure this file exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, cast

if TYPE_CHECKING:
    from nanoinfra.agent.tools.registry import ToolRegistry

#: The metadata key an interactive turn writes for connectors it named. Distinct from
#: `mcp_presets`, because a name in one namespace is not a name in the other and one shared list
#: would make `@github` attach a connector called github.
ATTACHED_CONNECTORS_META = "connectors"

#: Set by the WebUI when a turn pins `@calendar:<id>`, resolved before the turn starts.
RESOURCE_MENTIONS_META = "resource_mentions"


@dataclass(frozen=True, slots=True)
class ConnectorAttachment:
    """How one active connector's schemas reach the prompt, and what naming it looks like."""

    name: str
    attach: str
    #: The mention kinds its manifest declares -- `calendar` for Google Calendar. Pinning an object
    #: of one of these names the connector.
    kinds: frozenset[str]


_ATTACHMENTS: dict[str, ConnectorAttachment] = {}


def set_connector_attachments(attachments: "Mapping[str, ConnectorAttachment]") -> None:
    """Record the modes of every active connector, replacing what was there."""
    _ATTACHMENTS.clear()
    _ATTACHMENTS.update(attachments)


def mention_only_connectors() -> list[str]:
    """The active connectors whose schemas wait to be asked for, in a stable order."""
    return sorted(name for name, entry in _ATTACHMENTS.items() if entry.attach == "mention")


def _named_this_turn() -> tuple[frozenset[str], frozenset[str]]:
    """The connector names and the object kinds this turn named."""
    from nanoinfra.agent.tools.context import current_request_context

    ctx = current_request_context()
    if ctx is None:
        return frozenset(), frozenset()
    metadata = ctx.metadata or {}

    names: set[str] = set()
    raw_names = cast(object, metadata.get(ATTACHED_CONNECTORS_META))
    if isinstance(raw_names, (list, tuple)):
        for entry in cast("list[object] | tuple[object, ...]", raw_names):
            # An object from the composer, a plain string from an automation.
            if isinstance(entry, Mapping):
                value = cast(Mapping[str, object], entry).get("name")
                if isinstance(value, str) and value.strip():
                    names.add(value.strip())
            elif isinstance(entry, str) and entry.strip():
                names.add(entry.strip())

    kinds: set[str] = set()
    raw_mentions = cast(object, metadata.get(RESOURCE_MENTIONS_META))
    if isinstance(raw_mentions, (list, tuple)):
        for entry in cast("list[object] | tuple[object, ...]", raw_mentions):
            if isinstance(entry, Mapping):
                kind = cast(Mapping[str, object], entry).get("kind")
                if isinstance(kind, str) and kind.strip():
                    kinds.add(kind.strip().lower())
    return frozenset(names), frozenset(kinds)


def within_agent_connector_ceiling(name: str) -> bool:
    """Whether the agent answering this turn may load this connector's schemas at all (#266).

    The mention gate below widens on request; this caps regardless. `None` -- no list declared --
    is every deployment that has narrowed nobody.
    """
    from nanoinfra.agent.tools.context import current_request_context
    from nanoinfra.session.automation_turns import acting_agent_connectors

    ctx = current_request_context()
    if ctx is None:
        return True
    allowed = acting_agent_connectors(ctx.metadata)
    return True if allowed is None else name in allowed


def is_attached(name: str) -> bool:
    """Whether this connector's schemas belong in the current request.

    The acting agent's ceiling is asked first and is not negotiable. Then the mention rule.

    An unknown connector answers yes: a registered tool whose mode was never recorded is a bug in
    our bookkeeping, and the safe reading of a bug here is the behaviour that predates the field.
    """
    if not within_agent_connector_ceiling(name):
        return False
    entry = _ATTACHMENTS.get(name)
    if entry is None or entry.attach != "mention":
        return True
    names, kinds = _named_this_turn()
    return name in names or bool(entry.kinds & kinds)


def normalize_connector_mentions(raw: object) -> list[dict[str, str]]:
    """Sanitize the connector names a client says the turn attached.

    Filtered against the connectors that are actually active, so a client cannot name one into
    existence: the answer to "may this turn use google-calendar" is config's, and this only decides
    whether the operator asked. Bounded, lower-cased and de-duplicated for the same reasons the MCP
    normaliser is.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    known = {name.lower() for name in _ATTACHMENTS}
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in list(cast("list[object] | tuple[object, ...]", raw))[:8]:
        if isinstance(entry, Mapping):
            value = cast(Mapping[str, object], entry).get("name")
            name = value.strip().lower() if isinstance(value, str) else ""
        elif isinstance(entry, str):
            name = entry.strip().lower()
        else:
            continue
        if not name or len(name) > 64 or name in seen or name not in known:
            continue
        seen.add(name)
        out.append({"name": name})
    return out


def advertisement(registry: "ToolRegistry") -> str:
    """One line per mention-only connector, for the system prompt.

    Built from the live registry, so a connector that registered no tools is not advertised as
    something the model can ask for -- telling an operator to say `@calendar` when the attachment
    would do nothing is worse than silence.
    """
    # A connector this agent may not reach is not advertised as something it can ask for.
    names = [name for name in mention_only_connectors() if within_agent_connector_ceiling(name)]
    if not names:
        return ""
    counts = {
        source.removeprefix("connector:"): total
        for source, total in registry.source_counts().items()
        if source.startswith("connector:")
    }
    lines: list[str] = []
    for name in names:
        total = counts.get(name)
        if not total:
            continue
        entry = _ATTACHMENTS[name]
        pinning = (
            f", or `@{sorted(entry.kinds)[0]}:<id>` to pin one" if entry.kinds else ""
        )
        lines.append(
            f"- `{name}` — {total} operations, not loaded. "
            f"Say `@{name}` to use them this turn{pinning}."
        )
    if not lines:
        return ""
    return (
        "# Available data connectors\n\n"
        "These connectors are active and their operations are **not** in this prompt. You cannot "
        "call them until the user names one. If a request needs one, say which and ask the user to "
        "attach it -- do not substitute a different tool.\n\n" + "\n".join(lines)
    )


__all__ = [
    "ATTACHED_CONNECTORS_META",
    "RESOURCE_MENTIONS_META",
    "ConnectorAttachment",
    "advertisement",
    "is_attached",
    "within_agent_connector_ceiling",
    "mention_only_connectors",
    "normalize_connector_mentions",
    "set_connector_attachments",
]
