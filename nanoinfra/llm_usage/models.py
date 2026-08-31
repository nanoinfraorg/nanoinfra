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


__all__ = ["LLMCallRecord", "LLM_USAGE_SOURCES"]
