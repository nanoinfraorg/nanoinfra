"""A provider that raises must still reach the fallback chain.

Both live triggers raise from *outside* the provider's own `try`: the Copilot token refresh
(`github_copilot_provider.py`) and `_ensure_client()` (`openai_compat_provider.py`). The exception
used to unwind past `_try_with_fallback` to `_safe_chat`, which produced an error response with no
metadata at all -- so no fallback was tried, and the retry loop, left with only a substring to
match, gave up on the first attempt too.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from nanoinfra.config.schema import ModelPresetConfig
from nanoinfra.providers.base import LLMProvider, LLMResponse
from nanoinfra.providers.fallback_provider import FallbackProvider
from nanoinfra.providers.openai_compat_provider import OpenAICompatProvider

_NOT_LOGGED_IN = (
    "GitHub Copilot is not logged in. Run: nanoinfra provider login github-copilot"
)


class _RaisingProvider(LLMProvider):
    """A provider whose call raises before it can return anything.

    `GitHubCopilotProvider.chat` awaits `_refresh_client_api_key()` before `super().chat()`, and
    `OpenAICompatProvider.chat` awaits `_ensure_client()` before its own `try` -- so this is the
    real shape, not a contrived one.
    """

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self._exc = exc
        self.calls = 0

    def get_default_model(self) -> str:
        return "primary-model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        raise self._exc

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        return await self.chat(**kwargs)


class _AnsweringProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "fallback-model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(content="answered by the fallback")

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        return await self.chat(**kwargs)


def _preset(model: str = "fallback-model") -> ModelPresetConfig:
    return ModelPresetConfig(model=model, provider="custom")


# --- the chain -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_raised_exception_reaches_the_fallback() -> None:
    """The Copilot 'not logged in' path: our own policy says fall back, so it must get to."""
    primary = _RaisingProvider(RuntimeError(_NOT_LOGGED_IN))
    fallback = _AnsweringProvider()
    provider = FallbackProvider(primary, [_preset()], lambda _preset: fallback)

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert primary.calls == 1
    assert fallback.calls == 1, "the exception skipped the whole chain"
    assert response.content == "answered by the fallback"
    assert response.finish_reason == "stop"


@pytest.mark.asyncio
async def test_a_raised_exception_is_classified_before_the_retry_loop_sees_it() -> None:
    """With no fallback left, the error still carries the metadata the retry policy reads."""
    primary = _RaisingProvider(httpx.ConnectError("[Errno 111] Connection refused"))
    fallback = _RaisingProvider(httpx.ReadTimeout("timed out"))
    provider = FallbackProvider(primary, [_preset()], lambda _preset: fallback)

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_kind == "timeout", "the last error seen was the fallback's"
    assert LLMProvider.is_transient_response(response) is True


@pytest.mark.asyncio
async def test_a_streamed_call_that_raises_also_fails_over() -> None:
    primary = _RaisingProvider(RuntimeError(_NOT_LOGGED_IN))
    fallback = _AnsweringProvider()
    provider = FallbackProvider(primary, [_preset()], lambda _preset: fallback)

    response = await provider.chat_stream(messages=[{"role": "user", "content": "hi"}])

    assert fallback.calls == 1
    assert response.content == "answered by the fallback"


# --- the classifier ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected_kind"),
    [
        (httpx.ConnectError("refused"), "connection"),
        (httpx.ReadError("reset"), "connection"),
        (httpx.RemoteProtocolError("server disconnected"), "connection"),
        (httpx.ConnectTimeout("connect timed out"), "timeout"),
        (httpx.ReadTimeout("read timed out"), "timeout"),
        (httpx.PoolTimeout("pool timed out"), "timeout"),
    ],
)
def test_real_httpx_exceptions_classify_as_transient(
    exc: Exception, expected_kind: str
) -> None:
    response = FallbackProvider._error_response_from_exception(exc)

    assert response.finish_reason == "error"
    assert response.error_kind == expected_kind
    assert LLMProvider.is_transient_response(response) is True
    assert FallbackProvider._should_fallback(response) is True


def test_an_exception_with_no_message_still_says_what_it_was() -> None:
    """`ReadError()` stringifies to nothing, and 'Error calling LLM: ' classifies as nothing."""
    response = FallbackProvider._error_response_from_exception(httpx.ReadError(""))

    assert response.content is not None
    assert "ReadError" in response.content
    assert response.error_kind == "connection"


def test_a_status_carrying_exception_keeps_its_status_and_retry_after() -> None:
    request = httpx.Request("POST", "https://api.example.invalid/v1/chat/completions")
    raw = httpx.Response(
        429,
        headers={"retry-after": "20"},
        json={"error": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}},
        request=request,
    )
    exc = httpx.HTTPStatusError("429 Too Many Requests", request=request, response=raw)

    response = FallbackProvider._error_response_from_exception(exc)

    assert response.error_status_code == 429
    assert response.error_retry_after_s == 20
    assert response.error_code == "rate_limit_exceeded"
    assert LLMProvider.is_transient_response(response) is True


def test_not_logged_in_is_a_fallbackable_error() -> None:
    """`github_copilot_provider.py` emits exactly this text; the token list did not have it."""
    response = LLMResponse(
        content=f"Error calling LLM: RuntimeError: {_NOT_LOGGED_IN}",
        finish_reason="error",
    )

    assert FallbackProvider._should_fallback(response) is True


# --- the provider that reports the metadata ------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ReadError("reset"),
        httpx.RemoteProtocolError("server disconnected without sending a response"),
    ],
)
def test_transport_exceptions_are_not_reported_as_unclassified(exc: Exception) -> None:
    """Matching the immediate class name against 'connection' matched none of these three."""
    metadata = OpenAICompatProvider._extract_error_metadata(exc)

    assert metadata["error_kind"] == "connection"
    assert LLMProvider.is_transient_response(OpenAICompatProvider._handle_error(exc)) is True
