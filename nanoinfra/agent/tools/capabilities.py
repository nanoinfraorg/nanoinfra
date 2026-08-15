"""Capability classes for tools -- the vocabulary policy keys on.

Policy in nanoinfraorg/nanoinfra#7 and #8 keys on the class, never on a tool
name, so a new tool inherits a policy decision instead of an exemption. A tool
that declares no class resolves to ``mutate.remote``: the loader discovers
modules by ``pkgutil`` scan and by entry points, so an omission must cost the
most restrictive class rather than buy the most permissive one.

``mutate.inventory`` exists separately from ``mutate.local`` on purpose.
``UpdateServerTool`` (nanoinfra/agent/tools/servers.py) replaces a server's
``config`` and ``secretRef`` in full, so an inventory write changes what a later
``mutate.remote`` action against that name actually reaches. Classing it as a
plain local write would let one edit repoint a granted name at another address.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.agent.tools.context import (
    current_request_execution_context,
    current_request_session_key,
)

if TYPE_CHECKING:
    from nanoinfra.agent.tools.base import Tool

READ = "read"
MUTATE_LOCAL = "mutate.local"
MUTATE_INVENTORY = "mutate.inventory"
MUTATE_REMOTE = "mutate.remote"
CREDENTIAL_ACCESS = "credential.access"

CAPABILITY_CLASSES = frozenset({READ, MUTATE_LOCAL, MUTATE_INVENTORY, MUTATE_REMOTE, CREDENTIAL_ACCESS})

# The class an undeclared tool gets. Fail closed.
FAIL_CLOSED_CLASS = MUTATE_REMOTE


def capability_class_of(tool: Tool | type[Tool] | Any) -> str:
    """Return the declared capability class, or ``mutate.remote`` when none is declared."""
    declared = getattr(tool, "capability_class", None)
    if declared in CAPABILITY_CLASSES:
        return str(declared)
    return FAIL_CLOSED_CLASS


# Observations sit above INFO so they show at the default threshold -- an operator
# sizing M2's breakage should not have to raise verbosity to see what M2 would refuse.
OBSERVATION_LEVEL = "GATE"
try:  # pragma: no cover -- idempotent registration across reimports
    logger.level(OBSERVATION_LEVEL)
except ValueError:
    logger.level(OBSERVATION_LEVEL, no=25, color="<yellow>")


def command_digest(command: str) -> str:
    """Digest a resolved command for the record.

    Resolved commands routinely embed secrets (``mysql -p...``), so the record carries a
    digest and never the text. #16 makes full text an explicit opt-in.
    """
    return "sha256:" + hashlib.sha256(command.encode()).hexdigest()


def record_observation(
    *, capability_class: str, decision: str, tool: str, **fields: Any
) -> None:
    """Log the decision the gate *would* make. M1 enforces nothing.

    ``decision`` is ``preview`` when the call asked for a preview, and ``would_gate``
    when the call asked for execution. The record shape follows #16, minus ``scope``
    (#4), which does not exist yet. Callers pass only values that are safe to persist:
    ids, names, digests, never a credential value.

    ``execution_context`` (#5) comes from the current request the same way ``session_id``
    does. It reads unattended when no request is bound. So a missing value never looks
    like a person was present.
    """
    record: dict[str, Any] = {
        "capability_class": capability_class,
        "decision": decision,
        "tool": tool,
        "session_id": current_request_session_key(),
        "execution_context": current_request_execution_context(),
        **fields,
    }
    logger.bind(gate_observation=record).log(
        OBSERVATION_LEVEL, "would gate: {} {} via {}", decision, capability_class, tool
    )


__all__ = [
    "CAPABILITY_CLASSES",
    "CREDENTIAL_ACCESS",
    "FAIL_CLOSED_CLASS",
    "MUTATE_INVENTORY",
    "MUTATE_LOCAL",
    "MUTATE_REMOTE",
    "OBSERVATION_LEVEL",
    "READ",
    "capability_class_of",
    "command_digest",
    "record_observation",
]
