"""One agent's prompt, section by section, with the permission on each (#256).

Pattern: ``nanoinfra/webui/named_agents_api.py`` -- a pure function the gateway HTTP dispatcher
(``ws_http.py``) calls, with ``check_api_token`` at the call site rather than in here.

**What this answers, and what it deliberately does not.** The roster in ``settings_api`` carries
counts and never the bindings themselves, because an agent's tool groups, skills and delegates are
its *authority* and a browser that could enumerate them would be reading the authorization model
out of a settings payload. This route is the other kind of read: it answers *what is this agent
told, and what of that is mine to change*. So it carries section names, their permission, and the
addendum's own text -- which is prompt content, not authority. An addendum cannot widen what an
agent may reach; it is appended after the platform's sections and can displace none of them
(`agent/prompt_sections.py`). Tool groups, skills, connectors and delegates stay out of here, and
the line between the two reads is worth keeping in one sentence: *instructions, yes; permissions,
no.*

Token figures are given only for the sections whose size is a property of the deployment rather
than of a turn -- the safety notes, the tool contract, the addendum. Memory, bootstrap files and
the history change with every turn, and a number quoted for those would be one turn's measurement
presented as a constant. The per-turn numbers already exist where they belong: on the turn, in the
prompt manifest (#203).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from nanoinfra.agent.prompt_manifest import estimate_tokens
from nanoinfra.agent.prompt_sections import (
    ADDENDUM_SECTION,
    REPLACEMENT_WARNINGS,
    declared_overrides,
    section_inventory,
)
from nanoinfra.config.schema import Config
from nanoinfra.utils.prompt_templates import render_template

#: Which template a deployment-static section is measured from. Rendered on demand rather than at
#: import: this is a settings panel's read, not a hot path, and a module-level render would cost
#: every process that imports the gateway.
_STATIC_SECTION_TEMPLATES = {
    "Runtime": "agent/identity.md",
    "Safety notes": "agent/safety_notes.md",
    "Tool usage notes": "agent/tool_contract.md",
}

#: Sections whose template carries `{{ }}` placeholders the turn fills in. Their **source** is
#: what the panel shows and what a replacement starts from: a rendered copy would silently bake
#: one turn's paths into text the operator then edits, and the placeholders are exactly what a
#: replacement has to keep.
_TEMPLATED_SECTIONS = frozenset({"Runtime"})


def _static_tokens(name: str, addendum: str) -> int | None:
    """The token cost of a section that costs the same on every turn, or ``None``."""
    if name == ADDENDUM_SECTION:
        return estimate_tokens(addendum.strip()) if addendum.strip() else 0
    template = _STATIC_SECTION_TEMPLATES.get(name)
    if template is None:
        return None
    return estimate_tokens(render_template(template))


def _section_placeholders(name: str) -> list[str]:
    """The `{{ }}` names a section's text must keep, in the order they appear."""
    template = _STATIC_SECTION_TEMPLATES.get(name)
    if template is None or name not in _TEMPLATED_SECTIONS:
        return []
    source = _template_source(template)
    seen: list[str] = []
    for match in re.finditer(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", source):
        if match.group(1) not in seen:
            seen.append(match.group(1))
    return seen


def _template_source(template: str) -> str:
    """The template file as written, placeholders intact."""
    return (Path(__file__).resolve().parents[1] / "templates" / template).read_text(
        encoding="utf-8"
    )


def _platform_text(name: str, addendum: str) -> str | None:
    """The platform's own text for a section, or ``None`` when a turn is what produces it.

    Carried because a panel that lists section *names* is a map of the prompt and not the prompt:
    to decide whether to replace a section you have to be able to read what it currently says.
    This is instructions, not authority -- the same line the module docstring draws.

    ``None`` is honest rather than empty: the memory block, the bootstrap files and the history
    are assembled from a workspace and a session, so there is no text to show outside a turn. What
    one turn actually carried is on that turn, in the prompt manifest (#203).
    """
    if name == ADDENDUM_SECTION:
        return addendum
    template = _STATIC_SECTION_TEMPLATES.get(name)
    if template is None:
        return None
    # A templated section shows its **source**, because that is what a replacement must start
    # from: a rendered copy would bake one turn's paths into text the operator then edits.
    return _template_source(template) if name in _TEMPLATED_SECTIONS else render_template(template)


def webui_agent_prompt_payload(config: Config, name: str) -> dict[str, Any] | None:
    """``GET /api/webui/agents/prompt?agent=<name>`` -- or ``None`` when no such agent exists.

    ``None`` rather than an empty payload, so the dispatcher answers 404 for a name that is not in
    config. A panel that rendered "no sections" for a typo would be describing a prompt that does
    not exist.

    An **empty** name means the deployment's own agent -- ``agents.defaults`` -- which carries the
    same ``addendum`` and ``prompt_sections`` a named agent does (#265). That is a different thing
    from a name that is not in the roster, and collapsing the two would 404 the one agent every
    deployment has.
    """
    agent: Any
    if not name.strip():
        agent = config.agents.defaults
    else:
        agent = config.agents.named.get(name)
    if agent is None:
        return None
    addendum = agent.addendum or ""
    # Refuses, rather than filters, an override of a fixed section -- so a config that asks to
    # replace the tool contract surfaces here too and not only when a turn is assembled.
    overrides = declared_overrides(agent)
    sections: list[dict[str, Any]] = []
    for row in section_inventory(overrides=overrides, addendum=addendum):
        section = str(row["name"])
        tokens = _static_tokens(section, addendum) if row["static"] else None
        # What the deployment replaced it with, when it did; otherwise the platform's own text.
        # Both travel, so the panel can show the current text *and* offer the original back.
        override = overrides.get(section)
        platform = _platform_text(section, addendum)
        sections.append({
            **row,
            "tokens": tokens,
            "text": override if override is not None else platform,
            "platform_text": platform,
            # What a replacement has to keep, and what it costs to replace. Both empty for most
            # sections; a control that explains the cost is worth more than one that forbids.
            "placeholders": _section_placeholders(section),
            "warning": REPLACEMENT_WARNINGS.get(section, ""),
        })
    return {
        "agent": name,
        # So the panel can say whose prompt this is without inferring it from an empty string.
        "is_default_agent": not name.strip(),
        # `getattr`, because the deployment's own agent has no description: nothing delegates to
        # it, so there is no line explaining it to a peer.
        "description": getattr(agent, "description", ""),
        "sections": sections,
        # The text itself, because reading what an agent was told is the point of the panel. There
        # is no write path: an agent is edited in the config file a human reviews.
        "addendum": addendum,
        # Same caveat the prompt manifest carries: the numbers are our tokenizer's estimate, not
        # the provider's count, and the panel says which of the two it is showing.
        "measured": False,
    }


__all__ = ["webui_agent_prompt_payload"]
