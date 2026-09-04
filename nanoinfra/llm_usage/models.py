"""One row per provider attempt, and what a row may not carry (#176).

The exclusions are the reason to trust this store, so they are stated here rather than left to the
schema: **no prompts, no responses, no reasoning, no tool payloads, no provider error text, and no
session keys.** A row is a timestamp, a duration, a provider, a model, a coarse source, a finish
reason, and numbers. Nothing in it can be read back as something somebody said.

That is not incidental. Daily aggregates cannot answer "how many calls failed and retried, and what
did the retry cost", which is the question this exists for -- and the shortest path to answering it
would have been to log the calls, prompts and all. This shape answers it without ever holding
content, which means the store can be kept for 400 days without becoming a liability.
"""

from __future__ import annotations

from dataclasses import dataclass

from nanoinfra.llm_usage.context import LLMUsageSource
from nanoinfra.providers.base import LLMUsage

#: The coarse sources a row may name. Kept in step with `_SOURCE_KEYS` in `webui/token_usage.py`,
#: because #177 reads that file's rows into this store.
LLM_USAGE_SOURCES: frozenset[str] = frozenset({"user", "api", "cron", "dream", "system"})

#: What became of one tool call. `denied` is its own answer rather than a kind of `error`: a gate
#: that refused an action is not a tool that broke, and a metrics page that counted them together
#: would report a deployment working as designed as a fault (#233).
TOOL_CALL_OUTCOMES: frozenset[str] = frozenset({"ok", "error", "denied"})

#: Coarse kinds for a call that failed. The same rule as `_ERROR_KINDS` in `store.py`: a kind,
#: never the message -- an error message routinely quotes the argument back.
TOOL_CALL_ERROR_KINDS: frozenset[str] = frozenset({
    "blocked",
    "exception",
    "invalid_params",
    "not_found",
    "other",
    "tool_error",
    "unavailable",
})


@dataclass(frozen=True, slots=True)
class LLMCallRecord:
    """What one provider attempt cost and how it ended.

    An *attempt*, not a turn and not a request: a retried call is two rows, which is the only way
    the retry question has an answer. Sessions already own the content, so this contract owns none.
    """

    started_at_ms: int
    duration_ms: int
    provider: str
    model: str
    source: LLMUsageSource
    stream: bool
    finish_reason: str
    usage: LLMUsage | None = None
    error_status_code: int | None = None
    error_kind: str | None = None

    def __post_init__(self) -> None:
        if self.started_at_ms < 0 or self.duration_ms < 0:
            raise ValueError("an LLM call cannot have started or lasted a negative time")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("an LLM call record needs a provider and a model")
        if self.source not in LLM_USAGE_SOURCES:
            raise ValueError(f"{self.source!r} is not one of {sorted(LLM_USAGE_SOURCES)}")
        if not self.finish_reason.strip():
            raise ValueError("an LLM call record needs a finish reason")


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """What one tool call did, and **the address of its arguments rather than the arguments** (#232).

    ``session_key + turn_id + seq`` locates the call in the session history that already holds the
    arguments and the output, so a reader expands a row by reading the transcript. That is the
    whole design. A metrics database holding every command line would be a second copy of the
    conversation -- including the one with a token in an argument -- living outside the retention
    and the compaction that govern the first copy, and a store nobody expects to hold content is
    the worst place for it to end up.

    So this contract deliberately has **no field an argument, a path, a file body or an output
    could be written to**. ``tests/llm_usage/test_tool_call_store.py`` asserts it of the database
    file itself rather than of the columns, because a schema can grow a column.

    The identifiers are the one way this row differs from :class:`LLMCallRecord`, which keeps no
    session key at all. The difference is the point: the usage row answers what a *day* cost and
    needs no identity to do it, and this row answers what a *call* did, which is a question about
    one call in one session. Retention is 180 days here rather than 400 for the same reason.
    """

    ts_ms: int
    tool: str
    source: LLMUsageSource
    outcome: str
    duration_ms: int
    session_key: str | None = None
    turn_id: str | None = None
    seq: int | None = None
    actor: str | None = None
    capability_class: str | None = None
    gate_decision: str | None = None
    gate_reason: str | None = None
    error_kind: str | None = None

    def __post_init__(self) -> None:
        if self.ts_ms < 0 or self.duration_ms < 0:
            raise ValueError("a tool call cannot have started or lasted a negative time")
        if not self.tool.strip():
            raise ValueError("a tool call record needs a tool name")
        if self.source not in LLM_USAGE_SOURCES:
            raise ValueError(f"{self.source!r} is not one of {sorted(LLM_USAGE_SOURCES)}")
        if self.outcome not in TOOL_CALL_OUTCOMES:
            raise ValueError(f"{self.outcome!r} is not one of {sorted(TOOL_CALL_OUTCOMES)}")
        if self.seq is not None and self.seq < 0:
            raise ValueError("a tool call cannot be at a negative position in its turn")


__all__ = [
    "LLMCallRecord",
    "LLM_USAGE_SOURCES",
    "TOOL_CALL_ERROR_KINDS",
    "TOOL_CALL_OUTCOMES",
    "ToolCallRecord",
]
