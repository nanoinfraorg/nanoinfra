"""Anthropic streaming idle timeout should follow the full SSE stream, not text only."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanoinfra.providers.anthropic_provider import AnthropicProvider


def _final_message_stub(text: str = "Hi") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=2,
            cache_creation_input_tokens=None,
            cache_read_input_tokens=None,
        ),
    )


class _FakeAsyncStream:
    """Minimal async iterator + context manager mimicking AsyncMessageStream."""

    def __init__(self, chunks: list[SimpleNamespace]) -> None:
        self._chunks = chunks
        self._idx = 0
        self.get_final_message = AsyncMock(return_value=_final_message_stub())

    async def __anext__(self) -> SimpleNamespace:
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._idx]
        self._idx += 1
        return c

    def __aiter__(self) -> _FakeAsyncStream:
        return self

    async def __aenter__(self) -> _FakeAsyncStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass


class _ConsumingFakeAsyncStream:
    """Mimics the real AsyncMessageStream: ``__anext__`` yields chunks after a
    per-chunk network delay, and ``get_final_message()`` consumes whatever is
    left (like the SDK's ``until_done()``) before returning the snapshot."""

    def __init__(
        self,
        chunks: list[SimpleNamespace],
        per_chunk_delay: float,
        final: SimpleNamespace | None = None,
    ) -> None:
        self._chunks = chunks
        self._idx = 0
        self._delay = per_chunk_delay
        self._final = final if final is not None else _final_message_stub("ok")

    async def __anext__(self) -> SimpleNamespace:
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._idx]
        self._idx += 1
        await asyncio.sleep(self._delay)
        return c

    def __aiter__(self) -> _ConsumingFakeAsyncStream:
        return self

    async def get_final_message(self) -> SimpleNamespace:
        async for _ in self:
            pass
        return self._final

    async def __aenter__(self) -> _ConsumingFakeAsyncStream:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass


def _bind(provider: AnthropicProvider, fake: object) -> None:
    """Point the provider's mocked client at ``fake`` as its stream context."""
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=fake)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    provider._client.messages.stream = MagicMock(return_value=stream_cm)


@pytest.mark.asyncio
async def test_chat_stream_without_callback_survives_long_active_stream(monkeypatch) -> None:
    """Regression: the idle timeout must not double as a total timeout.

    A stream that keeps producing (5 x 0.06 s = 0.30 s) well past the idle
    bound (0.15 s) must finish. Before the fix the no-callback path skipped the
    per-chunk drain and wrapped ``get_final_message()`` in a single
    ``wait_for(idle_timeout_s)``, which measures total wall-clock time and so
    killed a stream that was never actually silent.
    """
    monkeypatch.setenv("NANOINFRA_STREAM_IDLE_TIMEOUT_S", "0.15")
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    chunks = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="a"),
        )
        for _ in range(5)
    ]
    _bind(provider, _ConsumingFakeAsyncStream(chunks, per_chunk_delay=0.06))

    res = await provider.chat_stream(messages=[{"role": "user", "content": "hello"}])

    assert res.finish_reason != "error", (
        f"active stream was killed by total-timeout misuse: {res.content}"
    )
    assert res.finish_reason == "stop"
    assert res.content == "ok"


@pytest.mark.asyncio
async def test_chat_stream_without_callback_still_enforces_idle_timeout(monkeypatch) -> None:
    """A genuinely silent stream must still be cut off by the idle timeout."""
    monkeypatch.setenv("NANOINFRA_STREAM_IDLE_TIMEOUT_S", "0.05")
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    class _StalledStream(_FakeAsyncStream):
        async def __anext__(self) -> SimpleNamespace:
            await asyncio.sleep(3600)
            raise StopAsyncIteration

    _bind(provider, _StalledStream([]))

    res = await provider.chat_stream(messages=[{"role": "user", "content": "hello"}])

    assert res.finish_reason == "error"
    assert res.error_kind == "timeout"
    assert "stalled" in (res.content or "")


