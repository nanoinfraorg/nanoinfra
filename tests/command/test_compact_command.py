"""Compaction can be asked for, not only waited for (#212).

Four mechanisms compact a session and none of them took a request. Idle auto-compact fires after
`session_ttl_minutes` of silence; `maybe_consolidate_by_tokens` runs twice a turn but only acts near
`consolidation_ratio` of the budget; the in-flight governor acts only once a request already
overflows. So an operator reading `25,327 in prompt` against a 200K window sees a number nothing
will act on, and has no move.

The reply is measured rather than reassuring. "Compacted ✓" hides the trade — a summary replaces
history with a *description* of history, and the operator is the one choosing to lose it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from nanoinfra.bus.events import InboundMessage
from nanoinfra.command.builtin import cmd_compact
from nanoinfra.command.router import CommandContext


class _FakeSession:
    def __init__(self, *, messages: int = 40, last_consolidated: int = 6) -> None:
        self.key = "cli:direct"
        self.messages = [{"role": "user", "content": f"m{i}"} for i in range(messages)]
        self.last_consolidated = last_consolidated
        self.metadata: dict[str, Any] = {}


class _FakeConsolidator:
    def __init__(self, summary: str | None = "a summary of what happened") -> None:
        self.summary = summary
        self.calls: list[tuple[str, Any]] = []

    async def compact_idle_session(self, key: str, *, runtime: Any, **_: Any) -> str | None:
        self.calls.append((key, runtime))
        return self.summary


def _ctx(
    *,
    session: _FakeSession | None = None,
    consolidator: _FakeConsolidator | None = None,
    runtime: Any = "runtime",
) -> tuple[CommandContext, _FakeConsolidator]:
    session = session if session is not None else _FakeSession()
    consolidator = consolidator or _FakeConsolidator()
    msg = InboundMessage(channel="cli", sender_id="u1", chat_id="direct", content="/compact")
    loop = SimpleNamespace(
        consolidator=consolidator,
        runtime_for_session=lambda _session: runtime,
        sessions=SimpleNamespace(get_or_create=lambda _key: session),
    )
    ctx = CommandContext(
        msg=msg,
        session=session,  # pyright: ignore[reportArgumentType]
        key=msg.session_key,
        raw="/compact",
        args="",
        loop=loop,  # pyright: ignore[reportArgumentType]
    )
    return ctx, consolidator


# --- it runs the compaction that already exists -------------------------------------------


async def test_it_drives_the_consolidator_for_this_session() -> None:
    ctx, consolidator = _ctx()

    await cmd_compact(ctx)

    assert [key for key, _ in consolidator.calls] == ["cli:direct"]


async def test_it_passes_the_session_runtime() -> None:
    """Archiving asks the model for a summary, so it needs the runtime this session resolved."""
    ctx, consolidator = _ctx(runtime="kimi-k3-runtime")

    await cmd_compact(ctx)

    assert consolidator.calls[0][1] == "kimi-k3-runtime"


# --- the reply is a measurement -----------------------------------------------------------


async def test_it_says_how_many_messages_it_archived() -> None:
    ctx, _ = _ctx(session=_FakeSession(messages=40, last_consolidated=6))

    reply = await cmd_compact(ctx)

    assert "34" in reply.content


async def test_it_says_how_many_stay_raw() -> None:
    """The replay window is what survives a summary, and it is the honest half of the trade."""
    ctx, _ = _ctx()

    reply = await cmd_compact(ctx)

    assert "8" in reply.content


async def test_it_says_the_summary_arrives_on_the_next_turn() -> None:
    """The summary is delivered by `prepare_session`, so this turn's prompt is unchanged. A reply
    that only said "done" would read as a command that did nothing."""
    ctx, _ = _ctx()

    reply = await cmd_compact(ctx)

    assert "next" in reply.content.lower()


# --- nothing to do, and saying so ---------------------------------------------------------


async def test_a_session_with_nothing_unarchived_says_so_and_calls_nobody() -> None:
    ctx, consolidator = _ctx(session=_FakeSession(messages=6, last_consolidated=6))

    reply = await cmd_compact(ctx)

    assert consolidator.calls == []
    assert "nothing" in reply.content.lower()


async def test_a_session_shorter_than_the_replay_window_is_left_alone() -> None:
    """Archiving four messages to keep eight raw is work that changes no prompt."""
    ctx, consolidator = _ctx(session=_FakeSession(messages=4, last_consolidated=0))

    reply = await cmd_compact(ctx)

    assert consolidator.calls == []
    assert "nothing" in reply.content.lower()


async def test_a_summary_the_model_declined_is_reported_rather_than_claimed() -> None:
    """`archive` answers `(nothing)` when it has nothing to say. Reporting that as a successful
    compaction would tell the operator history was replaced when it was not."""
    ctx, _ = _ctx(consolidator=_FakeConsolidator(summary="(nothing)"))

    reply = await cmd_compact(ctx)

    assert "nothing" in reply.content.lower()


async def test_a_failure_is_reported_and_does_not_raise() -> None:
    """A command that raises takes the turn with it; the operator gets no answer at all."""

    class _Failing(_FakeConsolidator):
        async def compact_idle_session(self, key: str, *, runtime: Any, **_: Any) -> str | None:
            raise RuntimeError("provider is down")

    ctx, _ = _ctx(consolidator=_Failing())

    reply = await cmd_compact(ctx)

    assert "could not" in reply.content.lower()


async def test_it_answers_on_the_channel_that_asked() -> None:
    ctx, _ = _ctx()

    reply = await cmd_compact(ctx)

    assert reply.channel == "cli"
    assert reply.chat_id == "direct"


# --- registration -------------------------------------------------------------------------


def _router():
    from nanoinfra.command.builtin import register_builtin_commands
    from nanoinfra.command.router import CommandRouter

    router = CommandRouter()
    register_builtin_commands(router)
    return router


def test_the_command_is_dispatchable() -> None:
    assert _router().is_dispatchable_command("/compact") is True


def test_it_is_not_a_priority_command() -> None:
    """A priority command runs before the dispatch lock. Compaction takes a session lock and calls
    a provider, so it belongs behind the lock with the rest of the session work."""
    assert _router().is_priority("/compact") is False
