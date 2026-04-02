"""Tests for /restart slash command."""

from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.providers.base import LLMResponse


def _make_loop():
    """Create a minimal AgentLoop with mocked dependencies."""
    from nanobot.agent.loop import AgentLoop
    from nanobot.bus.queue import MessageBus

    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    workspace = MagicMock()
    workspace.__truediv__ = MagicMock(return_value=MagicMock())

    with patch("nanobot.agent.loop.ContextBuilder"), \
         patch("nanobot.agent.loop.SessionManager"), \
         patch("nanobot.agent.loop.SubagentManager"):
        loop = AgentLoop(bus=bus, provider=provider, workspace=workspace)
    return loop, bus


class TestRestartCommand:

    @pytest.mark.asyncio
    async def test_restart_sends_message_and_calls_execv(self):
        from nanobot.command.builtin import cmd_restart
        from nanobot.config.runtime_keys import RESTART_NOTIFY_CHANNEL_ENV, RESTART_NOTIFY_CHAT_ID_ENV
        from nanobot.command.router import CommandContext

        loop, bus = _make_loop()
        msg = InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="/restart")
        ctx = CommandContext(msg=msg, session=None, key=msg.session_key, raw="/restart", loop=loop)

        with patch.dict(os.environ, {}, clear=False), \
             patch("nanobot.command.builtin.os.execv") as mock_execv:
            out = await cmd_restart(ctx)
            assert "Restarting" in out.content
            assert os.environ.get(RESTART_NOTIFY_CHANNEL_ENV) == "cli"
            assert os.environ.get(RESTART_NOTIFY_CHAT_ID_ENV) == "direct"

            await asyncio.sleep(1.5)
            mock_execv.assert_called_once()

    @pytest.mark.asyncio
    async def test_restart_intercepted_in_run_loop(self):
        """Verify /restart is handled at the run-loop level, not inside _dispatch."""
        loop, bus = _make_loop()
        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/restart")

        with patch.object(loop, "_dispatch", new_callable=AsyncMock) as mock_dispatch, \
             patch("nanobot.command.builtin.os.execv"):
            await bus.publish_inbound(msg)

            loop._running = True
            run_task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.1)
            loop._running = False
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

            mock_dispatch.assert_not_called()
            out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            assert "Restarting" in out.content

    @pytest.mark.asyncio
    async def test_status_intercepted_in_run_loop(self):
        """Verify /status is handled at the run-loop level for immediate replies."""
        loop, bus = _make_loop()
        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")

        with patch.object(loop, "_dispatch", new_callable=AsyncMock) as mock_dispatch:
            await bus.publish_inbound(msg)

            loop._running = True
            run_task = asyncio.create_task(loop.run())
            await asyncio.sleep(0.1)
            loop._running = False
            run_task.cancel()
            try:
                await run_task
            except asyncio.CancelledError:
                pass

            mock_dispatch.assert_not_called()
            out = await asyncio.wait_for(bus.consume_outbound(), timeout=1.0)
            assert "nanobot" in out.content.lower() or "Model" in out.content

    @pytest.mark.asyncio
    async def test_run_propagates_external_cancellation(self):
        """External task cancellation should not be swallowed by the inbound wait loop."""
        loop, _bus = _make_loop()

        run_task = asyncio.create_task(loop.run())
        await asyncio.sleep(0.1)
        run_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(run_task, timeout=1.0)

    @pytest.mark.asyncio
    async def test_help_includes_restart(self):
        loop, bus = _make_loop()
        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/help")

        response = await loop._process_message(msg)

        assert response is not None
        assert "/restart" in response.content
        assert "/status" in response.content
        assert response.metadata == {"render_as": "text"}

    @pytest.mark.asyncio
    async def test_status_reports_runtime_info(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [{"role": "user"}] * 3
        loop.sessions.get_or_create.return_value = session
        loop._start_time = time.time() - 125
        loop._last_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        loop.memory_consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(20500, "tiktoken")
        )

        msg = InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")

        response = await loop._process_message(msg)

        assert response is not None
        assert "Model: test-model" in response.content
        assert "Tokens: 0 in / 0 out" in response.content
        assert "Context: 20k/64k (31%)" in response.content
        assert "Session: 3 messages" in response.content
        assert "Uptime: 2m 5s" in response.content
        assert response.metadata == {"render_as": "text"}

    @pytest.mark.asyncio
    async def test_run_agent_loop_resets_usage_when_provider_omits_it(self):
        loop, _bus = _make_loop()
        loop.provider.chat_with_retry = AsyncMock(side_effect=[
            LLMResponse(content="first", usage={"prompt_tokens": 9, "completion_tokens": 4}),
            LLMResponse(content="second", usage={}),
        ])

        await loop._run_agent_loop([])
        assert loop._last_usage["prompt_tokens"] == 9
        assert loop._last_usage["completion_tokens"] == 4

        await loop._run_agent_loop([])
        assert loop._last_usage["prompt_tokens"] == 0
        assert loop._last_usage["completion_tokens"] == 0

    @pytest.mark.asyncio
    async def test_status_falls_back_to_last_usage_when_context_estimate_missing(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = [{"role": "user"}]
        loop.sessions.get_or_create.return_value = session
        loop._last_usage = {"prompt_tokens": 1200, "completion_tokens": 34}
        loop.memory_consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(0, "none")
        )

        response = await loop._process_message(
            InboundMessage(channel="telegram", sender_id="u1", chat_id="c1", content="/status")
        )

        assert response is not None
        assert "Tokens: 1200 in / 34 out" in response.content
        assert "Context: 1k/64k (1%)" in response.content

    @pytest.mark.asyncio
    async def test_process_direct_preserves_render_metadata(self):
        loop, _bus = _make_loop()
        session = MagicMock()
        session.get_history.return_value = []
        loop.sessions.get_or_create.return_value = session
        loop.subagents.get_running_count.return_value = 0

        response = await loop.process_direct("/status", session_key="cli:test")

        assert response is not None
        assert response.metadata == {"render_as": "text"}


