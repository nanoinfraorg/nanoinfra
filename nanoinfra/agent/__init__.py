"""Agent core module.

Every name here is a re-export, and each one is resolved on first access rather
than at import time (nanoinfraorg/nanoinfra#57).

The eager version of this file closed a cycle. Importing any submodule of a
package runs the package ``__init__``, so ``import nanoinfra.agent.tools.web``
pulled in the agent context, which pulls in the agent memory, which imports
``nanoinfra.session.manager``. A process whose first import was the session
manager therefore reached this file mid-cycle.

``nanoinfra/config/schema.py`` imports a tool config class from each tool module
to resolve its own field types, and it caught the resulting ``ImportError`` and
carried on. So the root ``Config`` class stayed unusable for the whole life of
that process.

A lazy re-export costs one dictionary lookup per name, once. It also makes an
import of one tool module cost one tool module.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanoinfra.agent.context import ContextBuilder
    from nanoinfra.agent.hook import (
        AgentHook,
        AgentHookContext,
        AgentRunHookContext,
        AgentTurnHookContext,
        AgentTurnHookFactory,
        CompositeHook,
    )
    from nanoinfra.agent.loop import AgentLoop
    from nanoinfra.agent.memory import MemoryStore
    from nanoinfra.agent.skills import SkillsLoader
    from nanoinfra.agent.subagent import SubagentManager

# Which module holds each name. A name that is absent from this map is absent
# from the package, and ``__getattr__`` says so with the same wording Python
# uses for a missing attribute.
_EXPORTS = {
    "AgentHook": "nanoinfra.agent.hook",
    "AgentHookContext": "nanoinfra.agent.hook",
    "AgentLoop": "nanoinfra.agent.loop",
    "AgentRunHookContext": "nanoinfra.agent.hook",
    "AgentTurnHookContext": "nanoinfra.agent.hook",
    "AgentTurnHookFactory": "nanoinfra.agent.hook",
    "CompositeHook": "nanoinfra.agent.hook",
    "ContextBuilder": "nanoinfra.agent.context",
    "MemoryStore": "nanoinfra.agent.memory",
    "SkillsLoader": "nanoinfra.agent.skills",
    "SubagentManager": "nanoinfra.agent.subagent",
}

__all__ = [
    "AgentHook",
    "AgentHookContext",
    "AgentLoop",
    "AgentRunHookContext",
    "AgentTurnHookContext",
    "AgentTurnHookFactory",
    "CompositeHook",
    "ContextBuilder",
    "MemoryStore",
    "SkillsLoader",
    "SubagentManager",
]


def __getattr__(name: str) -> Any:
    """Import the module that holds *name*, and remember the answer.

    The result lands in ``globals()``, so a second access reads the module
    namespace and never reaches this function.
    """
    module_path = _EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_path), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(_EXPORTS)
