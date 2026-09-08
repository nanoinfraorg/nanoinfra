"""The primary's error is the answer when no fallback produced one.

`last_response` was only ever assigned from a *fallback* response, so with every
`_provider_factory` raising -- a fallback preset with no API key is enough -- the chain reached a
synthesized "circuit open and no fallbacks available". That threw away the primary's real error,
including a 429 and its `Retry-After`, and the replacement carried no metadata, so it was dead on
attempt 1.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from nanoinfra.config.schema import ModelPresetConfig
from nanoinfra.providers import fallback_provider as fallback_module
from nanoinfra.providers.base import LLMProvider, LLMResponse
from nanoinfra.providers.fallback_provider import FallbackProvider


class _RateLimitedProvider(LLMProvider):
    """A primary that answers 429 with a `Retry-After` the caller is meant to honour."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def get_default_model(self) -> str:
        return "primary-model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            content="Error: rate limit reached for primary-model",
            finish_reason="error",
            error_status_code=429,
            error_code="rate_limit_exceeded",
            error_retry_after_s=20.0,
        )

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        return await self.chat(**kwargs)


def _unbuildable(_preset: ModelPresetConfig) -> LLMProvider:
    """What a fallback preset with no API key does at construction time."""
    raise RuntimeError("no api key configured for fallback preset")


def _preset(model: str = "fallback-model") -> ModelPresetConfig:
    return ModelPresetConfig(model=model, provider="custom")


@pytest.mark.asyncio
async def test_the_primary_429_survives_a_chain_that_built_nothing() -> None:
    primary = _RateLimitedProvider()
    provider = FallbackProvider(primary, [_preset()], _unbuildable)

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_status_code == 429, "the primary's own error, not a synthesized one"
    assert response.error_retry_after_s == 20.0, "including the wait the provider asked for"
    assert response.error_code == "rate_limit_exceeded"
    assert LLMProvider.is_transient_response(response) is True


@pytest.mark.asyncio
async def test_the_synthesized_error_is_retryable_and_says_when() -> None:
    """Only reachable with the circuit open *and* nothing buildable, and even then it is retried."""
    primary = _RateLimitedProvider()
    provider = FallbackProvider(primary, [_preset()], _unbuildable)
    provider._primary_tripped_at = time.monotonic()

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert primary.calls == 0, "the circuit is open, so the primary is not called at all"
    assert response.finish_reason == "error"
    assert response.error_should_retry is True
    assert response.error_retry_after_s is not None
    assert 0 < response.error_retry_after_s <= fallback_module._PRIMARY_COOLDOWN_S
    assert LLMProvider.is_transient_response(response) is True


@pytest.mark.asyncio
async def test_a_fallback_error_still_wins_over_the_primary_one() -> None:
    """The last error seen is still the one returned; this only fills the gap where there was none."""
    primary = _RateLimitedProvider()

    class _FailingFallback(LLMProvider):
        def get_default_model(self) -> str:
            return "fallback-model"

        async def chat(self, **kwargs: Any) -> LLMResponse:
            return LLMResponse(
                content="Error: fallback is overloaded",
                finish_reason="error",
                error_status_code=503,
            )

        async def chat_stream(self, **kwargs: Any) -> LLMResponse:
            return await self.chat(**kwargs)

    provider = FallbackProvider(primary, [_preset()], lambda _preset: _FailingFallback())

    response = await provider.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.error_status_code == 503
