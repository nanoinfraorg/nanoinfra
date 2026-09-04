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
from typing import TYPE_CHECKING, Iterable, Mapping, cast

if TYPE_CHECKING:
    from nanoinfra.agent.tools.registry import ToolRegistry
    from nanoinfra.config.schema import ToolGroupConfig

#: The metadata key a turn writes for the groups it named. Its own namespace, like `mcp_presets`
#: and `connectors`: `@servers` must not attach an MCP server that happens to be called servers.
ATTACHED_GROUPS_META = "tool_groups"

#: The discovery tool's name. It can never be deferred -- a search tool a search must first find
#: cannot be found -- so `is_attached` short-circuits it even if a config names it into a group.
TOOL_SEARCH_TOOL_NAME = "tool_search"

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
#: turn id -> the `search`-mode groups the model attached this turn via `tool_search`.
#:
#: Module state keyed by `turn_id` rather than a field on `RequestContext`, for the reason the
#: commissioning collector is (AGENTS.md): the context is a frozen per-turn snapshot the tool
#: cannot mutate, and the turn crosses the bus into another task. The loop clears the turn's
#: entry when the turn ends (`reset_search_attached`); an entry that outlived its turn would leak
#: an attach into the next turn, which open question 2 in the proposal says it must not.
_SEARCH_ATTACHED: dict[str, set[str]] = {}


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


#: The attach modes that hide a group's schemas until the turn asks for them. `mention` waits for
#: the user to name `@group`; `search` waits for the model to call `tool_search`. Both are gated by
#: the same predicate in `is_attached` -- the only difference is who does the naming.
_DEFERRED_MODES = frozenset({"mention", "search"})


def mention_only_groups() -> list[str]:
    """The declared groups a *user* widens by naming `@group`, in a stable order."""
    return sorted(name for name, group in _GROUPS.items() if group.attach == "mention")


def search_mode_groups() -> list[str]:
    """The declared groups the *model* widens by calling `tool_search`, in a stable order."""
    return sorted(name for name, group in _GROUPS.items() if group.attach == "search")


def group_of(tool_name: str) -> str | None:
    """The deferred group gating this tool, for a caller reporting why it is absent."""
    for name in sorted(_MEMBERSHIP.get(tool_name, ())):
        if _GROUPS[name].attach in _DEFERRED_MODES:
            return name
    return None


def attach_group_for_turn(turn_id: str | None, name: str) -> None:
    """Record that the model attached a `search` group this turn.

    Keyed by `turn_id` so a concurrent turn's attach never widens this one; a `None` turn id (a
    fallback context that carries none) is ignored rather than pooled under a shared key, because
    pooling would be the cross-turn leak this store exists to prevent.
    """
    if not turn_id:
        return
    _SEARCH_ATTACHED.setdefault(turn_id, set()).add(name)


def search_attached(turn_id: str | None) -> frozenset[str]:
    """The `search` groups the model attached on this turn."""
    if not turn_id:
        return frozenset()
    return frozenset(_SEARCH_ATTACHED.get(turn_id, ()))


def reset_search_attached(turn_id: str | None) -> None:
    """Drop this turn's model-driven attachments. Called by the loop when the turn ends."""
    if turn_id:
        _SEARCH_ATTACHED.pop(turn_id, None)


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
    # The model's own attachments this turn, via `tool_search`. Unioned here so `is_attached`
    # treats a `search` group the model found exactly like a `mention` group the user named --
    # one gate, two ways of naming into it.
    names |= search_attached(ctx.turn_id)
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


def agent_tool_group_ceiling() -> tuple[str, ...] | None:
    """The groups the agent answering this turn declared, ``None`` when it declared no ceiling.

    Read from the turn rather than from config, because the agent was resolved when the turn was
    built and the answer travels with it (``AUTOMATION_AGENT_META``). Empty is unrestricted, which
    is what every turn in a deployment that names no agent gets.
    """
    from nanoinfra.agent.tools.context import current_request_context
    from nanoinfra.session.automation_turns import automation_agent_tool_groups

    ctx = current_request_context()
    if ctx is None:
        return None
    return automation_agent_tool_groups(ctx.metadata)


