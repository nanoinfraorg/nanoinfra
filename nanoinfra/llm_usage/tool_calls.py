"""The seam that writes one row per tool call (#232).

A row is written by the code that *called* the tool, not by the tool, for the same reason #176 put
the LLM observer on the provider's retry loop rather than on an `AgentHook`: a tool that has to
remember to report itself is a tool that will not, and a new tool would arrive uncounted. Two
callers pass through here -- `ToolRegistry.execute` and the runner's own execute step -- and the
scope is **re-entrant**: when one calls the other, the outer scope owns the row, so a call that
takes both paths is still exactly one row.

What the scope reads for itself, rather than being told:

- `session_key`, `turn_id` and `actor` from the bound request (`agent/tools/context.py`).
- `source` from the contextvar `agent/loop.py` binds for the whole turn, so `user` / `api` /
  `cron` / `dream` / `system` mean here exactly what they mean on the usage heatmap.
- `seq`, from a counter per turn. It is the position of the call in its turn, which together with
  the two identifiers above is the address of the arguments in the session history.
- the gate's answer, from `gate_join.py`, which is empty when no gate answered.

Fails open. A tool call that broke because its telemetry broke would be a worse trade than a
missing row, so every path here swallows and logs -- the same contract as `record_llm_call`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

from loguru import logger

from nanoinfra.llm_usage.gate_join import GateNote, current_gate_note, gate_note_scope

if TYPE_CHECKING:
    from nanoinfra.llm_usage.context import LLMUsageSource

#: Decisions that mean the action did not happen. A denied call is recorded as **denied and not as
#: an error** (#233): a gate refusing an action is the deployment working, and a metrics page that
#: counted the two together would report policy as breakage.
_REFUSING_DECISIONS = frozenset({"denied", "deny", "refused", "expired"})

#: True while a row is being recorded in this context. The inner scope of a nested pair yields a
#: handle that writes nothing, because the outer one already addresses the same call.
_RECORDING: ContextVar[bool] = ContextVar("nanoinfra_tool_call_recording", default=False)

#: `seq` per turn. A dict rather than a contextvar because a turn spans several agent iterations
#: and, with `concurrent_tools`, several tasks -- and a task gets a *copy* of the context, so a
#: counter created inside one would restart at zero for its sibling.
_SEQ_LOCK = threading.Lock()
_SEQ_BY_TURN: dict[str, int] = {}
#: How many turns the counter remembers. Bounded because nothing tells this module that a turn
#: ended, and the least recently used entry is the one least likely to see another call.
_MAX_TURNS_TRACKED = 512

# The prefixes this tree's own error paths produce. Matched to *classify*, and the row keeps only
# the class -- never the text, which routinely quotes the argument back. `tool_error` is the
# honest default: the call failed and said so, and why is a question for the transcript.
_ERROR_MARKERS: tuple[tuple[str, str], ...] = (
    ("not found.", "not_found"),
    ("is unavailable", "unavailable"),
    ("invalid parameters for tool", "invalid_params"),
    ("parameters must be a json object", "invalid_params"),
    ("non-bypassable security boundary", "blocked"),
    ("outside the workspace", "blocked"),
)


def next_seq(session_key: str | None, turn_id: str | None) -> int:
    """The position of the next tool call in this turn, counting from zero."""
    key = f"{session_key or ''}|{turn_id or ''}"
    with _SEQ_LOCK:
        # Popped and re-inserted rather than updated in place, so the dict orders by *last use*.
        # A plain update would leave a long turn at the front of the insertion order and evict it
        # while it was still running, and its next call would then reuse an address already taken.
        seq = _SEQ_BY_TURN.pop(key, 0)
        _SEQ_BY_TURN[key] = seq + 1
        if len(_SEQ_BY_TURN) > _MAX_TURNS_TRACKED:
            del _SEQ_BY_TURN[next(iter(_SEQ_BY_TURN))]
        return seq


def reset_tool_call_seq() -> None:
    """Forget every turn's position. For tests, so one asserts on `seq` from a known start."""
    with _SEQ_LOCK:
        _SEQ_BY_TURN.clear()