@pytest.mark.asyncio
async def test_notify_restart_done_waits_until_channel_running() -> None:
    from nanobot.bus.queue import MessageBus
    from nanobot.cli.commands import _notify_restart_done_when_channel_ready

    bus = MessageBus()
    channel = SimpleNamespace(is_running=False)

    class DummyChannels:
        enabled_channels = ["feishu"]

        @staticmethod
        def get_channel(name: str):
            return channel if name == "feishu" else None

    async def _mark_running() -> None:
        await asyncio.sleep(0.02)
        channel.is_running = True

    marker = asyncio.create_task(_mark_running())
    sent = await _notify_restart_done_when_channel_ready(
        bus=bus,
        channels=DummyChannels(),
        channel="feishu",
        chat_id="oc_123",
        timeout_s=0.2,
        poll_s=0.01,
    )
    await marker

    assert sent is True
    out = await asyncio.wait_for(bus.consume_outbound(), timeout=0.1)
    assert out.channel == "feishu"
    assert out.chat_id == "oc_123"
    assert out.content == "Restart completed."


@pytest.mark.asyncio
async def test_notify_restart_done_times_out_when_channel_not_running() -> None:
    from nanobot.bus.queue import MessageBus
    from nanobot.cli.commands import _notify_restart_done_when_channel_ready

    bus = MessageBus()
    channel = SimpleNamespace(is_running=False)

    class DummyChannels:
        enabled_channels = ["feishu"]

        @staticmethod
        def get_channel(name: str):
            return channel if name == "feishu" else None

    sent = await _notify_restart_done_when_channel_ready(
        bus=bus,
        channels=DummyChannels(),
        channel="feishu",
        chat_id="oc_123",
        timeout_s=0.05,
        poll_s=0.01,
    )
    assert sent is False
    assert bus.outbound_size == 0


def test_should_show_cli_restart_notice() -> None:
    from nanobot.cli.commands import _should_show_cli_restart_notice

    assert _should_show_cli_restart_notice("cli", "direct", "cli:direct") is True
    assert _should_show_cli_restart_notice("cli", "", "cli:direct") is True
    assert _should_show_cli_restart_notice("cli", "other", "cli:direct") is False
    assert _should_show_cli_restart_notice("feishu", "oc_123", "cli:direct") is False