def within_agent_ceiling(tool_name: str) -> bool:
    """Whether the agent answering this turn may reach this tool at all.

    A different question from the mention gate below, and it has to be asked separately: a
    mention *widens* on request, and this **caps** regardless of what the turn asks for. The rule
    itself is ``tools_for_groups``, imported rather than restated so an automation's agent and a
    delegated agent are capped by one implementation.
    """
    allowed = agent_tool_group_ceiling()
    # `None` is "no ceiling declared" and an empty tuple is "declared, and it is empty" -- the
    # second caps the turn to the ungrouped tools rather than lifting the cap.
    if allowed is None:
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
        f"({', '.join(agent_tool_group_ceiling() or ()) or 'no tool groups at all'}). It is "
        "not available on this turn, and "
        "nothing in the request can widen it -- the agent's configuration is the ceiling."
    )


#: The tool names this gateway registered, recorded by the thing that registered them.
#:
#: The Settings panel that declares a group needs the tools a member list may name, and no route
#: from a settings request reaches the live registry: the payload is a pure function over config.
#: Recorded here rather than rediscovered, because a tool's name is an instance property and
#: re-deriving it would mean constructing every tool a second time to ask.
_REGISTERED_TOOLS: list[str] = []


def set_registered_tools(names: "Iterable[str]") -> None:
    """Record what this gateway registered. Called once, after the tools are built."""
    _REGISTERED_TOOLS.clear()
    _REGISTERED_TOOLS.extend(sorted({str(name) for name in names if str(name).strip()}))


def registered_tools() -> tuple[str, ...]:
    """The registered tool names, empty before the gateway has built any.

    Empty means *not yet known*, never *none exist* -- a caller that treated it as the whole truth
    would tell an operator their deployment has no tools.
    """
    return tuple(_REGISTERED_TOOLS)


def is_attached(tool_name: str) -> bool:
    """Whether this tool's schema belongs in the current request.

    The acting agent's ceiling is asked first, and it is not negotiable: a tool outside the
    groups that agent declared is absent whatever the turn mentions.

    Then the deferral gate, with three answers, and the middle one is the one worth stating:

    - A tool in no group, or in no group set to a deferred mode (`mention`/`search`), is always
      available. That is every tool in a default deployment.
    - A tool that is in an `always` group *and* a deferred group is available. Both are explicit
      operator statements, and the one that widens is the one that was written down last in a file
      where the alternative -- a capability silently withdrawn -- is the worse failure.
    - Otherwise it needs one of its groups named this turn -- by the user (`mention`, via `@group`)
      or by the model (`search`, via `tool_search`). Both land in `attached_groups`.
    """
    if tool_name == TOOL_SEARCH_TOOL_NAME:
        # Never group-gated: the tool that surfaces deferred tools must itself stay loaded, or the
        # model has no way to search. A misconfiguration that grouped it is overridden here.
        return True
    if not within_agent_ceiling(tool_name):
        return False
    groups = _MEMBERSHIP.get(tool_name)
    if not groups:
        return True
    gating = {name for name in groups if _GROUPS[name].attach in _DEFERRED_MODES}
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


def _present_searchable_tools(group: ToolGroup, registry: "ToolRegistry | None") -> list[str]:
    """A `search` group's tools that are installed *and* within the acting agent's ceiling.

    The ceiling is asked here, not just in `is_attached`, because the whole point of the ceiling
    is that a tool outside it can never be reached -- so it must never be a search *result* either,
    or the model would learn a name it is then refused. Presence uses the live registry when we
    have one, and the recorded `registered_tools()` otherwise (the WebUI has no registry to hand).
    """
    known = set(registered_tools())
    out: list[str] = []
    for tool in sorted(group.tools):
        present = registry.has(tool) if registry is not None else (tool in known)
        if present and within_agent_ceiling(tool):
            out.append(tool)
    return out