def classify_tool_error(payload: object) -> str:
    """Name a coarse kind for a failed call, from the text of its own error message.

    The text is read and thrown away; only the kind is stored. That is the same discipline
    `_clean_error_kind` applies to a provider's error in `store.py`, and for the same reason: the
    message is the field most likely to quote the argument back.
    """
    text = str(payload)[:400].lower() if payload is not None else ""
    for marker, kind in _ERROR_MARKERS:
        if marker in text:
            return kind
    return "tool_error"


class ToolCallScope:
    """One tool call in flight. The caller says how it ended; the scope writes the row."""

    __slots__ = (
        "tool",
        "capability_class",
        "recording",
        "_started_ms",
        "_started_at",
        "_outcome",
        "_error_kind",
        "_session_key",
        "_turn_id",
        "_seq",
        "_actor",
        "_source",
    )

    def __init__(
        self,
        *,
        tool: str,
        capability_class: str | None,
        recording: bool,
    ) -> None:
        self.tool = tool
        self.capability_class = capability_class
        self.recording = recording
        self._started_ms = int(time.time() * 1000)
        self._started_at = time.monotonic()
        self._outcome = "ok"
        self._error_kind: str | None = None
        self._session_key: str | None = None
        self._turn_id: str | None = None
        self._seq: int | None = None
        self._actor: str | None = None
        self._source: LLMUsageSource = "system"
        if recording:
            self._read_the_turn()

    def failed(self, error_kind: str | None = None) -> None:
        """The call ended in an error. A gate denial still overrides this at write time."""
        self._outcome = "error"
        self._error_kind = error_kind or "tool_error"

    def _read_the_turn(self) -> None:
        # Imported here rather than at module level: this module is reached from the gate's own
        # import in `audit.py`, and the usage package has stayed clear of the agent tree.
        from nanoinfra.agent.tools.context import current_request_context
        from nanoinfra.llm_usage.context import current_llm_usage_source

        self._source = current_llm_usage_source()
        ctx = current_request_context()
        if ctx is not None:
            self._session_key = ctx.session_key
            self._turn_id = ctx.turn_id
            self._actor = ctx.sender_id
        self._seq = next_seq(self._session_key, self._turn_id)

    def finish(self, note: GateNote | None) -> None:
        """Write the row. Called by the scope that owns this call, once, at the end of it."""
        if not self.recording:
            return
        from nanoinfra.llm_usage import record_tool_call
        from nanoinfra.llm_usage.models import ToolCallRecord

        decision = note.decision if note is not None else None
        outcome = "denied" if decision in _REFUSING_DECISIONS else self._outcome
        record_tool_call(
            ToolCallRecord(
                ts_ms=self._started_ms,
                tool=self.tool,
                source=self._source,
                outcome=outcome,
                duration_ms=max(0, int((time.monotonic() - self._started_at) * 1000)),
                session_key=self._session_key,
                turn_id=self._turn_id,
                seq=self._seq,
                # The person the row belongs to: the one the gate authenticated as having
                # answered when there was an answer, and otherwise the identity the turn arrived
                # under. The first is authentication and the second is a channel's claim, which
                # is why the answered case wins -- and why the two never merge into one name for
                # a call nobody approved.
                actor=(note.actor if note is not None and note.actor else self._actor),
                capability_class=self.capability_class,
                gate_decision=decision,
                gate_reason=note.reason if note is not None else None,
                # A denial is not a failure, so it carries no kind. An error under a gate that
                # allowed the action keeps its own.
                error_kind=None if outcome == "denied" else self._error_kind,
            )
        )


@contextmanager
def tool_call_record(
    *,
    tool: str,
    capability_class: str | None = None,
) -> Generator[ToolCallScope]:
    """Write one row for the tool call made inside this block.

    An exception leaving the block is an error outcome and is re-raised untouched: this is
    telemetry, and it does not get to change what the caller sees.
    """
    if _RECORDING.get():
        yield ToolCallScope(tool=tool, capability_class=capability_class, recording=False)
        return
    token = _RECORDING.set(True)
    scope = ToolCallScope(tool=tool, capability_class=capability_class, recording=True)
    try:
        with gate_note_scope():
            try:
                yield scope
            except BaseException:
                scope.failed("exception")
                raise
            finally:
                try:
                    scope.finish(current_gate_note())
                except Exception:
                    logger.exception("failed to record a tool call")
    finally:
        _RECORDING.reset(token)


__all__ = [
    "ToolCallScope",
    "classify_tool_error",
    "next_seq",
    "reset_tool_call_seq",
    "tool_call_record",
]
