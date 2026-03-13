"""Tests for FeishuChannel tool hint code block formatting."""

import json
from unittest.mock import MagicMock, patch

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.channels.feishu import FeishuChannel


@pytest.fixture
def mock_feishu_channel():
    """Create a FeishuChannel with mocked client."""
    config = MagicMock()
    config.app_id = "test_app_id"
    config.app_secret = "test_app_secret"
    config.encrypt_key = None
    config.verification_token = None
    bus = MagicMock()
    channel = FeishuChannel(config, bus)
    channel._client = MagicMock()  # Simulate initialized client
    return channel


def test_tool_hint_sends_code_message(mock_feishu_channel):
    """Tool hint messages should be sent as code blocks."""
    msg = OutboundMessage(
        channel="feishu",
        chat_id="oc_123456",
        content='web_search("test query")',
        metadata={"_tool_hint": True}
    )

    with patch.object(mock_feishu_channel, '_send_message_sync') as mock_send:
        # Run send in async context
        import asyncio
        asyncio.run(mock_feishu_channel.send(msg))

        # Verify code message was sent
        assert mock_send.call_count == 1
        call_args = mock_send.call_args[0]
        receive_id_type, receive_id, msg_type, content = call_args

        assert receive_id_type == "chat_id"
        assert receive_id == "oc_123456"
        assert msg_type == "code"

        # Parse content to verify structure
        content_dict = json.loads(content)
        assert content_dict["title"] == "Tool Call"
        assert content_dict["code"] == 'web_search("test query")'
        assert content_dict["language"] == "text"


def test_tool_hint_empty_content_does_not_send(mock_feishu_channel):
    """Empty tool hint messages should not be sent."""
    msg = OutboundMessage(
        channel="feishu",
        chat_id="oc_123456",
        content="   ",  # whitespace only
        metadata={"_tool_hint": True}
    )

    with patch.object(mock_feishu_channel, '_send_message_sync') as mock_send:
        import asyncio
        asyncio.run(mock_feishu_channel.send(msg))

        # Should not send any message
        mock_send.assert_not_called()


def test_tool_hint_without_metadata_sends_as_normal(mock_feishu_channel):
    """Regular messages without _tool_hint should use normal formatting."""
    msg = OutboundMessage(
        channel="feishu",
        chat_id="oc_123456",
        content="Hello, world!",
        metadata={}
    )

    with patch.object(mock_feishu_channel, '_send_message_sync') as mock_send:
        import asyncio
        asyncio.run(mock_feishu_channel.send(msg))

        # Should send as text message (detected format)
        assert mock_send.call_count == 1
        call_args = mock_send.call_args[0]
        _, _, msg_type, content = call_args
        assert msg_type == "text"
        assert json.loads(content) == {"text": "Hello, world!"}


def test_tool_hint_multiple_tools_in_one_message(mock_feishu_channel):
    """Multiple tool calls should be in a single code block."""
    msg = OutboundMessage(
        channel="feishu",
        chat_id="oc_123456",
        content='web_search("query"), read_file("/path/to/file")',
        metadata={"_tool_hint": True}
    )

    with patch.object(mock_feishu_channel, '_send_message_sync') as mock_send:
        import asyncio
        asyncio.run(mock_feishu_channel.send(msg))

        call_args = mock_send.call_args[0]
        content = json.loads(call_args[3])
        assert content["code"] == 'web_search("query"), read_file("/path/to/file")'
        assert "\n" not in content["code"]  # Single line as intended