def search_groups(query: str, registry: "ToolRegistry | None" = None) -> list[dict[str, object]]:
    """The `search`-mode groups matching a topic, each with the tools it would load.

    Matching is case-insensitive substring of any query word against the group's name, its
    description and its tool names -- a small closed corpus, so nothing heavier than this is
    warranted (proposals/tool-search.md: no embedding index, no service). A group with no query,
    or with no present-and-permitted tools, is not a match. Ceiling-filtered via
    `_present_searchable_tools`, so a group outside the acting agent's contract never surfaces.
    """
    # Two-letter words ("at", "no", "of") are substrings of half the English language and match
    # noise ("at" is inside "update"); drop them. An **empty** query is still a real request --
    # `searchable_groups` uses it to list everything -- so distinguish "no words at all" (list all)
    # from "only short words" (match nothing) by keying the guard on the raw split, not the filter.
    raw = (query or "").lower().split()
    words = [w for w in raw if len(w) >= 3]
    matches: list[dict[str, object]] = []
    for name in search_mode_groups():
        group = _GROUPS[name]
        tools = _present_searchable_tools(group, registry)
        if not tools:
            continue
        haystack = " ".join([name.lower(), group.description.lower(), *(t.lower() for t in tools)])
        if raw and not any(w in haystack for w in words):
            continue
        matches.append({"name": name, "description": group.description, "tools": tools})
    return matches


def searchable_groups(registry: "ToolRegistry | None" = None) -> list[dict[str, object]]:
    """Every `search`-mode group with present, permitted tools -- what the model *could* find.

    A search with no words returns this whole set, and `tool_search` uses it to tell the model
    which topics were considered when a query matched nothing, so it can say "there is no tool for
    that" rather than invent one.
    """
    return search_groups("", registry)


#: The one pointer that stands in for every `search`-mode surface at once (groups, MCP servers,
#: connectors). Deliberately not one line per item: that per-item enumeration is what `mention`
#: pays, and the flat cost here is what lets a deployment defer far more than two clusters
#: (proposals/tool-search.md). The loop emits it when *any* surface has something searchable, so it
#: lives here as a constant the loop can reach without re-deriving the wording.
SEARCH_POINTER_TEXT = (
    "# Searchable tools\n\n"
    "Some installed tools are not loaded into this prompt. If a request needs a capability you "
    "have no tool for, call `tool_search` with the topic (for example the service or the "
    "action) to load the matching tools for this turn, then use them. Do not tell the user to "
    "attach anything, and do not substitute a different tool -- search first."
)


def has_searchable_groups(registry: "ToolRegistry") -> bool:
    """Whether any `search`-mode group has a present, permitted tool this deployment could load."""
    return any(
        _present_searchable_tools(_GROUPS[name], registry) for name in search_mode_groups()
    )


def search_advertisement(registry: "ToolRegistry") -> str:
    """The shared pointer, emitted when a `search`-mode group has a present, permitted tool.

    Kept for callers that only gate on groups; the loop ORs this with the MCP and connector
    surfaces so the pointer appears when *any* of the three defers by search.
    """
    return SEARCH_POINTER_TEXT if has_searchable_groups(registry) else ""


def declared_groups() -> dict[str, ToolGroup]:
    """The declared groups, for the WebUI surface that offers them as mentions."""
    return dict(_GROUPS)


__all__ = [
    "ATTACHED_GROUPS_META",
    "BUILTIN_GROUPS",
    "SEARCH_POINTER_TEXT",
    "TOOL_SEARCH_TOOL_NAME",
    "ToolGroup",
    "advertisement",
    "agent_ceiling_refusal",
    "agent_tool_group_ceiling",
    "attach_group_for_turn",
    "has_searchable_groups",
    "attached_groups",
    "declared_groups",
    "group_membership",
    "group_of",
    "is_attached",
    "mention_only_groups",
    "mentions_in_text",
    "normalize_group_mentions",
    "reset_search_attached",
    "search_advertisement",
    "search_attached",
    "search_groups",
    "search_mode_groups",
    "searchable_groups",
    "set_tool_groups",
    "within_agent_ceiling",
]
