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

from typing import Any

from nanoinfra.agent.prompt_manifest import estimate_tokens
from nanoinfra.agent.prompt_sections import (
    ADDENDUM_SECTION,
    declared_overrides,
    section_inventory,
)
from nanoinfra.config.schema import Config
from nanoinfra.utils.prompt_templates import render_template

#: Which template a deployment-static section is measured from. Rendered on demand rather than at
#: import: this is a settings panel's read, not a hot path, and a module-level render would cost
#: every process that imports the gateway.
_STATIC_SECTION_TEMPLATES = {
    "Safety notes": "agent/safety_notes.md",
    "Tool usage notes": "agent/tool_contract.md",
}


def _static_tokens(name: str, addendum: str) -> int | None:
    """The token cost of a section that costs the same on every turn, or ``None``."""
    if name == ADDENDUM_SECTION:
        return estimate_tokens(addendum.strip()) if addendum.strip() else 0
    template = _STATIC_SECTION_TEMPLATES.get(name)
    if template is None:
        return None
    return estimate_tokens(render_template(template))


def webui_agent_prompt_payload(config: Config, name: str) -> dict[str, Any] | None:
    """``GET /api/webui/agents/prompt?agent=<name>`` -- or ``None`` when no such agent exists.

    ``None`` rather than an empty payload, so the dispatcher answers 404 for a name that is not in
    config. A panel that rendered "no sections" for a typo would be describing a prompt that does
    not exist.
    """
    agent = config.agents.named.get(name)
    if agent is None:
        return None
    addendum = agent.addendum or ""
    # Refuses, rather than filters, an override of a fixed section -- so a config that asks to
    # replace the tool contract surfaces here too and not only when a turn is assembled.
    overrides = declared_overrides(agent)
    sections: list[dict[str, Any]] = []
    for row in section_inventory(overrides=overrides, addendum=addendum):
        tokens = _static_tokens(str(row["name"]), addendum) if row["static"] else None
        sections.append({**row, "tokens": tokens})
    return {
        "agent": name,
        "description": agent.description,
        "sections": sections,
        # The text itself, because reading what an agent was told is the point of the panel. There
        # is no write path: an agent is edited in the config file a human reviews.
        "addendum": addendum,
        # Same caveat the prompt manifest carries: the numbers are our tokenizer's estimate, not
        # the provider's count, and the panel says which of the two it is showing.
        "measured": False,
    }


__all__ = ["webui_agent_prompt_payload"]
