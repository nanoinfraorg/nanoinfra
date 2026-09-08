"""An ephemeral SDK run writes nothing, and leaves the cached session alone.

``Nanoinfra.run(ephemeral=True)`` documents a turn that is not persisted. What the flag
suppressed was the ``session_turn_persisted`` event and consolidation; the session file was
written on every ephemeral turn, mid-turn checkpoints included, and the shared in-memory
session carried the turn's messages afterwards. The coverage that existed
(``tests/test_nanoinfra_facade.py``, ``test_ephemeral_run_does_not_invoke_persisted_turn_callback``)
asserted the missing event, which is why the storage half went unnoticed. So every assertion
here is about state: the bytes on disk, and the messages the cached session object holds.

``ephemeral`` also carries an older, internal meaning that these tests must not change: a Dream
turn is ephemeral and its session *is* saved to disk, where the WebUI sidebar lists it and
``MemoryStore.prune_dream_sessions`` rotates it. That guard lives in
``tests/agent/test_dream.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.nanoinfra import Nanoinfra
from nanoinfra.providers.base import LLMResponse

_KEY = "sdk:shared"


def _fake_provider(replies: list[str] | None = None) -> MagicMock:
    """A provider that answers, and records the prompt it was handed."""
    provider = MagicMock(name="test-model")
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(
        max_tokens=8192,
        temperature=0.1,
        reasoning_effort=None,
    )
    provider.prompts: list[list[dict[str, Any]]] = []
    pending = list(replies or [])

    async def _chat(**kwargs: Any) -> LLMResponse:
        provider.prompts.append(list(kwargs.get("messages") or []))
        content = pending.pop(0) if pending else "answer"
        return LLMResponse(content=content, tool_calls=[])

    provider.chat_with_retry = AsyncMock(side_effect=_chat)
    # `run_streamed` takes the other entry point; both answer the same way here.
    provider.chat_stream_with_retry = AsyncMock(side_effect=_chat)
    return provider


def _bot(tmp_path: Path, provider: MagicMock) -> Nanoinfra:
    from nanoinfra.agent.loop import AgentLoop
    from nanoinfra.bus.queue import MessageBus

    return Nanoinfra(AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
    ))


def _stored(bot: Nanoinfra) -> dict[str, bytes]:
    """Every session file on disk, by name, so a new one is as visible as a changed one."""
    sessions_dir = bot._loop.sessions.sessions_dir
    return {path.name: path.read_bytes() for path in sorted(sessions_dir.glob("*.jsonl"))}


@pytest.mark.asyncio
async def test_ephemeral_run_leaves_the_stored_session_byte_identical(tmp_path):
    bot = _bot(tmp_path, _fake_provider())
    await bot.run("remember this", session_key=_KEY)
    before = _stored(bot)
    assert before, "the ordinary run must have written a session file"

    await bot.run("do not keep me", session_key=_KEY, ephemeral=True)

    assert _stored(bot) == before


@pytest.mark.asyncio
async def test_ephemeral_streamed_run_leaves_the_stored_session_byte_identical(tmp_path):
    """`run_streamed` builds its own kwargs, so it needs its own assertion."""
    bot = _bot(tmp_path, _fake_provider())
    await bot.run("remember this", session_key=_KEY)
    before = _stored(bot)

    run = await bot.run_streamed("do not keep me", session_key=_KEY, ephemeral=True)
    await run.wait()

    assert _stored(bot) == before


@pytest.mark.asyncio
async def test_ephemeral_run_on_a_new_key_writes_no_session_file(tmp_path):
    bot = _bot(tmp_path, _fake_provider())

    await bot.run("do not keep me", session_key="sdk:fresh", ephemeral=True)

    assert _stored(bot) == {}


@pytest.mark.asyncio
async def test_ephemeral_run_leaves_the_cached_session_unchanged(tmp_path):
    bot = _bot(tmp_path, _fake_provider())
    await bot.run("remember this", session_key=_KEY)
    cached = bot._loop.sessions.get_cached(_KEY)
    assert cached is not None
    before = [dict(message) for message in cached.messages]

    await bot.run("do not keep me", session_key=_KEY, ephemeral=True)

    assert bot._loop.sessions.get_cached(_KEY) is cached
    assert cached.messages == before


@pytest.mark.asyncio
async def test_run_after_an_ephemeral_run_continues_the_stored_transcript(tmp_path):
    provider = _fake_provider()
    bot = _bot(tmp_path, provider)
    await bot.run("remember this", session_key=_KEY)

    await bot.run("do not keep me", session_key=_KEY, ephemeral=True)
    await bot.run("what now", session_key=_KEY)

    last_prompt = "\n".join(str(message.get("content")) for message in provider.prompts[-1])
    assert "remember this" in last_prompt
    assert "do not keep me" not in last_prompt


@pytest.mark.asyncio
async def test_ephemeral_run_that_fails_leaves_the_stored_session_byte_identical(tmp_path):
    provider = _fake_provider()
    bot = _bot(tmp_path, provider)
    await bot.run("remember this", session_key=_KEY)
    before = _stored(bot)
    provider.chat_with_retry = AsyncMock(side_effect=RuntimeError("provider down"))

    with pytest.raises(RuntimeError):
        await bot.run("do not keep me", session_key=_KEY, ephemeral=True)

    # The turn never reached its save stage, so what this pins is the write before the model
    # call: _persist_user_message_early put the prompt on disk during the build stage.
    assert _stored(bot) == before
