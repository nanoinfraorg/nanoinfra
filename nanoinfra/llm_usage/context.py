"""Which kind of turn a provider call belongs to, without keeping the turn's identity (#176).

A row in the store says a call came from a person, from the API, from a schedule, from a Dream
consolidation, or from the system. It does **not** say which session, which chat, or which user --
those are the identifiers that would turn telemetry into a transcript, and the classification below
is deliberately lossy so that the answer cannot be walked back to the key it came from.

A contextvar rather than an argument, because the classification is a property of the turn and the
call sites in between -- the runner, the retry loop, a provider wrapper -- have no reason to know
about it or to thread it through.
"""

from __future__ import annotations

from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Literal

LLMUsageSource = Literal["user", "api", "cron", "dream", "system"]

#: `system` rather than `user` when nothing bound a source: an unattributed call is more likely to
#: be internal than to be somebody typing, and over-counting `user` would flatter the figure that
#: matters most.
_CURRENT_SOURCE: ContextVar[LLMUsageSource] = ContextVar(
    "nanoinfra_llm_usage_source",
    default="system",
)


def source_from_session_key(session_key: str | None) -> LLMUsageSource:
    """Classify a session key and keep none of it.

    The prefixes are the ones `webui/token_usage.py` already classified on, so the day rows this
    store inherits in #177 line up with the rows it writes itself.
    """
    key = session_key or ""
    if key.startswith("dream:"):
        return "dream"
    if key == "heartbeat" or key.startswith("cron:"):
        return "cron"
    if key.startswith("api:"):
        return "api"
    if key.startswith("system:"):
        return "system"
    return "user"


def source_from_request(
    session_key: str | None,
    *,
    channel: str | None,
    metadata: Mapping[str, object] | None,
) -> LLMUsageSource:
    """Classify a turn from the ingress that started it.

    Reads only trusted metadata this process wrote -- the cron and trigger markers -- rather than
    anything a message carried in, and falls back to the session key.
    """
    values = metadata or {}
    if isinstance(values.get("_cron_trigger"), Mapping):
        return "cron"
    if isinstance(values.get("_local_trigger"), Mapping):
        return "cron"
    if channel == "api":
        return "api"
    if channel == "system":
        return "system"
    return source_from_session_key(session_key)


def current_llm_usage_source() -> LLMUsageSource:
    return _CURRENT_SOURCE.get()


def bind_llm_usage_source(source: LLMUsageSource) -> Token[LLMUsageSource]:
    return _CURRENT_SOURCE.set(source)


def reset_llm_usage_source(token: Token[LLMUsageSource]) -> None:
    _CURRENT_SOURCE.reset(token)


@contextmanager
def llm_usage_source(source: LLMUsageSource) -> Generator[None]:
    """Bind a source for every provider call made inside this block."""
    token = bind_llm_usage_source(source)
    try:
        yield
    finally:
        reset_llm_usage_source(token)


__all__ = [
    "LLMUsageSource",
    "bind_llm_usage_source",
    "current_llm_usage_source",
    "llm_usage_source",
    "reset_llm_usage_source",
    "source_from_request",
    "source_from_session_key",
]
