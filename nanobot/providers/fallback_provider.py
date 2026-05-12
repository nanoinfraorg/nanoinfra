"""Provider wrapper that transparently fails over to fallback models on error."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanobot.providers.base import LLMProvider, LLMResponse

# Circuit breaker tuned to match OpenAICompatProvider's Responses API breaker.
_PRIMARY_FAILURE_THRESHOLD = 3
_PRIMARY_COOLDOWN_S = 60


class FallbackProvider(LLMProvider):
    """Wrap a primary provider and transparently failover to fallback models.

    When the primary model returns an error and no content has been streamed yet,
    the wrapper tries each fallback model in order.  Each fallback model may
    reside on a different provider — a factory callable creates the underlying
    provider on-the-fly.

    Key design:
    - Failover is request-scoped (the wrapper itself is stateless between turns).
    - Skipped when content was already streamed to avoid duplicate output.
    - Recursive failover is prevented by the factory returning plain providers.
    - Primary provider is circuit-broken after repeated failures to avoid
      wasting requests on a known-bad endpoint.
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback_models: list[str],
        provider_factory: Callable[[str], LLMProvider],
    ):
        self._primary = primary
        self._fallback_models = list(fallback_models)
        self._provider_factory = provider_factory
        self._has_fallbacks = bool(fallback_models)
        self._primary_failures = 0
        self._primary_tripped_at: float | None = None

    @property
    def generation(self):
        return self._primary.generation

    @generation.setter
    def generation(self, value):
        self._primary.generation = value

    def get_default_model(self) -> str:
        return self._primary.get_default_model()

    def _primary_available(self) -> bool:
        """Return True if the primary provider is not currently tripped."""
        if self._primary_tripped_at is None:
            return True
        if time.monotonic() - self._primary_tripped_at >= _PRIMARY_COOLDOWN_S:
            # Half-open: allow one probe attempt.
            return True
        return False

    async def chat(self, **kwargs: Any) -> LLMResponse:
        if not self._has_fallbacks:
            return await self._primary.chat(**kwargs)
        return await self._try_with_fallback(
            lambda p, kw: p.chat(**kw), kwargs, has_streamed=None
        )

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        if not self._has_fallbacks:
            return await self._primary.chat_stream(**kwargs)

        has_streamed: list[bool] = [False]
        original_delta = kwargs.get("on_content_delta")

        async def _tracking_delta(text: str) -> None:
            if text:
                has_streamed[0] = True
            if original_delta:
                await original_delta(text)

        kwargs["on_content_delta"] = _tracking_delta
        return await self._try_with_fallback(
            lambda p, kw: p.chat_stream(**kw), kwargs, has_streamed=has_streamed
        )

    async def _try_with_fallback(
        self,
        call: Callable[[LLMProvider, dict[str, Any]], Awaitable[LLMResponse]],
        kwargs: dict[str, Any],
        has_streamed: list[bool] | None,
    ) -> LLMResponse:
        primary_model = kwargs.get("model") or self._primary.get_default_model()

        if self._primary_available():
            response = await call(self._primary, kwargs)
            if response.finish_reason != "error":
                self._primary_failures = 0
                self._primary_tripped_at = None
                return response

            if has_streamed is not None and has_streamed[0]:
                logger.warning(
                    "Primary model error but content already streamed; skipping failover"
                )
                return response

            self._primary_failures += 1
            if self._primary_failures >= _PRIMARY_FAILURE_THRESHOLD:
                self._primary_tripped_at = time.monotonic()
                logger.warning(
                    "Primary model '{}' circuit open after {} consecutive failures",
                    primary_model, self._primary_failures,
                )
        else:
            logger.debug("Primary model '{}' circuit open; skipping", primary_model)

        last_response: LLMResponse | None = None
        primary_skipped = not self._primary_available()
        for idx, fallback_model in enumerate(self._fallback_models):
            if has_streamed is not None and has_streamed[0]:
                break
            if idx == 0 and primary_skipped:
                logger.info(
                    "Primary model '{}' circuit open, trying fallback '{}'",
                    primary_model, fallback_model,
                )
            elif idx == 0:
                logger.info(
                    "Primary model '{}' failed, trying fallback '{}'",
                    primary_model, fallback_model,
                )
            else:
                logger.info(
                    "Fallback '{}' also failed, trying next fallback '{}'",
                    self._fallback_models[idx - 1], fallback_model,
                )
            try:
                fallback_provider = self._provider_factory(fallback_model)
            except Exception as exc:
                logger.warning(
                    "Failed to create provider for fallback '{}': {}", fallback_model, exc
                )
                continue

            original_model = kwargs.get("model")
            kwargs["model"] = fallback_model
            try:
                fallback_response = await call(fallback_provider, kwargs)
            finally:
                if original_model is not None:
                    kwargs["model"] = original_model
                else:
                    kwargs.pop("model", None)

            if fallback_response.finish_reason != "error":
                logger.info(
                    "Fallback '{}' succeeded after primary '{}' failed",
                    fallback_model, primary_model,
                )
                return fallback_response

            last_response = fallback_response
            logger.warning(
                "Fallback '{}' also failed: {}",
                fallback_model,
                (fallback_response.content or "")[:120],
            )

        logger.warning(
            "All {} fallback model(s) failed",
            len(self._fallback_models),
        )
        # Return the last error response we saw (primary or last fallback).
        if last_response is not None:
            return last_response
        # Primary was tripped and we have no fallbacks — synthesize an error.
        return LLMResponse(
            content=f"Primary model '{primary_model}' circuit open and no fallbacks available",
            finish_reason="error",
        )
