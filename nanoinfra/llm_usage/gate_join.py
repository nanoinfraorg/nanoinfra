"""The join key the gate log never had (#233).

`gates/gate-YYYY-MM-DD.jsonl` has held every decision since #16, and the tool-call row in #232
has held everything except the decision, because the two records shared no key. This module is
that key, and it is a context variable rather than a column: the gate decides deep inside
``tool.execute``, and the row is written by the seam that called it.

A **box** is installed per tool call rather than a bare value, for two reasons. A value set inside
a call would still be set when the next call in the same task started, and a sequential batch would
then inherit the previous action's decision. And a decision taken on a worker thread --
``asyncio.to_thread`` hands the callable a *copy* of the context -- can never be seen by the caller
if what it wrote was a variable; a box is one object that both sides hold.

Nothing here fabricates a decision. A deployment with no gate configured writes no audit record,
so no note is taken, and the row's decision column stays **empty**. An empty column says the gate
did not answer. An ``allow`` would say it did.

The reach of the note is the reach of the process. A refusal of a remote action is recorded on the
agent side -- the tool re-decides it to make it terminal -- so it joins. An **allow** is recorded in
the executor, and `ExecuteResponse` carries no decision field, so it does not: #261 holds that gap
and the test that pins it.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

#: The record kinds in `gates/audit.py` that are **not** decisions: `completion` is written when
#: an action ends (#46) and `grant_written` when an answer produced a standing grant (#219). The
#: row already carries the outcome of the action, so neither may overwrite the answer that
#: authorized it -- otherwise an allowed action would read `completion`.
#:
#: The two strings are copied rather than imported, because `gates/audit.py` imports this module.
_NOT_DECISIONS = frozenset({"completion", "grant_written"})

#: How long a decision value may be, and which characters it may hold. A shape rule rather than a
#: list of names: the gate's vocabulary grows (`allow`, `approve`, `denied`, `expired`, `refused`,
#: `cleared`, `preview`, `would_gate`, `grant_promoted` today), and a list here would quietly file
#: the next one as `other`. The shape still refuses a sentence, which is what the rule is for.
_MAX_DECISION_CHARS = 32

#: The gate's own words about *policy* -- which class, which scope, which grant, who approved. The
#: action's own text never reaches this field: the gate log itself keeps a digest of the command
#: rather than the command (#16), and this row keeps neither.
_MAX_REASON_CHARS = 240


@dataclass(frozen=True, slots=True)
class GateNote:
    """What the gate answered for the tool call now running."""

    decision: str
    reason: str | None = None
    actor: str | None = None


class GateNoteBox:
    """One mutable slot, installed for the length of one tool call."""

    __slots__ = ("note",)

    def __init__(self) -> None:
        self.note: GateNote | None = None


_CURRENT_BOX: ContextVar[GateNoteBox | None] = ContextVar(
    "nanoinfra_gate_note_box",
    default=None,
)


def clean_gate_decision(value: str | None) -> str | None:
    """Normalise a decision, or answer ``None`` when it is not one."""
    if not value:
        return None
    cleaned = value.strip().lower()
    if not cleaned or len(cleaned) > _MAX_DECISION_CHARS:
        return None
    if not all(ch.isalnum() or ch in "_.-" for ch in cleaned):
        return None
    return cleaned


def clean_gate_reason(value: str | None) -> str | None:
    """One line of the gate's reason, or ``None``. Blank text is nobody's reason."""
    if not value:
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:_MAX_REASON_CHARS]


def note_gate_decision(
    *,
    decision: str | None,
    reason: str | None = None,
    actor: str | None = None,
) -> None:
    """Record what the gate answered for the tool call now running. A no-op outside one.

    The last decision wins, because the last one is the one the action ran (or did not run)
    under. The **reason and the approver survive** a later note that carries neither, so a
    *suspended -> approved by alberto -> allowed* sequence still names alberto on the row: the
    record that finally allows the action holds no actor, and dropping the name there would lose
    the only half of the audit trail a log does not already have.
    """
    box = _CURRENT_BOX.get()
    if box is None:
        return
    cleaned = clean_gate_decision(decision)
    if cleaned is None or cleaned in _NOT_DECISIONS:
        return
    previous = box.note
    box.note = GateNote(
        decision=cleaned,
        reason=clean_gate_reason(reason) or (previous.reason if previous else None),
        actor=_named_or_none(actor) or (previous.actor if previous else None),
    )


def current_gate_note() -> GateNote | None:
    """What the gate has answered so far in this tool call, or ``None``."""
    box = _CURRENT_BOX.get()
    return box.note if box is not None else None


@contextmanager
def gate_note_scope() -> Generator[GateNoteBox]:
    """Collect gate decisions taken inside this block, and only inside it."""
    box = GateNoteBox()
    token = _CURRENT_BOX.set(box)
    try:
        yield box
    finally:
        _CURRENT_BOX.reset(token)


def _named_or_none(value: str | None) -> str | None:
    """An empty name reads as a person, so nobody is ``None`` -- the rule #79 set for the log."""
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned[:128] if cleaned else None


__all__ = [
    "GateNote",
    "GateNoteBox",
    "clean_gate_decision",
    "clean_gate_reason",
    "current_gate_note",
    "gate_note_scope",
    "note_gate_decision",
]
