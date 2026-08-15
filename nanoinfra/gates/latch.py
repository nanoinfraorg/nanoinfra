"""Terminal denials and the session latch -- nanoinfraorg/nanoinfra#15.

A denial that only fails the tool call invites a retry. The model then sends a slightly
different command until one command passes. That design is a brute-force oracle, and the human
who answers each prompt is the rate limiter. So a denial here is a terminal state and not an
error.

Four rules, and each rule closes one part of the hole:

1. A denial ends the action. ``TerminalDenial`` says so in the text, and the text carries no
   retry advice. ``nanoinfra/agent/runner.py`` appends "try a different approach" to tool
   failures, so ``is_terminal_denial`` exists for the runner to suppress that one hint.
2. A denial latches the capability class for the session. While the latch holds, the gate
   refuses the same class and asks nobody. A new prompt is the oracle, so no new prompt is the
   whole point.
3. Only an operator clears the latch. Time does not clear it, a new turn does not clear it, and
   no value a model supplies reaches the clearing code. The gate half (``DenialLatch``) has no
   clearing member at all, and the operator half (``LatchController``) needs the private state
   object that ``new_denial_latch`` hands to its caller once.
4. Every refusal under the latch reaches a recorder. A latched session that keeps trying is
   then visible as exactly that.

Two deliberate limits, because a silent limit is worse than a stated one:

- The marker is text as well as a type. Text survives a process boundary and ``str(exc)``,
  which the type does not. A tool that echoes model output could therefore fake the marker,
  and that buys the loss of one retry hint. It never buys an authorization, because the latch
  state lives here and never in the text.
- State stays in this process. Persistence would put the latch in a file, and the model holds
  filesystem tools. A restart drops the latch, and the record of rule 4 is what survives.

The module is a library. It reads no policy, opens no transport, and knows no sink: #8 owns the
decision that calls ``deny``, and #16 owns the record that ``record`` reaches.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from loguru import logger

from nanoinfra.agent.tools.base import ToolResult


class RestoredLatchState(Protocol):
    """What ``new_denial_latch`` needs from a restored state (#32).

    Structural on purpose. ``nanoinfra.gates.latch_restore`` imports this module, so importing
    its concrete type here would close a cycle. A protocol keeps the types real without one.
    """

    @property
    def latched(self) -> Mapping[tuple[str, str], float]: ...

    @property
    def refusals(self) -> Mapping[tuple[str, str], int]: ...


# The marker the runner keys on. It reads as a machine token so it cannot collide with prose,
# and it stays out of the sentence a human reads.
TERMINAL_DENIAL_MARKER = "[gate:denial:terminal]"

# The sentence that makes the state terminal for the model. Rule 1 forbids retry advice, so
# this text names the one path that exists: an operator, outside the conversation.
_TERMINAL_NOTE = (
    "This action is over. It will not run, and a changed command for the same purpose will "
    "not run either. Further {capability_class} actions in this session get a refusal with no "
    "prompt to anybody. Only an operator lifts that block, outside this conversation. Report "
    "the outcome and continue with work that needs no {capability_class} action."
)


class TerminalDenial(ToolResult):
    """A denied action, as the result the model reads.

    It subclasses ``ToolResult`` with ``is_error`` set, because ``is_tool_error_result`` in
    nanoinfra/agent/tools/registry.py keys on that flag. The runner has to enter its error
    branch to classify the result at all, and the classifier then strips the retry hint.

    ``capability_class`` rides along so a caller that only holds the result can still name what
    is latched.
    """

    capability_class: str

    def __new__(cls, message: str, *, capability_class: str) -> TerminalDenial:
        note = _TERMINAL_NOTE.format(capability_class=capability_class)
        content = f"{message}\n\n{note}\n{TERMINAL_DENIAL_MARKER}"
        obj = cast(TerminalDenial, super().__new__(cls, content, is_error=True))
        obj.capability_class = capability_class
        return obj

    def __reduce__(self) -> tuple[Callable[[str, str], TerminalDenial], tuple[str, str]]:
        """Copy and pickle by field, and never through ``__new__``.

        The runner deep-copies messages and a subagent pickles them. The default protocol for a
        ``str`` subclass calls ``__new__`` with the finished text, which would append the
        terminal note a second time and would drop ``is_error``.
        """
        return _restore_terminal_denial, (str(self), self.capability_class)


def _restore_terminal_denial(content: str, capability_class: str) -> TerminalDenial:
    """Rebuild a denial from its stored fields. Module level, because pickle needs a name."""
    obj = cast(TerminalDenial, ToolResult.__new__(TerminalDenial, content, is_error=True))
    obj.capability_class = capability_class
    return obj


def is_terminal_denial(result: object) -> bool:
    """True when *result* is a denial that must reach the model without a retry hint.

    The type check is the authority in one process. The text check covers the paths that keep
    the string and drop the type: ``str(exc)``, a serialized tool result, and #18's executor
    process.
    """
    if isinstance(result, TerminalDenial):
        return True
    if isinstance(result, str):
        return TERMINAL_DENIAL_MARKER in result
    return False


class LatchEventKind(StrEnum):
    """What happened. #16 records the value, so it is a value and not a log sentence."""

    DENIED = "denied"
    REFUSED = "refused"
    CLEARED = "cleared"


@dataclass(frozen=True, slots=True)
class LatchEvent:
    """One record. Rule 4 needs the refusals countable, so the count travels with the event.

    ``action_digest`` carries a digest and never the command. Resolved commands routinely embed
    secrets (``mysql -p...``), and callers pass ``command_digest`` from
    nanoinfra/agent/tools/capabilities.py.
    """

    kind: LatchEventKind
    session_id: str
    capability_class: str
    at: float
    tool: str | None = None
    turn_id: str | None = None
    actor: str | None = None
    reason: str | None = None
    action_digest: str | None = None
    refusal_count: int = 0

    def audit_fields(self) -> dict[str, str | float | int | None]:
        """The record as flat fields, in the shape #16 persists."""
        return {
            "kind": str(self.kind),
            "session_id": self.session_id,
            "capability_class": self.capability_class,
            "at": self.at,
            "tool": self.tool,
            "turn_id": self.turn_id,
            "actor": self.actor,
            "reason": self.reason,
            "action_digest": self.action_digest,
            "refusal_count": self.refusal_count,
        }


@dataclass(slots=True)
class _Entry:
    """One latched class. ``refusals`` counts the attempts that arrived after the denial."""

    reason: str
    at: float
    refusals: int = 0


@dataclass(slots=True)
class _LatchState:
    """The shared half. It holds data and applies no rule, so neither half can drift.

    The type is private, and rule 3 uses that: ``LatchController`` refuses anything else, so a
    caller that holds only strings from a tool call cannot mint the operator half.
    """

    clock: Callable[[], float]
    record: Callable[[LatchEvent], None] | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    entries: dict[tuple[str, str], _Entry] = field(default_factory=dict)

    def emit(self, event: LatchEvent) -> None:
        """Hand the event to the recorder, and survive a dead recorder.

        A refusal that a broken sink turns into an exception would become a pass at the call
        site, and a pass is the thing this module prevents. So the refusal wins and the failure
        gets logged.
        """
        if self.record is None:
            return
        try:
            self.record(event)
        except Exception as exc:
            # Broad on purpose. Any sink failure is a record problem, never a reason to pass.
            logger.error("gate latch record failed for {}: {}", event.session_id, exc)


class DenialLatch:
    """The half the gate holds. It denies, it refuses, and it cannot clear (rule 3).

    ``deny`` sets the latch as one step. A separate ``latch`` method would let a caller deny
    without latching, and a denial that does not latch is the retryable denial this item
    removes.
    """

    def __init__(self, state: _LatchState) -> None:
        """Take the shared state. ``new_denial_latch`` is the way to build a pair."""
        self._state = state

    def deny(
        self,
        *,
        session_id: str,
        capability_class: str,
        tool: str,
        reason: str,
        turn_id: str | None = None,
        actor: str | None = None,
        action_digest: str | None = None,
    ) -> TerminalDenial:
        """End this action, latch the class, and record the denial.

        ``reason`` is operator-side policy text that a human reads. Callers pass a digest for
        the action itself and never the resolved command.
        """
        state = self._state
        with state.lock:
            now = state.clock()
            state.entries[(session_id, capability_class)] = _Entry(reason=reason, at=now)
        state.emit(
            LatchEvent(
                kind=LatchEventKind.DENIED,
                session_id=session_id,
                capability_class=capability_class,
                at=now,
                tool=tool,
                turn_id=turn_id,
                actor=actor,
                reason=reason,
                action_digest=action_digest,
            )
        )
        return TerminalDenial(
            f"Denied by the capability gate: {reason}",
            capability_class=capability_class,
        )

    def refuse(
        self,
        *,
        session_id: str,
        capability_class: str,
        tool: str,
        turn_id: str | None = None,
        action_digest: str | None = None,
    ) -> TerminalDenial | None:
        """Answer a gated action while the latch holds, and ask nobody (rule 2).

        ``None`` means no latch, so the caller continues to its normal decision. A result means
        the caller returns it and must not prompt: the prompt is the oracle.
        """
        state = self._state
        key = (session_id, capability_class)
        with state.lock:
            entry = state.entries.get(key)
            if entry is None:
                return None
            entry.refusals += 1
            count = entry.refusals
            reason = entry.reason
            now = state.clock()
        state.emit(
            LatchEvent(
                kind=LatchEventKind.REFUSED,
                session_id=session_id,
                capability_class=capability_class,
                at=now,
                tool=tool,
                turn_id=turn_id,
                reason=reason,
                action_digest=action_digest,
                refusal_count=count,
            )
        )
        return TerminalDenial(
            f"Refused by the capability gate: a {capability_class} action was already denied in "
            "this session. Nobody was asked, because that denial stands.",
            capability_class=capability_class,
        )

    def is_latched(self, *, session_id: str, capability_class: str) -> bool:
        """Whether this class is latched for this session."""
        with self._state.lock:
            return (session_id, capability_class) in self._state.entries

    def latched_classes(self, session_id: str) -> frozenset[str]:
        """Every latched class for a session, so an operator sees what to clear."""
        with self._state.lock:
            return frozenset(
                capability_class
                for latched_session, capability_class in self._state.entries
                if latched_session == session_id
            )

    def refusal_count(self, *, session_id: str, capability_class: str) -> int:
        """How many gated actions arrived after the denial. Zero when nothing is latched."""
        with self._state.lock:
            entry = self._state.entries.get((session_id, capability_class))
            return 0 if entry is None else entry.refusals


class LatchController:
    """The half an operator surface holds. Holding it *is* the authenticated path.

    The controller does not decide who counts as an operator. ``gates.approvers`` in #7 owns
    that identity check, the same way ``ApprovalTokenStore`` leaves it to the gate. One module
    that both authenticates and clears would hide the decision.
    """

    def __init__(self, state: object) -> None:
        """Refuse anything that is not the private state (rule 3).

        A model supplies strings and JSON. This constructor makes those values fail at the
        door, so no argument at any tool boundary produces a controller.
        """
        if not isinstance(state, _LatchState):
            raise TypeError(
                "a latch controller needs the private state from new_denial_latch()"
            )
        self._state: _LatchState = state

    def clear(
        self,
        *,
        session_id: str,
        capability_class: str,
        actor: str,
        reason: str | None = None,
    ) -> bool:
        """Lift one latch. ``False`` when nothing was latched, so a typo does not read as done."""
        state = self._state
        with state.lock:
            entry = state.entries.pop((session_id, capability_class), None)
            now = state.clock()
        if entry is None:
            return False
        state.emit(
            LatchEvent(
                kind=LatchEventKind.CLEARED,
                session_id=session_id,
                capability_class=capability_class,
                at=now,
                actor=actor,
                reason=reason,
            )
        )
        return True

    def clear_session(
        self, *, session_id: str, actor: str, reason: str | None = None
    ) -> tuple[str, ...]:
        """Lift every latch on one session, and name what was lifted."""
        state = self._state
        with state.lock:
            classes = sorted(
                capability_class
                for latched_session, capability_class in state.entries
                if latched_session == session_id
            )
        cleared = [
            capability_class
            for capability_class in classes
            if self.clear(
                session_id=session_id,
                capability_class=capability_class,
                actor=actor,
                reason=reason,
            )
        ]
        return tuple(cleared)


def new_denial_latch(
    *,
    record: Callable[[LatchEvent], None] | None = None,
    clock: Callable[[], float] = time.time,
    restored: RestoredLatchState | None = None,
) -> tuple[DenialLatch, LatchController]:
    """Build the two halves, and split them at birth (rule 3).

    The caller keeps ``DenialLatch`` for the gate path and puts ``LatchController`` behind the
    operator surface. Nothing on the gate half reaches the controller, so no tool argument and
    no prompt text can clear a latch.

    ``clock`` stamps records for a human to read, so a wall clock is right here. It drives no
    expiry, because rule 3 says elapsed time clears nothing.

    ``restored`` seeds the state from the audit log at start (#32). A restart used to drop
    every latch, and the agent can cause a restart, so the deny-restart-retry loop bypassed
    this module. The value comes from ``nanoinfra.gates.latch_restore.restore_latches``. It is
    read by attribute rather than imported by type, because that module imports this one and a
    type import here would close a cycle.
    """
    state = _LatchState(clock=clock, record=record)
    if restored is not None:
        _seed(state, restored)
    return DenialLatch(state), LatchController(state)


def _seed(state: _LatchState, restored: RestoredLatchState) -> None:
    """Copy restored latches into a fresh state.

    A restored latch keeps its refusal count, so the banner in #28 does not reset to zero and
    hide a session that keeps trying. The reason text says the latch predates this process,
    because an operator who reads the banner needs to know that.
    """
    latched = dict(restored.latched)
    refusals = dict(restored.refusals)
    for key, at in latched.items():
        state.entries[key] = _Entry(
            reason="Denied before the last gateway restart, and restored from the audit log.",
            at=float(at),
            refusals=int(refusals.get(key, 0)),
        )


__all__ = [
    "TERMINAL_DENIAL_MARKER",
    "DenialLatch",
    "LatchController",
    "LatchEvent",
    "RestoredLatchState",
    "LatchEventKind",
    "TerminalDenial",
    "is_terminal_denial",
    "new_denial_latch",
]
