"""The named-agent roster, as the WebUI reads it (#255).

Pattern: ``nanoinfra/webui/diagrams_api.py`` -- a small pure function the gateway HTTP dispatcher
(``ws_http.py``) calls into, gated by ``check_api_token`` at the call site rather than in here.

The roster is authority: which agents exist, and what each may reach, is decided in a config file
that a human reviews. So this answers **who exists and what they are for**, and deliberately not
what any of them may reach -- no model preset, no tool group, no skill, no connector, no delegate
list. The composer needs a name to offer and a line to explain it; a browser that could enumerate
an agent's bindings would be reading the authorization model out of a mention menu.

``settings_api.settings_payload`` carries the same roster with *counts* of those bindings, which is
what the Agents page shows. This is the smaller read the composer makes on every thread.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.config.schema import Config


def webui_named_agents_payload(config: Config) -> dict[str, Any]:
    """``GET /api/webui/agents/named`` -- the mentionable agents, in config order.

    Empty for every deployment that names no agent, which is the shape that makes the ``agent:``
    mention prefix disappear rather than offer an empty menu.
    """
    return {
        "agents": [
            {"name": name, "description": agent.description}
            for name, agent in config.agents.named.items()
        ]
    }
