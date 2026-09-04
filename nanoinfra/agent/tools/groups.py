"""When a group of built-in tool schemas reaches the prompt (#210).

Measured on the demo: a greeting cost 17,302 tokens, 10,273 of them the 31 built-in tool schemas.
Two clusters accounted for 3,857 of those -- 22% of the whole prompt -- for capabilities the turn
never touched:

| group        | tools | tokens |
|--------------|-------|--------|
| Diagrams     | 5     | 2,438  |
| SSH servers  | 6     | 1,419  |

`attach: "mention"` already answers this for an MCP server and for a connector, because each has a
name to hang the mode on. A built-in tool has neither: `exec` and `create_diagram` are both just
built-ins. So this file adds the missing concept -- a **named group of built-in tools** -- and then
reuses the machinery the other two paths already share: the registry's availability filter, and one
advertised line inside the stable prompt block.

The advertised line is not decoration; it is what makes the trade acceptable. A model that cannot
see that a capability exists cannot say "I can do that if you attach it": it fails, or quietly
substitutes something worse, and a silently worse answer is harder to notice than a large bill.

**Groups ship defined but `always`.** `BUILTIN_GROUPS` names the members so an operator does not
have to list five tool names to save 2,438 tokens, and the default mode changes nothing on upgrade
-- a tool that vanished because a release regrouped it would be a behaviour change nobody asked
for. Turning one on is one line of config.

Module state rather than a field on each tool, for the reason the MCP and connector sides have the
same shape: a group's mode changes in config while its tool instances are live, and eleven of them
disagreeing about it is the failure this file exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, cast

if TYPE_CHECKING:
    from nanoinfra.agent.tools.registry import ToolRegistry
    from nanoinfra.config.schema import ToolGroupConfig

#: The metadata key a turn writes for the groups it named. Its own namespace, like `mcp_presets`
#: and `connectors`: `@servers` must not attach an MCP server that happens to be called servers.
ATTACHED_GROUPS_META = "tool_groups"

#: The groups nanoinfra defines, so config carries a mode rather than a list of tool names. Both
#: were measured; the token figures are in the module docstring.
BUILTIN_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "diagrams": (
        "read, create and update saved infrastructure diagrams",
        (
            "create_diagram",
            "update_diagram",
            "get_diagram",
            "list_diagrams",
            "list_diagram_components",
        ),
    ),
    "servers": (
        "register SSH servers and run commands on them",
        (
            "create_server",
            "update_server",
            "delete_server",
            "get_server",
            "list_servers",
            "execute_on_server",
            # A server's NOTES.md is part of the same cluster (#223), so one operator switch
            # governs the whole Servers surface rather than leaving its memory half-attached.
            "device_notes",
        ),
    ),
}


@dataclass(frozen=True, slots=True)
class ToolGroup:
    """One declared group of built-in tools, and how its schemas reach the prompt."""

    name: str
    attach: str
    tools: frozenset[str]
    description: str = ""


_GROUPS: dict[str, ToolGroup] = {}
#: tool name -> the groups holding it. A tool may sit in more than one; see `is_attached`.
_MEMBERSHIP: dict[str, set[str]] = {}
#: `group_membership()`'s answer, which the agent ceiling asks once per tool per provider call.
#: Emptied by `set_tool_groups`, the only thing that can change it. Empty therefore means "not
#: built yet": a real answer always holds the `BUILTIN_GROUPS` members, which are never none.
_FULL_MEMBERSHIP: dict[str, frozenset[str]] = {}


def set_tool_groups(groups: "Mapping[str, ToolGroupConfig] | None") -> None:
    """Record the declared groups, replacing what was there.

    A group that names no tools inherits `BUILTIN_GROUPS`' members, which is what lets config say
    `{"diagrams": {"attach": "mention"}}` and mean the five diagram tools. A group that does name
    tools is taken at its word -- including a name nanoinfra does not ship, because a deployment
    with a plugin tool has every right to group it.
    """
    _GROUPS.clear()
    _MEMBERSHIP.clear()
    _FULL_MEMBERSHIP.clear()
    for name, cfg in (groups or {}).items():
        declared = tuple(getattr(cfg, "tools", ()) or ())
        members = declared or BUILTIN_GROUPS.get(name, ("", ()))[1]
        if not members:
            # Neither declared nor built-in: nothing to gate, and keeping it would advertise a
            # group that can never load a tool.
            continue
        description = getattr(cfg, "description", "") or BUILTIN_GROUPS.get(name, ("", ()))[0]
        group = ToolGroup(
            name=name,
            attach=getattr(cfg, "attach", "always") or "always",
            tools=frozenset(members),
            description=description,
        )
        _GROUPS[name] = group
        for tool_name in group.tools:
            _MEMBERSHIP.setdefault(tool_name, set()).add(name)


def mention_only_groups() -> list[str]:
    """The declared groups whose schemas wait to be asked for, in a stable order."""
    return sorted(name for name, group in _GROUPS.items() if group.attach == "mention")


def group_of(tool_name: str) -> str | None:
    """The mention-mode group gating this tool, for a caller reporting why it is absent."""
    for name in sorted(_MEMBERSHIP.get(tool_name, ())):
        if _GROUPS[name].attach == "mention":
            return name
    return None


def mentions_in_text(text: str | None) -> frozenset[str]:
    """The declared groups named as `@group` in a message.

    The MCP and connector gates read structured mentions from the composer, because their names
    come from a live catalogue a person picks from. A group name does not: it is a short, closed
    set written in config. So the text is enough, and reading it here buys something the composer
    could not -- `@diagrams` works from Telegram, from Discord and from the CLI, none of which
    have a composer at all. The advertised line tells the user to say `@diagrams`, so it had
    better work wherever they can say it.

    Matched against the declared names only, so nothing is attached by a name nobody configured,
    and a false positive costs one turn of schemas rather than a capability.
    """
    if not text or "@" not in text:
        return frozenset()
    lowered = text.lower()
    return frozenset(
        name for name in _GROUPS if f"@{name.lower()}" in lowered
    )


def attached_groups() -> frozenset[str]:
    """The groups this turn named, however it named them.

    Metadata for a composer mention and for an automation's declaration; the message text for
    everybody else. Both, rather than either: an automation types no `@`, and a Telegram user
    sends no metadata.
    """
    from nanoinfra.agent.tools.context import current_request_context

    ctx = current_request_context()
    if ctx is None:
        return frozenset()
    names: set[str] = set(mentions_in_text(ctx.original_user_text))
    raw = cast(object, (ctx.metadata or {}).get(ATTACHED_GROUPS_META))
    if isinstance(raw, (list, tuple)):
        for entry in cast("list[object] | tuple[object, ...]", raw):
            # An object from the composer, a plain string from an automation.
            if isinstance(entry, Mapping):
                value = cast(Mapping[str, object], entry).get("name")
                if isinstance(value, str) and value.strip():
                    names.add(value.strip())
            elif isinstance(entry, str) and entry.strip():
                names.add(entry.strip())
    return frozenset(names)


def group_membership() -> "Mapping[str, frozenset[str]]":
    """tool name -> the groups holding it, from config first and the built-ins for the rest.

    Config first because a group a deployment declared is the one that governs; ``BUILTIN_GROUPS``
    only answers for a group config never mentioned. Both halves are needed: a deployment that
    declares no groups at all still has diagram tools and server tools, and a ceiling computed
    against an empty membership would cap nothing.

    Cached, because the ceiling asks this once per tool per provider call and the answer only
    changes when ``set_tool_groups`` replaces the declarations.
    """
    if _FULL_MEMBERSHIP:
        return _FULL_MEMBERSHIP
    membership: dict[str, set[str]] = {}
    for name, group in _GROUPS.items():
        for tool_name in group.tools:
            membership.setdefault(tool_name, set()).add(name)
    for name, (_description, tools) in BUILTIN_GROUPS.items():
        if name in _GROUPS:
            continue
        for tool_name in tools:
            membership.setdefault(tool_name, set()).add(name)
    _FULL_MEMBERSHIP.update(
        {name: frozenset(groups) for name, groups in membership.items()}
    )
    return _FULL_MEMBERSHIP


def agent_tool_group_ceiling() -> tuple[str, ...]:
    """The groups the agent answering this turn declared, empty when it declared none.

    Read from the turn rather than from config, because the agent was resolved when the turn was
    built and the answer travels with it (``AUTOMATION_AGENT_META``). Empty is unrestricted, which
    is what every turn in a deployment that names no agent gets.
    """
    from nanoinfra.agent.tools.context import current_request_context
    from nanoinfra.session.automation_turns import automation_agent_tool_groups

    ctx = current_request_context()
    if ctx is None:
        return ()
    return automation_agent_tool_groups(ctx.metadata)


def within_agent_ceiling(tool_name: str) -> bool:
    """Whether the agent answering this turn may reach this tool at all.

    A different question from the mention gate below, and it has to be asked separately: a
    mention *widens* on request, and this **caps** regardless of what the turn asks for. The rule
    itself is ``tools_for_groups``, imported rather than restated so an automation's agent and a
    delegated agent are capped by one implementation.
    """
    allowed = agent_tool_group_ceiling()
    if not allowed:
        return True
    from nanoinfra.agent.delegation import tools_for_groups

    return tool_name in tools_for_groups([tool_name], allowed, group_membership())


def agent_ceiling_refusal(tool_name: str) -> str | None:
    """The reason this tool is outside the acting agent's contract, or ``None``.

    Hiding the schema is not enough on its own. A model that names a tool it never saw would
    otherwise reach it, and "the agent is a ceiling" has to mean the call is refused and not
    merely undocumented. The text names the agent, because the reader is the model and a refusal
    it can explain to a person is worth more than a bare denial.
    """
    if within_agent_ceiling(tool_name):
        return None
    from nanoinfra.agent.tools.context import current_request_context

    ctx = current_request_context()
    # `RequestContext.agent` is the resolved answer -- the loop already checked the name against
    # the roster -- so the message names who is acting rather than who was asked for.
    agent = ctx.agent if ctx is not None else None
    who = f"`{agent}`" if agent else "this agent"
    return (
        f"Error: Tool '{tool_name}' is outside {who}'s tool groups "
        f"({', '.join(agent_tool_group_ceiling())}). It is not available on this turn, and "
        "nothing in the request can widen it -- the agent's configuration is the ceiling."
    )


def is_attached(tool_name: str) -> bool:
    """Whether this tool's schema belongs in the current request.

    The acting agent's ceiling is asked first, and it is not negotiable: a tool outside the
    groups that agent declared is absent whatever the turn mentions.

    Then the mention gate, with three answers, and the middle one is the one worth stating:

    - A tool in no group, or in no group set to `mention`, is always available. That is every tool
      in a default deployment.
    - A tool that is in an `always` group *and* a `mention` group is available. Both are explicit
      operator statements, and the one that widens is the one that was written down last in a file
      where the alternative -- a capability silently withdrawn -- is the worse failure.
    - Otherwise it needs one of its groups named this turn.
    """
    if not within_agent_ceiling(tool_name):
        return False
    groups = _MEMBERSHIP.get(tool_name)
    if not groups:
        return True
    gating = {name for name in groups if _GROUPS[name].attach == "mention"}
    if len(gating) != len(groups):
        return True
    return bool(gating & attached_groups())


def normalize_group_mentions(raw: object) -> list[dict[str, str]]:
    """Sanitize the group names a client says the turn attached.

    Filtered against the declared groups, so a client cannot name one into existence: whether a
    group exists is config's answer, and this only decides whether the operator asked for it.
    Bounded, lower-cased and de-duplicated for the same reasons the MCP normaliser is.
    """
    if not isinstance(raw, (list, tuple)):
        return []
    known = {name.lower() for name in _GROUPS}
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
    """One line per mention-only group, for the system prompt.

    Counted against the live registry rather than against config, so a group whose tools are all
    disabled in this deployment is not advertised as something the model can ask for -- telling a
    user to say `@diagrams` when the attachment would load nothing is worse than silence.
    """
    names = mention_only_groups()
    if not names:
        return ""
    lines: list[str] = []
    for name in names:
        group = _GROUPS[name]
        present = sorted(tool for tool in group.tools if registry.has(tool))
        if not present:
            continue
        purpose = f" — {group.description}" if group.description else ""
        lines.append(
            f"- `{name}`{purpose}: {len(present)} tools, not loaded. "
            f"Say `@{name}` to use them this turn."
        )
    if not lines:
        return ""
    return (
        "# Available tool groups\n\n"
        "These built-in tools are installed and their schemas are **not** in this prompt. You "
        "cannot call them until the user names the group. If a request needs one, say which and "
        "ask the user to attach it -- do not substitute a different tool.\n\n" + "\n".join(lines)
    )


def declared_groups() -> dict[str, ToolGroup]:
    """The declared groups, for the WebUI surface that offers them as mentions."""
    return dict(_GROUPS)


__all__ = [
    "ATTACHED_GROUPS_META",
    "BUILTIN_GROUPS",
    "ToolGroup",
    "advertisement",
    "agent_ceiling_refusal",
    "agent_tool_group_ceiling",
    "attached_groups",
    "declared_groups",
    "group_membership",
    "group_of",
    "is_attached",
    "mention_only_groups",
    "mentions_in_text",
    "normalize_group_mentions",
    "set_tool_groups",
    "within_agent_ceiling",
]
