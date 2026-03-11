"""Tests for subagent reasoning_content and thinking_blocks handling."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSubagentReasoningContent:
    """Test that subagent properly handles reasoning_content and thinking_blocks."""

    @pytest.mark.asyncio
    async def test_subagent_message_includes_reasoning_content(self):
        """Verify reasoning_content is included in assistant messages with tool calls.

        This is the fix for issue #1834: Spawn/subagent tool fails with
        Deepseek Reasoner due to missing reasoning_content field.
        """
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "deepseek-reasoner"

        # Create a real Path object for workspace
        workspace = Path("/tmp/test_workspace")
        workspace.mkdir(parents=True, exist_ok=True)

        # Capture messages that are sent to the provider
        captured_messages = []

        async def mock_chat(*args, **kwargs):
            captured_messages.append(kwargs.get("messages", []))
            # Return response with tool calls and reasoning_content
            tool_call = ToolCallRequest(
                id="test-1",
                name="read_file",
                arguments={"path": "/test.txt"},
            )
            return LLMResponse(
                content="",
                tool_calls=[tool_call],
                reasoning_content="I need to read this file first",
            )

        provider.chat_with_retry = AsyncMock(side_effect=mock_chat)

        mgr = SubagentManager(provider=provider, workspace=workspace, bus=bus)

        # Mock the tools registry
        with patch("nanobot.agent.subagent.ToolRegistry") as MockToolRegistry:
            mock_registry = MagicMock()
            mock_registry.get_definitions.return_value = []
            mock_registry.execute = AsyncMock(return_value="file content")
            MockToolRegistry.return_value = mock_registry

            result = await mgr.spawn(
                task="Read a file",
                label="test",
                origin_channel="cli",
                origin_chat_id="direct",
                session_key="cli:direct",
            )

            # Wait for the task to complete
            await asyncio.sleep(0.5)

        # Check the captured messages
        assert len(captured_messages) >= 1
        # Find the assistant message with tool_calls
        found = False
        for msg_list in captured_messages:
            for msg in msg_list:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    assert "reasoning_content" in msg, "reasoning_content should be in assistant message with tool_calls"
                    assert msg["reasoning_content"] == "I need to read this file first"
                    found = True
        assert found, "Should have found an assistant message with tool_calls"

    @pytest.mark.asyncio
    async def test_subagent_message_includes_thinking_blocks(self):
        """Verify thinking_blocks is included in assistant messages with tool calls."""
        from nanobot.agent.subagent import SubagentManager
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "claude-sonnet"

        workspace = Path("/tmp/test_workspace2")
        workspace.mkdir(parents=True, exist_ok=True)

        captured_messages = []

        async def mock_chat(*args, **kwargs):
            captured_messages.append(kwargs.get("messages", []))
            tool_call = ToolCallRequest(
                id="test-2",
                name="read_file",
                arguments={"path": "/test.txt"},
            )
            return LLMResponse(
                content="",
                tool_calls=[tool_call],
                thinking_blocks=[
                    {"signature": "sig1", "thought": "thinking step 1"},
                    {"signature": "sig2", "thought": "thinking step 2"},
                ],
            )

        provider.chat_with_retry = AsyncMock(side_effect=mock_chat)

        mgr = SubagentManager(provider=provider, workspace=workspace, bus=bus)

        with patch("nanobot.agent.subagent.ToolRegistry") as MockToolRegistry:
            mock_registry = MagicMock()
            mock_registry.get_definitions.return_value = []
            mock_registry.execute = AsyncMock(return_value="file content")
            MockToolRegistry.return_value = mock_registry

            result = await mgr.spawn(
                task="Read a file",
                label="test",
                origin_channel="cli",
                origin_chat_id="direct",
            )

            await asyncio.sleep(0.5)

        # Check the captured messages
        found = False
        for msg_list in captured_messages:
            for msg in msg_list:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    assert "thinking_blocks" in msg, "thinking_blocks should be in assistant message with tool_calls"
                    assert len(msg["thinking_blocks"]) == 2
                    found = True
        assert found, "Should have found an assistant message with tool_calls"
