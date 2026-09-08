"""A read-only turn writes nothing, wherever the write would have landed.

`process_direct(read_only_session=True)` is the seam the SDK's `ephemeral=True` uses. The
session file is protected by the detached copy `_restore_turn` builds, but two history writers
live outside it -- the file-cap archiver and token consolidation, both of which append to
memory/history.jsonl. `ephemeral` is what gates those, so a read-only turn implies it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nanoinfra.providers.base import LLMResponse


def _loop(tmp_path):
    from nanoinfra.agent.loop import AgentLoop
    from nanoinfra.bus.queue import MessageBus

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.supports_tools = True
    provider.generation = MagicMock(max_tokens=4096)
    provider.chat_with_retry = AsyncMock(
        return_value=LLMResponse(content="done", finish_reason="stop")
    )
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        context_window_tokens=8000,
    )


async def test_read_only_turn_writes_no_session_file(tmp_path):
    loop = _loop(tmp_path)

    response = await loop.process_direct(
        "hi", session_key="cli:direct", read_only_session=True,
    )

    assert response is not None
    assert list(loop.sessions.sessions_dir.glob("*.jsonl")) == []
    # The goal runtime-context provider (agent/tools/long_task.py) reads the key through
    # `get_or_create`, which caches an empty session as a side effect of the read. That entry is
    # not the turn's state, so what this pins is that no part of the turn reached it.
    cached = loop.sessions.get_cached("cli:direct")
    assert cached is None or (cached.messages == [] and cached.metadata == {})


async def test_read_only_turn_runs_no_history_writer(tmp_path):
    loop = _loop(tmp_path)

    with (
        patch.object(loop.context.memory, "raw_archive") as archive,
        patch.object(loop.consolidator, "maybe_consolidate_by_tokens") as consolidate,
    ):
        await loop.process_direct(
            "hi", session_key="cli:direct", read_only_session=True,
        )

    archive.assert_not_called()
    consolidate.assert_not_called()