@pytest.mark.asyncio
async def test_chat_stream_without_callback_returns_the_unchanged_response() -> None:
    """Draining unconditionally must not change what a callback-less caller gets.

    Every field comes from the final message snapshot, never from the chunks,
    so a caller that passes no callbacks sees the same content, tool calls,
    thinking blocks, finish reason and usage as it did while the drain loop was
    being skipped. The assertion is deliberately whole-response: it is the pin
    that would move if draining ever started contributing to the parse.
    """
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    final = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="pondering", signature="sig"),
            SimpleNamespace(type="text", text="here you go"),
            SimpleNamespace(
                type="tool_use",
                id="toolu_9",
                name="write_file",
                input={"path": "notes.md"},
            ),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=11,
            output_tokens=7,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=3,
        ),
    )
    chunks = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="tool_use", id="toolu_9", name="write_file"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="pondering"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="here you go"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"path":'),
        ),
    ]
    _bind(provider, _ConsumingFakeAsyncStream(chunks, per_chunk_delay=0.0, final=final))

    res = await provider.chat_stream(messages=[{"role": "user", "content": "write"}])

    assert res.content == "here you go"
    assert res.finish_reason == "tool_calls"
    assert res.error_kind is None
    assert [(c.id, c.name, c.arguments) for c in res.tool_calls] == [
        ("toolu_9", "write_file", {"path": "notes.md"})
    ]
    assert res.thinking_blocks == [
        {"type": "thinking", "thinking": "pondering", "signature": "sig"}
    ]
    assert res.usage is not None
    assert (
        res.usage.input_tokens,
        res.usage.output_tokens,
        res.usage.cache_read_tokens,
        res.usage.cache_write_tokens,
    ) == (19, 7, 3, 5)


@pytest.mark.asyncio
async def test_chat_stream_calls_on_content_delta_only_for_text_delta() -> None:
    """Thinking deltas must be consumed without invoking on_content_delta."""
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    chunks = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="think"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="Hi"),
        ),
    ]
    fake = _FakeAsyncStream(chunks)
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=fake)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    provider._client.messages.stream = MagicMock(return_value=stream_cm)

    out: list[str] = []

    async def on_delta(s: str) -> None:
        out.append(s)

    await provider.chat_stream(
        messages=[{"role": "user", "content": "hello"}],
        on_content_delta=on_delta,
        on_thinking_delta=None,
    )

    assert out == ["Hi"]
    fake.get_final_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_stream_invokes_on_thinking_delta_for_thinking_delta() -> None:
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    chunks = [
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="a"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="thinking_delta", thinking="b"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            delta=SimpleNamespace(type="text_delta", text="X"),
        ),
    ]
    fake = _FakeAsyncStream(chunks)
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=fake)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    provider._client.messages.stream = MagicMock(return_value=stream_cm)

    thinking_parts: list[str] = []
    text_parts: list[str] = []

    async def on_thinking(s: str) -> None:
        thinking_parts.append(s)

    async def on_text(s: str) -> None:
        text_parts.append(s)

    await provider.chat_stream(
        messages=[{"role": "user", "content": "hello"}],
        on_content_delta=on_text,
        on_thinking_delta=on_thinking,
    )

    assert thinking_parts == ["a", "b"]
    assert text_parts == ["X"]


@pytest.mark.asyncio
async def test_chat_stream_invokes_tool_call_delta_for_input_json_delta() -> None:
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    chunks = [
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="tool_use",
                id="toolu_1",
                name="write_file",
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json='{"path":"notes.md","content":"',
            ),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json="line\\n"),
        ),
    ]
    fake = _FakeAsyncStream(chunks)
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=fake)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    provider._client.messages.stream = MagicMock(return_value=stream_cm)

    deltas: list[dict] = []

    async def on_tool_delta(delta: dict) -> None:
        deltas.append(delta)

    await provider.chat_stream(
        messages=[{"role": "user", "content": "write"}],
        on_tool_call_delta=on_tool_delta,
    )

    assert deltas == [
        {
            "index": 1,
            "call_id": "toolu_1",
            "name": "write_file",
            "arguments_delta": "",
        },
        {
            "index": 1,
            "call_id": "toolu_1",
            "name": "write_file",
            "arguments_delta": '{"path":"notes.md","content":"',
        },
        {
            "index": 1,
            "call_id": "toolu_1",
            "name": "write_file",
            "arguments_delta": "line\\n",
        },
    ]
    fake.get_final_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_stream_without_callback_still_finalizes() -> None:
    provider = AnthropicProvider(api_key="sk-test")
    provider._client = MagicMock()

    fake = _FakeAsyncStream([])
    fake.get_final_message = AsyncMock(return_value=_final_message_stub("ok"))
    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=fake)
    stream_cm.__aexit__ = AsyncMock(return_value=None)
    provider._client.messages.stream = MagicMock(return_value=stream_cm)

    res = await provider.chat_stream(
        messages=[{"role": "user", "content": "hello"}],
        on_content_delta=None,
    )
    assert res.content == "ok"
    fake.get_final_message.assert_awaited_once()
