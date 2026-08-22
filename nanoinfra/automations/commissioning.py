"""Commissioning: rehearse an automation once, and act on nothing -- #182.

An automation is created from prose, so **the commands are not in its text**: the model composes
them when the automation runs. Nothing can enumerate them by reading the record, which is why the
only instrument that answers "what permission will this need?" is a run.

A commissioning run is that run, with every gated tool forced to preview. It resolves and it
evaluates; it never acts. Two properties follow, and both are why this is safe to do without
asking anybody first:

- A preview reaches no host and resolves no credential, so the rehearsal needs no permission of
  its own. It cannot be a way to act before a person has agreed to anything.
- Since #179 a preview carries the gate's answer, so the rehearsal learns the real decision for
  the real context, including the grant that would permit each action.

The collector is the flag. A tool asks whether one is bound rather than reading a separate
boolean, so a turn cannot be half-commissioned: the same object that says "force preview" is the
one that receives what the preview found.

Scope of the flag is a context variable, so it covers exactly the turn that set it and no
concurrent turn. It never reaches a store, and nothing a model can say sets it.
"""

from __future__ import annotations

import uuid
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PreviewedAction:
    """One action a commissioning run previewed, and what a real run of it would meet.

    ``hosts`` and ``command`` are the two halves of the standing grant that would permit it, in
    the form config takes: hosts as the resolver returned them, never the label the caller typed,
    and the command exactly as it would run.
    """

    tool: str
    capability_class: str
    outcome: str | None
    reason: str
    grant_id: str | None = None
    scope: str | None = None
    hosts: tuple[str, ...] = ()
    command: str = ""
    credential_outcome: str | None = None
    credential_reason: str = ""

    @property
    def permitted(self) -> bool:
        """True when a real run would be allowed, credential included."""
        if self.outcome != "allow":
            return False
        return self.credential_outcome in (None, "allow")

    @property
    def grantable(self) -> bool:
        """Whether a standing grant could permit this action at all.

        A grant carries no capability class and can never permit an inventory write (#23), and no
        policy permits `all` scope. Both cases are refusals to propose rather than grants to
        write, so the caller reports them instead of offering a fix that does not exist.
        """
        if self.capability_class != "mutate.remote":
            return False
        if self.scope == "all" or not self.hosts or not self.command:
            return False
        return True

    def as_grant(self, *, grant_id: str) -> dict[str, Any]:
        """The grant that would permit this action, as `gates.standingGrants` takes it."""
        return {
            "id": grant_id,
            "contexts": ["unattended"],
            "hosts": list(self.hosts),
            "commands": [self.command],
        }


@dataclass
class CommissioningCollector:
    """What one commissioning run found, in the order it found it.

    Deduplicated on the resolved action rather than on the call, so a turn that previews the same
    command against the same hosts twice proposes one grant. Two different commands against the
    same host stay two entries: the grant list matches exact strings, so each one needs its own.
    """

    actions: list[PreviewedAction] = field(default_factory=list[PreviewedAction])

    def record(self, action: PreviewedAction) -> None:
        key = (action.capability_class, action.command, action.hosts)
        for existing in self.actions:
            if (existing.capability_class, existing.command, existing.hosts) == key:
                return
        self.actions.append(action)

    @property
    def refused(self) -> list[PreviewedAction]:
        """The actions a scheduled run would not be allowed to take."""
        return [action for action in self.actions if not action.permitted]

    @property
    def permitted(self) -> bool:
        """True when every action this run previewed would be allowed unattended.

        An empty run is permitted: an automation that touches no gated capability needs no grant,
        and reporting it as blocked would teach an operator to ignore the report.
        """
        return not self.refused


_CURRENT: ContextVar[CommissioningCollector | None] = ContextVar(
    "nanoinfra_commissioning", default=None
)


@contextmanager
def commissioning_run() -> Generator[CommissioningCollector]:
    """Bind a collector for the duration of one turn, and force every gated tool to preview."""
    collector = CommissioningCollector()
    token = _CURRENT.set(collector)
    try:
        yield collector
    finally:
        _CURRENT.reset(token)


def current_commissioning() -> CommissioningCollector | None:
    """The collector bound to this turn, or None outside a commissioning run."""
    return _CURRENT.get()


def forces_preview() -> bool:
    """Whether a gated tool must preview rather than act, whatever its arguments say."""
    return _CURRENT.get() is not None


#: Names the collector a commissioning turn belongs to. The turn crosses the message bus and is
#: processed in another task, so a context variable set by the submitter never reaches the tools.
#: The id travels in metadata and the collector stays in this process: a collector on the wire
#: would be a store, and the automation's own turn must not be able to reach what grades it.
COMMISSIONING_TURN_META = "_commissioning_turn"

_BY_TURN: dict[str, CommissioningCollector] = {}


@contextmanager
def commissioning_turn() -> Generator[tuple[str, CommissioningCollector]]:
    """Register a collector for one turn, and hand back the id that names it.

    The registration lives exactly as long as this block, so a turn that never arrives leaves
    nothing behind.
    """
    collector = CommissioningCollector()
    turn_id = uuid.uuid4().hex
    _BY_TURN[turn_id] = collector
    try:
        yield turn_id, collector
    finally:
        _BY_TURN.pop(turn_id, None)


@contextmanager
def bind_commissioning(metadata: Mapping[str, Any] | None) -> Generator[None]:
    """Bind the collector this turn's metadata names, if any, for the turn's duration.

    Entered by the turn processor, so the binding sits in the same task as the tools it governs.
    Metadata that names no collector -- an ordinary turn, or a stale id -- binds nothing, which
    is what keeps a replayed message from turning into a rehearsal.
    """
    turn_id = (metadata or {}).get(COMMISSIONING_TURN_META)
    collector = _BY_TURN.get(turn_id) if isinstance(turn_id, str) else None
    if collector is None:
        yield
        return
    token = _CURRENT.set(collector)
    try:
        yield
    finally:
        _CURRENT.reset(token)


COMMISSIONING_PREVIEW_NOTE = (
    "This is a commissioning run, so the action was previewed and nothing ran. The arguments "
    "given were kept; only execution was withheld. Continue the turn as if it had run, and "
    "report what it would have done."
)


__all__ = [
    "COMMISSIONING_PREVIEW_NOTE",
    "COMMISSIONING_TURN_META",
    "CommissioningCollector",
    "PreviewedAction",
    "bind_commissioning",
    "commissioning_run",
    "commissioning_turn",
    "current_commissioning",
    "forces_preview",
]
