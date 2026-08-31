"""Tests for /stop preserving partial context from interrupted turns.

When /stop cancels an active task, the runtime checkpoint (tool results,
assistant messages accumulated so far) should be materialized into session
history rather than silently discarded.

See: https://github.com/HKUDS/nanobot/issues/2966
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.bus.queue import MessageBus


def _make_provider():
    """Create an LLM provider mock with required attributes."""
    from types import SimpleNamespace
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = SimpleNamespace(max_tokens=4096, temperature=0.1, reasoning_effort=None)
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


def _make_loop(tmp_path: Path) -> AgentLoop:
    """Create a real AgentLoop with mocked provider — avoids patching __init__."""
    bus = MessageBus()
    provider = _make_provider()
    with patch("nanoinfra.agent.loop.ContextBuilder"), \
         patch("nanoinfra.agent.loop.SessionManager"), \
         patch("nanoinfra.agent.loop.SubagentManager") as mock_subagent_manager:
        mock_subagent_manager.return_value.cancel_by_session = AsyncMock(return_value=0)
        return AgentLoop(bus=bus, provider=provider, workspace=tmp_path)


class TestStopPreservesContext:
    """Verify that /stop restores partial context via checkpoint."""

    def test_restore_checkpoint_method_exists(self, tmp_path):
        """AgentLoop should have _restore_runtime_checkpoint."""
        loop = _make_loop(tmp_path)
        assert hasattr(loop, "_restore_runtime_checkpoint")

    def test_checkpoint_key_constant(self, tmp_path):
        """The runtime checkpoint key should be defined."""
        loop = _make_loop(tmp_path)
        assert loop._RUNTIME_CHECKPOINT_KEY == "runtime_checkpoint"

    def test_cancel_dispatch_restores_checkpoint(self, tmp_path):
        """When a task is cancelled, the checkpoint should be restored."""
        loop = _make_loop(tmp_path)
        session = MagicMock()
        session.metadata = {
            "runtime_checkpoint": {
                "phase": "awaiting_tools",
                "iteration": 0,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search for that.",
                    "tool_calls": [{"id": "tc_1", "type": "function",
                                    "function": {"name": "web_search", "arguments": "{}"}}],
                },
                "completed_tool_results": [
                    {"role": "tool", "tool_call_id": "tc_1",
                     "content": "Search results: ..."},
                ],
                "pending_tool_calls": [],
            }
        }
        session.messages = [
            {"role": "user", "content": "Search for something"},
        ]
        loop.sessions.get_or_create.return_value = session

        restored = loop._restore_runtime_checkpoint(session)
        assert restored is True
        assert len(session.messages) > 1
        assert "runtime_checkpoint" not in session.metadata


@pytest.mark.asyncio
async def test_dispatch_cancellation_restores_checkpoint():
    """Regression for #2966: /stop interrupting _dispatch must materialize the
    in-flight runtime checkpoint into session.messages before the cancellation
    unwinds, so the next turn can see the partial work.

    This exercises the real _dispatch path (locks, pending queues, the
    CancelledError handler) rather than poking _restore_runtime_checkpoint in
    isolation, so a future refactor that drops the cancel-time restore is
    caught by CI instead of silently regressing.
    """
    from nanoinfra.bus.events import InboundMessage
    from nanoinfra.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanoinfra.agent.loop.ContextBuilder"), \
         patch("nanoinfra.agent.loop.SessionManager"), \
         patch("nanoinfra.agent.loop.SubagentManager") as mock_subagent_manager:
        mock_subagent_manager.return_value.cancel_by_session = AsyncMock(return_value=0)
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)

    checkpoint_key = loop._RUNTIME_CHECKPOINT_KEY
    session = SimpleNamespace(
        key="test:c1",
        metadata={
            checkpoint_key: {
                "phase": "awaiting_tools",
                "iteration": 0,
                "assistant_message": {
                    "role": "assistant",
                    "content": "Let me search.",
                    "tool_calls": [
                        {
                            "id": "tc_1",
                            "type": "function",
                            "function": {"name": "web_search", "arguments": "{}"},
                        }
                    ],
                },
                "completed_tool_results": [
                    {"role": "tool", "tool_call_id": "tc_1", "content": "Search hit."},
                ],
                "pending_tool_calls": [],
            }
        },
        messages=[{"role": "user", "content": "Search for something"}],
    )

    loop.sessions.get_or_create = MagicMock(return_value=session)
    loop.sessions.save = MagicMock()

    async def _cancel(*_args, **_kwargs):
        raise asyncio.CancelledError()

    loop._process_message = _cancel

    msg = InboundMessage(channel="test", sender_id="u1", chat_id="c1", content="work")

    with pytest.raises(asyncio.CancelledError):
        await loop._dispatch(msg)

    roles = [m.get("role") for m in session.messages]
    assert roles == ["user", "assistant", "tool"], (
        "Expected the assistant message and completed tool result from the "
        f"interrupted turn to be materialized into session.messages; got {roles}"
    )
    assert checkpoint_key not in session.metadata, \
        "Checkpoint metadata should be cleared after restore"
    assert loop.sessions.save.called, \
        "Session should be persisted so the restored state survives process restart"


# --- a gateway exit is the same interruption, and #200 asks whether it survives -----------


@pytest.mark.asyncio
async def test_shutdown_awaits_the_tasks_it_cancels(tmp_path: Path) -> None:
    """The branch that materialises a checkpoint runs *in* the cancelled task.

    So a shutdown that cancelled without awaiting would exit before that branch could write,
    and the partial turn would be lost -- which is what #200 was raised about. It survives
    because `_close_mcp_unlocked` gathers what it cancels; nothing asserted that until now.
    """
    import inspect

    source = inspect.getsource(AgentLoop._close_mcp_unlocked)

    assert "task.cancel()" in source
    cancel_at = source.index("task.cancel()")
    gather_at = source.index("asyncio.gather(*active_tasks")
    assert cancel_at < gather_at, "the cancel must be followed by a gather, not replace it"


def test_an_interrupted_turn_closes_with_a_record_rather_than_a_gap(tmp_path: Path) -> None:
    """A turn that persisted only the user message is closed on the next start.

    Deliberately closed rather than resumed. A resumed turn re-enters the capability gate, and
    an approval answered before the restart is not answered after it -- so a silent replay
    would ask a person to approve the same action twice, or worse, act on the belief that they
    already had.
    """
    from nanoinfra.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    session = manager.get_or_create("websocket:interrupted")
    session.messages.append({"role": "user", "content": "do the thing"})
    session.metadata[AgentLoop._PENDING_USER_TURN_KEY] = True

    loop = AgentLoop.__new__(AgentLoop)
    assert loop._restore_pending_user_turn(session) is True

    assert session.messages[-1]["role"] == "assistant"
    assert "interrupted" in session.messages[-1]["content"]
    # And the flag is gone, so a second start does not append a second record.
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata
    assert loop._restore_pending_user_turn(session) is False
