import asyncio
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.ask import AskUserInterrupt, AskUserTool
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.schema import tool_parameters_schema
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import GenerationSettings, LLMResponse, ToolCallRequest


def _make_provider(chat_with_retry):
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings()
    provider.chat_with_retry = chat_with_retry
    return provider


def test_ask_user_tool_schema_and_interrupt():
    tool = AskUserTool()
    schema = tool.to_schema()["function"]

    assert schema["name"] == "ask_user"
    assert "question" in schema["parameters"]["required"]
    assert schema["parameters"]["properties"]["options"]["type"] == "array"

    with pytest.raises(AskUserInterrupt) as exc:
        asyncio.run(tool.execute("Continue?", options=["Yes", "No"]))

    assert exc.value.question == "Continue?"
    assert exc.value.options == ["Yes", "No"]


@pytest.mark.asyncio
async def test_runner_pauses_on_ask_user_without_executing_later_tools():
    @tool_parameters(tool_parameters_schema(required=[]))
    class LaterTool(Tool):
        called = False

        @property
        def name(self) -> str:
            return "later"

        @property
        def description(self) -> str:
            return "Should not run after ask_user pauses the turn."

        async def execute(self, **kwargs):
            self.called = True
            return "later result"

    async def chat_with_retry(**kwargs):
        return LLMResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[
                ToolCallRequest(
                    id="call_ask",
                    name="ask_user",
                    arguments={"question": "Install this package?", "options": ["Yes", "No"]},
                ),
                ToolCallRequest(id="call_later", name="later", arguments={}),
            ],
        )

    later = LaterTool()
    tools = ToolRegistry()
    tools.register(AskUserTool())
    tools.register(later)

    result = await AgentRunner(_make_provider(chat_with_retry)).run(AgentRunSpec(
        initial_messages=[{"role": "user", "content": "continue"}],
        tools=tools,
        model="test-model",
        max_iterations=3,
        max_tool_result_chars=16_000,
        concurrent_tools=True,
    ))

    assert result.stop_reason == "ask_user"
    assert result.final_content == "Install this package?"
    assert "ask_user" in result.tools_used
    assert later.called is False
    assert result.messages[-1]["role"] == "assistant"
    assert result.messages[-1]["tool_calls"][0]["function"]["name"] == "ask_user"
    assert not any(message.get("name") == "ask_user" for message in result.messages)


@pytest.mark.asyncio
async def test_ask_user_sends_buttons_and_resumes_with_next_message(tmp_path):
    seen_messages: list[list[dict]] = []

    async def chat_with_retry(**kwargs):
        seen_messages.append(kwargs["messages"])
        if len(seen_messages) == 1:
            return LLMResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="call_ask",
                        name="ask_user",
                        arguments={
                            "question": "Install the optional package?",
                            "options": ["Install", "Skip"],
                        },
                    )
                ],
            )
        return LLMResponse(content="Skipped install.", usage={})

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_make_provider(chat_with_retry),
        workspace=tmp_path,
        model="test-model",
    )

    first = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="set it up")
    )

    assert first is not None
    assert first.content == "Install the optional package?"
    assert first.buttons == [["Install", "Skip"]]

    session = loop.sessions.get_or_create("cli:direct")
    assert any(message.get("role") == "assistant" and message.get("tool_calls") for message in session.messages)
    assert not any(message.get("role") == "tool" and message.get("name") == "ask_user" for message in session.messages)

    second = await loop._process_message(
        InboundMessage(channel="cli", sender_id="user", chat_id="direct", content="Skip")
    )

    assert second is not None
    assert second.content == "Skipped install."
    assert any(
        message.get("role") == "tool"
        and message.get("name") == "ask_user"
        and message.get("content") == "Skip"
        for message in seen_messages[-1]
    )
    assert not any(
        message.get("role") == "user" and message.get("content") == "Skip"
        for message in session.messages
    )
    assert any(
        message.get("role") == "tool"
        and message.get("name") == "ask_user"
        and message.get("content") == "Skip"
        for message in session.messages
    )
