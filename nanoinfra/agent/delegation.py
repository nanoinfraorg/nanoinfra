"""What a delegated turn is, and what it may reach (#250, #251).

Its own module because three things need it and none of them should import the other two: the
delegation tool constructs a binding, ``SubagentManager`` runs one, and the gate layer reads the
actor rule off it.

The invariant the whole design rests on, stated once so it can be tested:

    A delegated turn never holds more authority than the turn that spawned it.

That is what makes one level more than a simplification. With chains, the invariant needs a
transitive check at every hop, and an audit reader has to reconstruct the path to learn who
authorised what. With one level the check is local and the record is two names.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class DelegateBinding:
    """Who a delegated turn runs as, and the bindings it was declared with.

    Carried rather than derived, because the peer's turn runs in another task: by the time it
    starts, the config lookup that authorised it is over and the answer has to travel with it.
    """

    #: The peer that acts. Its own name, not the manager's -- see the actor rule below.
    name: str
    #: The agent that asked. Present on every binding, which is what makes the turn refuse to
    #: delegate again.
    delegated_by: str
    #: The human whose turn this ultimately serves, when there is one. ``None`` on an unattended
    #: chain, where a standing grant is the only thing that can authorise the peer's actions.
    actor: str | None = None
    #: The `tools.groups` the peer may reach. Empty means every group, which is the reading a
    #: single-agent deployment already has.
    tool_groups: tuple[str, ...] = ()
    #: Skills loaded in full for the peer. Empty means the catalogue is summarised, as today.
    skills: tuple[str, ...] = ()
    #: Appended after the platform's own prompt sections. It specialises the peer and cannot
    #: replace the tool contract or the safety notes.
    addendum: str = ""
    #: What the manager was allowed to do. The peer is capped by it, never widened past it.
    #:
    #: **Not populated by the delegation tool.** It is here because the ceiling belongs on the
    #: binding rather than in a second lookup at the gate, and it is empty because deciding what
    #: "allowed" means for a turn is #251's question, not this module's. An empty set means
    #: *not computed*, never *nothing allowed* -- a reader that treats it as a deny-list would
    #: refuse every delegation.
    inherited_capabilities: frozenset[str] = field(default_factory=frozenset)

    def audit_chain(self) -> str:
        """The record a reader should not need a second file to understand."""
        if self.actor:
            return f"{self.actor} -> {self.delegated_by} -> {self.name}"
        return f"{self.delegated_by} -> {self.name}"


class DelegatedAnswer(str):
    """A peer's answer, carrying what the peer's own turn cost.

    A ``str`` subclass because the tool contract is a string and every existing consumer must keep
    reading it as one. The cost rides alongside rather than inside: a delegated turn is its own
    turn, so folding its tokens into the asking agent's usage would print one turn's cost twice.

    The attribute does not survive JSON -- the value serialises as a plain string -- so whatever
    builds the wire payload has to copy it out deliberately. That is
    ``utils/progress_events.build_tool_event_finish_payloads``.
    """

    __slots__ = ("usage",)

    usage: dict[str, object] | None

    def __new__(cls, answer: str, usage: dict[str, object] | None = None) -> "DelegatedAnswer":
        value = super().__new__(cls, answer)
        value.usage = usage
        return value


def refuse_second_level(binding: DelegateBinding | None) -> str | None:
    """The reason a delegated turn may not delegate, or ``None`` when it may.

    A message rather than an exception, because the caller is a model and a refusal it can read
    is one it can act on: the peer reports back and the manager delegates the next step itself.
    """
    if binding is None:
        return None
    return (
        f"Delegation is one level deep. This turn is already {binding.name}, acting for "
        f"{binding.delegated_by}. Report what you found and let {binding.delegated_by} decide "
        "who does the next step."
    )


def allowed_delegates(
    agent: str | None,
    roster: Mapping[str, object],
) -> tuple[str, ...]:
    """The peers *agent* may reach, read from config at the moment of the call.

    The roster is the authorization (see the config-is-authority rule), so this is deliberately a
    fresh read rather than something captured when the tool was registered: an agent removed from
    a roster stops being reachable on the next turn, not on the next restart.
    """
    if not agent:
        return ()
    entry = roster.get(agent)
    if entry is None:
        return ()
    return tuple(getattr(entry, "delegates", ()) or ())


def acting_capabilities(
    tool_names: Iterable[str],
    allowed_groups: Iterable[str],
    membership: Mapping[str, Iterable[str]],
    class_of: Callable[[str], str],
) -> frozenset[str]:
    """The capability classes the turn that is delegating can actually reach.

    This is the *ceiling* a peer inherits: a manager that can only read must not be able to reach
    a remote mutation by asking a peer to do it. It is computed from the acting turn's own
    registry rather than from config, because the registry is what the turn can actually call --
    a tool absent for want of a collaborator is not authority the manager holds.

    ``class_of`` is injected so this stays a pure function over names: the capability vocabulary
    lives in ``agent/tools/capabilities.py`` and the registry holds the instances, and neither
    belongs in a rule this small.
    """
    reachable = tools_for_groups(tool_names, allowed_groups, membership)
    return frozenset(class_of(name) for name in reachable)


def tools_for_groups(
    tool_names: Iterable[str],
    allowed_groups: Iterable[str],
    membership: Mapping[str, Iterable[str]],
) -> frozenset[str]:
    """The tools a peer keeps, given the groups it was declared with.

    Three rules, and the second is the one worth writing down:

    1. An empty ``allowed_groups`` keeps everything. A two-line agent has to be meaningful.
    2. A tool in **no** group is kept. Groups cover surfaces -- servers, diagrams -- not the whole
       tool set, so treating "ungrouped" as "denied" would leave a peer unable to read a file.
       What an agent may do to the filesystem is `tools.file`, which it already inherits.
    3. A tool in one or more groups is kept only if one of them was declared.
    """
    allowed = frozenset(allowed_groups)
    if not allowed:
        return frozenset(tool_names)
    kept: set[str] = set()
    for name in tool_names:
        groups = frozenset(membership.get(name, ()))
        if not groups or groups & allowed:
            kept.add(name)
    return frozenset(kept)
