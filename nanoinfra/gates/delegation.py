"""Who signs a delegated action -- nanoinfraorg/nanoinfra#251.

The gate does not ask whether a tool is dangerous. It asks **may this actor do this now**, and
delegation puts that question in doubt: a manager that may only read files can cause a hands-on
peer to act. Three answers exist, and only one survives.

- *The peer inherits the manager's actor.* Wrong, and the failure is silent: an approval a human
  gave to a read-only manager becomes an approval for whatever its peer does next. That is
  authority laundering with extra steps.
- *The peer runs as itself, with no human actor.* Never wrong, and restrictive: this model
  already names a turn with nobody behind it -- unattended, whose only allow path is a standing
  grant.
- *The delegated action carries the originating human as its actor*, with the delegation chain
  beside it. An approval routes back to the person who asked, and the prompt they answer names
  the peer that will act rather than the manager that asked for it.

This module holds the third, falling back to the second when there is no human. Both rules below
live here, and not in the tool that asks: a tool that checks its own ceiling is a ceiling the
model can talk its way past.

**The actor rule.** A delegated turn runs under the ``subagent`` execution context, because
nobody watches a subagent. A delegation that names a human is a different fact: the person who
asked is present, and an approval can reach them. So the context the gate decides under is
``interactive`` when the chain names a human, and unchanged when it does not. That is the only
direction the invariant allows, because the human on the chain is the human the *spawning* turn
authenticated -- ``nanoinfra/agent/tools/delegate.py`` leaves the name unset on an unattended
turn, so a manager started by cron delegates as unattended and a standing grant is the only
thing that lets its peer act.

**The ceiling**, which is the invariant stated once so it can be tested:

    A delegated turn never holds more authority than the turn that spawned it.

An action whose capability class sits outside the set the spawning turn held refuses, whatever
the policy matrix says for the context. That is what makes one level of delegation more than a
simplification: the check is local, and the record is two names. With chains it would need a
transitive check at every hop, and an audit reader would have to rebuild the path to learn who
authorised what.

**Both agent names are the agent's assertion about itself**, exactly as ``origin_path`` and
``origin_actor`` are (see ``nanoinfra/gates/executor/protocol.py``). A compromised agent can
claim any pair, and it can already claim ``interactive`` on the frame, so the actor rule hands it
nothing it did not have. One thing it must not gain: prose on an operator's screen. The approvals
inbox renders these two values, and no field on that screen is model-authored. So a name is
normalised here -- bounded, and matched against the pattern config accepts for an agent name --
and anything else becomes no name at all. Absent attribution renders nothing, and never a guess.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from nanoinfra.agent.tools.capabilities import CAPABILITY_CLASSES
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    EXECUTION_CONTEXT_SUBAGENT,
)

# The pattern ``agents.named`` accepts (``nanoinfra/config/schema.py``). A name the gate reads has
# to be a name config could have declared. The schema refuses anything else at load, and this is
# the second check -- on the value that arrived from the process the model steers.
_AGENT_NAME = re.compile(r"^[\w.-]+$", re.UNICODE)

# A bound, because this value reaches an operator's screen. Far above any name an operator would
# type, and far below a paragraph.
MAX_AGENT_NAME_CHARS = 64

#: The capability classes the spawning turn held, for the turn it spawned.
#:
#: Its own context variable rather than a field on ``RequestContext``, for the reason
#: ``current_workspace_scope`` is one: it is an authority fact that the runner binds around a
#: delegated turn and the gate layer reads, and the two sides share no call frame. The default is
#: the empty set, which reads as "the spawning turn declared no ceiling" -- see
#: :meth:`Delegation.ceiling_refusal` for why that is not "deny everything".
_INHERITED_CAPABILITIES: ContextVar[frozenset[str]] = ContextVar(
    "nanoinfra_gate_inherited_capabilities",
    default=frozenset(),
)


def agent_name(value: str | None) -> str:
    """The agent name a record and an approval prompt may show, or ``""`` for no name at all.

    Blank, over-long, and anything outside the config name pattern all answer ``""``. A field
    that reaches a human must not be able to carry a sentence, and a value this function cannot
    vouch for is better rendered as nothing than as a guess.
    """
    named = (value or "").strip()
    if not named or len(named) > MAX_AGENT_NAME_CHARS or _AGENT_NAME.match(named) is None:
        return ""
    return named


def capability_ceiling(classes: Iterable[str] | None) -> frozenset[str]:
    """The declared ceiling, keeping the class names this deployment models.

    An element outside the vocabulary is dropped rather than refused, because dropping narrows
    the ceiling and refusing would widen the blast radius of a typo into a broken deployment.
    Narrowing is the fail-closed direction here.
    """
    if not classes:
        return frozenset()
    return frozenset(name for name in classes if name in CAPABILITY_CLASSES)


def bind_inherited_capabilities(classes: Iterable[str] | None):
    """Declare the ceiling for the turn inside this block. Returns a context manager.

    The runner that starts a delegated turn wraps it in this, the way it already wraps it in a
    workspace scope. Nothing inside the block can widen the value, because the value is read and
    never written by the gate layer.
    """

    @contextmanager
    def _bound():
        token = _INHERITED_CAPABILITIES.set(capability_ceiling(classes))
        try:
            yield
        finally:
            _INHERITED_CAPABILITIES.reset(token)

    return _bound()


def current_inherited_capabilities() -> frozenset[str]:
    """The ceiling the spawning turn declared, or the empty set when it declared none."""
    return _INHERITED_CAPABILITIES.get()


class DelegatedFrame(Protocol):
    """What the gate reads off one request to learn who is acting.

    A protocol rather than an import of the wire types, so this module stays readable by the
    agent side too. Read-only members, because every frame here is a frozen dataclass.
    """

    @property
    def origin_actor(self) -> str | None: ...

    @property
    def acting_agent(self) -> str | None: ...

    @property
    def delegated_by(self) -> str | None: ...

    @property
    def inherited_capabilities(self) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class Delegation:
    """The two agents and the human behind one action, as the gate reads them.

    Every field defaults to blank, which is the shape of every deployment that does not delegate:
    no agent, no chain, and neither rule below does anything.
    """

    #: The peer that acts. Its own name, never the manager's.
    acting_agent: str = ""
    #: The agent that asked. Two names are what make this a delegation.
    delegated_by: str = ""
    #: The human the spawning turn authenticated, or ``""`` on an unattended chain.
    origin_actor: str = ""
    #: What the spawning turn was allowed to do. Empty means it declared no ceiling.
    inherited_capabilities: frozenset[str] = frozenset()

    @property
    def is_delegated(self) -> bool:
        """True when two agents named themselves. One name alone is not a delegation."""
        return bool(self.acting_agent and self.delegated_by)

    @property
    def has_human(self) -> bool:
        """True when a person is behind this chain, and an approval can route back to them."""
        return bool(self.origin_actor)

    def chain(self) -> str | None:
        """``"alberto -> manager -> sre-prod"``, or ``None`` when no agent is named.

        The line exists so a reader can answer "who authorised this" without opening a second
        file. ``None`` for every deployment that names no agent, which is every deployment today:
        an empty string in the record would read as a chain nobody can parse.
        """
        if not self.acting_agent:
            return None
        named = [
            name for name in (self.origin_actor, self.delegated_by, self.acting_agent) if name
        ]
        return " -> ".join(named)

    def effective_execution_context(self, declared: str) -> str:
        """The context the gate decides under, given the one the frame declared.

        Idempotent, so a caller that applies it twice gets the same answer and no caller has to
        know whether another one already did.

        Only a delegated turn is affected, and only upward from ``subagent``: an automation stays
        an automation, and a plain subagent -- a child of one agent rather than a peer -- keeps
        the unattended reading it has always had.
        """
        if declared != EXECUTION_CONTEXT_SUBAGENT:
            return declared
        if not self.is_delegated or not self.has_human:
            return declared
        return EXECUTION_CONTEXT_INTERACTIVE

    def ceiling_refusal(self, capability_class: str) -> str | None:
        """Why this action exceeds the spawning turn's authority, or ``None`` when it does not.

        An empty ``inherited_capabilities`` binds nothing. It reads as "the spawning turn
        declared no ceiling", which is the state of a deployment that never restricted its
        manager, and reading it as "deny everything" would refuse a peer the right to read a
        file. The ceiling is a *narrowing* of what the context already permits, so an absent one
        leaves the matrix in charge and the matrix already fails closed.
        """
        if not self.is_delegated or not self.inherited_capabilities:
            return None
        if capability_class in self.inherited_capabilities:
            return None
        allowed = ", ".join(sorted(self.inherited_capabilities))
        return (
            f"Refusing {capability_class} for `{self.acting_agent}`: `{self.delegated_by}` "
            f"delegated this task holding {allowed}, and a delegated turn never holds more "
            "authority than the turn that spawned it. Widen the delegating agent, or let it do "
            "this step itself."
        )


def delegation_of(frame: DelegatedFrame) -> Delegation:
    """Read the delegation one request declares, normalised.

    Every value here came from the process the model steers, so every value is normalised before
    the gate compares it or an operator reads it.
    """
    return Delegation(
        acting_agent=agent_name(frame.acting_agent),
        delegated_by=agent_name(frame.delegated_by),
        origin_actor=(frame.origin_actor or "").strip(),
        inherited_capabilities=capability_ceiling(frame.inherited_capabilities),
    )


__all__ = [
    "MAX_AGENT_NAME_CHARS",
    "Delegation",
    "DelegatedFrame",
    "agent_name",
    "bind_inherited_capabilities",
    "capability_ceiling",
    "current_inherited_capabilities",
    "delegation_of",
]
