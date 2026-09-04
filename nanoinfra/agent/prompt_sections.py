"""What a deployment may change about the prompt, section by section (#256).

The prompt is assembled from named sections (`agent/context.py`), and the manifest already knows
every one of them and what it costs (#203). What was missing is the other half of that record:
**which of those sections are an operator's to change.**

Two wrong answers were available, and both were considered:

- *One textarea holding the whole system prompt.* An operator can then delete the tool contract and
  the safety notes, after which the gate still refuses the action but the model no longer knows the
  rules it is supposed to be following -- the worst of both. The refusal stops being explicable.
- *Addendum only.* Too blunt in the other direction. Specialising an agent by replacing what it
  remembers, or by writing its workspace's own instruction files, is a real need.

So each section carries a permission, and this module is where that table lives:

===============  =========================================================================
`replaceable`    what the agent remembers -- a deployment's own text in place of ours
`workspace`      already yours by another route (`AGENTS.md` and friends), per workspace
`fixed`          the tool contract and the safety notes; no override path exists at all
`derived`        computed from config (skills, connectors, groups) -- editing config edits it
`append_only`    the per-agent addendum, which is added and can displace nothing
===============  =========================================================================

Two structural properties, rather than advice a reader has to remember:

1. **An unknown section name is `fixed`.** A section added later is not replaceable until somebody
   decides it is, so forgetting to update this table fails closed instead of opening a hole.
2. **The addendum is a separate argument, not an entry in the override map.** It cannot name a
   section, so it cannot take one's place -- the only thing it can do is be appended. That is why
   `compose_sections` takes the two as different parameters with different types of effect.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

#: The section a per-agent addendum becomes. Named here because three places need the same
#: string: the assembler that appends it, the permission table, and the panel that shows it.
ADDENDUM_SECTION = "Agent addendum"


class SectionPermission(str, Enum):
    """What a deployment may do to one prompt section.

    A `str` enum because the value travels to a browser and reads there as-is; a panel showing
    `SectionPermission.FIXED` would be showing a Python repr to an operator.
    """

    REPLACEABLE = "replaceable"
    WORKSPACE = "workspace"
    FIXED = "fixed"
    DERIVED = "derived"
    APPEND_ONLY = "append_only"


#: Every section the system prompt can contain, in the order it is assembled, with the permission
#: that governs it. The names are the ones `ContextBuilder.build_system_prompt` records, so a
#: rename there without a rename here makes the section `fixed` rather than silently editable.
#:
#: `Safety notes` is the row the design calls *gate and safety notes*: what is refused and why. It
#: is its own section for a reason worth stating -- it used to live inside the identity text, which
#: is the one section a deployment is most likely to replace. A persona swap would have taken the
#: prompt-injection rules with it, and nothing would have said so.
SECTION_PERMISSIONS: Mapping[str, SectionPermission] = {
    # Not the persona, despite the name, and therefore not replaceable. This section is computed:
    # the platform, and the paths to the agent's own `SOUL.md`, `MEMORY.md` and history log. A
    # deployment that replaced it would leave the model without the location of its own memory.
    # The persona reaches the prompt through `Bootstrap files` -- `SOUL.md` is a workspace file,
    # which is why that row reads *already yours* rather than *replaceable*.
    "Runtime": SectionPermission.DERIVED,
    "Safety notes": SectionPermission.FIXED,
    "Bootstrap files": SectionPermission.WORKSPACE,
    "Tool usage notes": SectionPermission.FIXED,
    "Memory": SectionPermission.REPLACEABLE,
    "Active skills": SectionPermission.DERIVED,
    "Skills catalogue": SectionPermission.DERIVED,
    "MCP servers advertised": SectionPermission.DERIVED,
    "Connectors advertised": SectionPermission.DERIVED,
    "Tool groups advertised": SectionPermission.DERIVED,
    ADDENDUM_SECTION: SectionPermission.APPEND_ONLY,
    "Recent history": SectionPermission.DERIVED,
    "Session summary": SectionPermission.DERIVED,
}

#: The sections whose size is the same on every turn of a deployment, so a panel can state their
#: cost without waiting for a turn to happen. Everything else depends on the conversation, the
#: workspace or the memory file, and a number quoted for those would be a number from one turn
#: presented as a property of the agent.
DEPLOYMENT_STATIC_SECTIONS: frozenset[str] = frozenset(
    {"Safety notes", "Tool usage notes", ADDENDUM_SECTION}
)


class PromptSectionRefusedError(ValueError):
    """An override named a section that is not the deployment's to replace.

    An error rather than a filtered-out entry: a config that asks to replace the tool contract has
    an intention behind it, and silently ignoring the request produces a prompt nobody expects and
    a debugging session nobody enjoys. The names are carried so the message can list them.
    """

    def __init__(self, names: Sequence[str]) -> None:
        self.names: tuple[str, ...] = tuple(names)
        listed = ", ".join(repr(name) for name in self.names)
        super().__init__(
            f"these prompt sections cannot be replaced: {listed}. "
            "The tool contract and the safety notes are fixed, and a derived section changes by "
            "changing the config it is derived from. Use the agent's addendum to add instructions."
        )


def permission_for(name: str) -> SectionPermission:
    """The permission governing ``name``, defaulting to ``fixed`` for anything unlisted.

    Fails closed on purpose. A section that arrives without a decision about it is not replaceable
    until the decision is made here, in a file a human reviews.
    """
    return SECTION_PERMISSIONS.get(name, SectionPermission.FIXED)


def is_replaceable(name: str) -> bool:
    """True when a deployment's own text may stand in for this section's."""
    return permission_for(name) is SectionPermission.REPLACEABLE


def resolve_overrides(overrides: Mapping[str, str] | None) -> dict[str, str]:
    """Validate a section-override map and return it, or refuse the whole thing.

    All-or-nothing: a partially applied override set is a prompt that matches neither what the
    deployment asked for nor what the platform ships, and the operator would have to diff two
    files to find out which. Empty values are dropped instead of refused, because "" is how a
    config file spells *leave this alone*.
    """
    if not overrides:
        return {}
    wanted = {name: text for name, text in overrides.items() if text.strip()}
    refused = [name for name in wanted if not is_replaceable(name)]
    if refused:
        raise PromptSectionRefusedError(sorted(refused))
    return wanted


def declared_overrides(agent: Any) -> dict[str, str]:
    """The section overrides a named agent declares, validated.

    Read with ``getattr`` in this one place. The config field is the agent's, and this module is
    the only thing that has to know whether it exists yet -- every caller gets the same validated
    map either way, and an agent that declares nothing gets ``{}``.
    """
    declared: object = getattr(agent, "prompt_sections", None) or {}
    if not isinstance(declared, Mapping):
        return {}
    pairs = cast("Mapping[object, object]", declared)
    return resolve_overrides({str(key): str(value) for key, value in pairs.items()})


@dataclass(frozen=True, slots=True)
class ComposedSection:
    """One section of an assembled prompt, and where its text came from."""

    name: str
    text: str
    permission: SectionPermission
    #: True when ``text`` is the deployment's and not the platform's.
    overridden: bool = False


def compose_sections(
    platform_sections: Sequence[tuple[str, str]],
    *,
    overrides: Mapping[str, str] | None = None,
    addendum: str = "",
) -> list[ComposedSection]:
    """Apply the overrides a deployment is allowed to make, then append the addendum.

    The two parameters do different things and cannot be confused for each other, which is the
    whole point: ``overrides`` is keyed by section name and is checked against the table above,
    while ``addendum`` is a bare string with nowhere to put a section name. An addendum therefore
    **cannot displace anything** -- not the tool contract, not the safety notes, not even the
    persona it is specialising. It arrives after them.

    A pure function, so the rule can be tested without assembling a real prompt, and so the panel
    that shows the composition and the assembler that builds it agree by construction.
    """
    resolved = resolve_overrides(overrides)
    composed: list[ComposedSection] = []
    for name, text in platform_sections:
        replacement = resolved.get(name)
        composed.append(
            ComposedSection(
                name=name,
                text=replacement if replacement is not None else text,
                permission=permission_for(name),
                overridden=replacement is not None,
            )
        )
    if addendum.strip():
        composed.append(
            ComposedSection(
                name=ADDENDUM_SECTION,
                text=addendum.strip(),
                permission=SectionPermission.APPEND_ONLY,
            )
        )
    return composed


def section_inventory(
    *,
    overrides: Mapping[str, str] | None = None,
    addendum: str = "",
) -> list[dict[str, Any]]:
    """Every section a prompt can contain, with its permission -- the shape a panel reads.

    The whole table rather than one turn's sections, because the question the panel answers is
    *what is this agent told, and what of it is mine to change*. A section that happens to be
    empty on the turn you are looking at still has a permission, and hiding it would make the
    answer depend on which turn you asked after.
    """
    resolved = resolve_overrides(overrides)
    rows: list[dict[str, Any]] = []
    for name, permission in SECTION_PERMISSIONS.items():
        if name == ADDENDUM_SECTION:
            present = bool(addendum.strip())
        else:
            present = True
        rows.append(
            {
                "name": name,
                "permission": permission.value,
                "overridden": name in resolved,
                "present": present,
                # False means "this section's size is a property of the turn, not of the agent".
                # The panel says so rather than quoting one turn's number as a constant.
                "static": name in DEPLOYMENT_STATIC_SECTIONS,
            }
        )
    return rows


__all__ = [
    "ADDENDUM_SECTION",
    "DEPLOYMENT_STATIC_SECTIONS",
    "SECTION_PERMISSIONS",
    "ComposedSection",
    "PromptSectionRefusedError",
    "SectionPermission",
    "compose_sections",
    "declared_overrides",
    "is_replaceable",
    "permission_for",
    "resolve_overrides",
    "section_inventory",
]
