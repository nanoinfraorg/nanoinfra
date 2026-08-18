"""Read-only Agent Plugins state for the WebUI Apps panel.

There is no mutating counterpart to this module, and that is deliberate. Activation is declared in
``tools.agentPlugins`` and reconciled by the executor (nanoinfraorg/nanoinfra#141), so an endpoint
that toggled a package would be a second authority contradicting the first. The panel displays
state and says where the state comes from.

See nanoinfraorg/nanoinfra#142.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

# What the panel is allowed to tell the operator about where authority lives. Naming the config key
# in the payload keeps the UI copy and the actual mechanism from drifting apart.
AUTHORITY = "tools.agentPlugins"


def agent_plugins_payload() -> dict[str, Any]:
    """Return every installed Agent Plugin with its components and activation state.

    Three states, because "not active" hides a distinction an operator needs:

    - ``active``    listed in config and its content still matches what was activated
    - ``modified``  listed in config but the package changed, so it deactivated itself
    - ``inactive``  nobody listed it

    ``modified`` is the one worth surfacing. It looks like "off" and it means "review me".
    """
    from nanoinfra.agent.plugins import (
        _discover_plugin_skills,  # pyright: ignore[reportPrivateUsage]
        discover_agent_plugins,
        workspace_from_config,
    )
    from nanoinfra.config.loader import load_config

    config = load_config()
    workspace = workspace_from_config(config)
    declared = list(getattr(config.tools, "agent_plugins", []) or [])
    declared_set = {name.strip() for name in declared if name.strip()}

    plugins: list[dict[str, Any]] = []
    try:
        discovered = discover_agent_plugins(workspace)
    except (OSError, RuntimeError) as exc:
        # A panel that cannot read the tree says so rather than claiming nothing is installed.
        logger.warning("Could not read Agent Plugins: {}", exc)
        return {
            "plugins": [],
            "unknown": sorted(declared_set),
            "editable": False,
            "authority": AUTHORITY,
            "error": "Agent Plugins could not be read.",
        }

    for plugin in discovered:
        is_declared = plugin.name in declared_set
        if plugin.enabled:
            state = "active"
        elif is_declared:
            state = "modified"
        else:
            state = "inactive"
        plugins.append({
            "name": plugin.name,
            "display_name": plugin.display_name,
            "description": plugin.description,
            "repository": plugin.repository,
            "version": plugin.version,
            "category": plugin.category,
            "accent_color": plugin.accent_color,
            "logo": plugin.logo,
            "permissions": list(plugin.permissions),
            # Skill names, not paths. The package root is operator-side detail.
            "skills": [name for name, _path in _discover_plugin_skills(plugin.name, plugin.root)],
            "mcp_servers": list(plugin.mcp_servers),
            "state": state,
            "declared": is_declared,
        })

    installed_names = {plugin.name for plugin in discovered}
    return {
        "plugins": plugins,
        "unknown": sorted(declared_set - installed_names),
        "editable": False,
        "authority": AUTHORITY,
    }
