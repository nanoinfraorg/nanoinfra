"""Provider wrapper that transparently fails over to fallback models on error."""

# pyright: reportIncompatibleVariableOverride=false

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import replace
from typing import Any

from loguru import logger

from nanoinfra.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    ProviderCallContext,
    ProviderConversationState,
)

# Circuit breaker tuned to match OpenAICompatProvider's Responses API breaker.
_PRIMARY_FAILURE_THRESHOLD = 3
_PRIMARY_COOLDOWN_S = 60
_FALLBACK_ERROR_KINDS = frozenset({
    "timeout",
    "connection",
    "server_error",
    "rate_limit",
    "overloaded",
})
_AUTHENTICATION_ERROR_KINDS = frozenset({
    "authentication",
    "auth",
    "permission",
})
_AUTHENTICATION_ERROR_TOKENS = (
    "authentication_error",
    "authentication error",
    "invalid_api_key",
    "invalid api key",
    "incorrect_api_key",
    "incorrect api key",
    "expired_api_key",
    "expired api key",
    "invalid credential",
    "expired credential",
    "credential has expired",
    "credentials have expired",
    "invalid_token",
    "invalid token",
    "expired_token",
    "expired token",
    "unauthorized",
    # What an unauthenticated OAuth provider says instead of returning a 401:
    # `github_copilot_provider.py` raises "GitHub Copilot is not logged in" from its token
    # exchange, and a credential the operator has to renew is exactly the case worth failing over.
    "not logged in",
    "permission_denied",
    "permission denied",
    "access_denied",
    "account_deactivated",
    "organization_deactivated",
)
_NON_FALLBACK_ERROR_KINDS = frozenset({
    "content_filter",
    "refusal",
    "context_length",
    "invalid_request",
})
_FALLBACK_ERROR_TOKENS = (
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "too many requests",
    "overloaded",
    "server_error",
    "server error",
    "temporarily unavailable",
    "timeout",
    "timed out",
    "connection",
    "empty",  # API returned empty choices (e.g. DeepSeek peak hours), transient
    "insufficient_quota",
    "insufficient quota",
    "quota_exceeded",
    "quota exceeded",
    "quota_exhausted",
    "quota exhausted",
    "billing_hard_limit",
    "insufficient_balance",
    "balance",
    "out of credits",
)


FallbackModelObserver = Callable[[str], Awaitable[None]]


#: The leaf provider serving the call in this task, for the telemetry row (#176).
#:
#: Set around each delegation rather than stored on the instance, so two turns in
#: flight cannot label each other's rows.
_SERVING_PROVIDER: ContextVar[str] = ContextVar("nanoinfra_serving_provider", default="")


class FallbackProvider(LLMProvider):
    """Wrap a primary provider and transparently failover to fallback models.

    When the primary model returns a fallbackable error before content has been
    streamed, the wrapper tries each fallback model in order. Streamed timeout
    errors are the recovery exception: the caller may close the current stream
    segment, then the wrapper continues failover with later deltas in a new
    segment. Each fallback model may reside on a different provider — a factory
    callable creates the underlying provider on-the-fly.

    Key design:
    - Failover is request-scoped (the wrapper itself is stateless between turns).
    - Skipped when content was already streamed to avoid duplicate output,
      except timeout recovery can resume in a new stream segment.
    - Recursive failover is prevented by the factory returning plain providers.
    - Primary provider is circuit-broken after repeated failures to avoid
      wasting requests on a known-bad endpoint.
    """

    supports_stream_recover_callback = True

    def __init__(
        self,
        primary: LLMProvider,
        fallback_presets: list[Any],
        provider_factory: Callable[[Any], LLMProvider],
        fallback_model_observer: FallbackModelObserver | None = None,
        primary_context_window_tokens: int | None = None,
    ):
        self._primary = primary
        self._fallback_presets = list(fallback_presets)
        self._provider_factory = provider_factory
        self._fallback_model_observer = fallback_model_observer
        self._primary_context_window_tokens = primary_context_window_tokens
        self._has_fallbacks = bool(fallback_presets)
        self._primary_failures = 0
        self._primary_tripped_at: float | None = None

    @property
    def generation(self) -> GenerationSettings:
        return self._primary.generation

    @generation.setter
    def generation(self, value: GenerationSettings) -> None:
        self._primary.generation = value

    def get_default_model(self) -> str:
        return self._primary.get_default_model()

    def set_fallback_model_observer(self, observer: FallbackModelObserver | None) -> None:
        """Attach a process-level observer without changing request call signatures."""
        self._fallback_model_observer = observer

    def observed_provider_name(self) -> str:
        """The leaf that served the last call in *this* task, not this wrapper's name.

        A contextvar rather than an instance field: two turns can be mid-call at
        once, and an instance field would let one of them label the other's row.
        Each turn runs in its own Task, so each gets its own copy.

        Unset means the primary answered, which is the case a bare `chat()` with no
        fallbacks configured takes.
        """
        served = _SERVING_PROVIDER.get()
        return served or self._primary.observed_provider_name()

    def set_llm_call_observer(self, observer: Callable[[Any], None] | None) -> None:
        """Attach the usage observer to this wrapper *and* to the leaves that do the calling.

        This class makes no provider calls of its own -- it delegates to a primary and, on
        failure, to a provider the factory builds. Setting it only here would record nothing at
        all, and setting it only on the primary would lose exactly the calls a fallback made,
        which are the ones worth knowing about (#176).
        """
        super().set_llm_call_observer(observer)
        self._primary.set_llm_call_observer(observer)

    @property
    def supports_progress_deltas(self) -> bool:
        return bool(getattr(self._primary, "supports_progress_deltas", False))

    def can_resume_conversation_state(
        self,
        state: ProviderConversationState,
        model: str | None = None,
    ) -> bool:
        return self._primary.can_resume_conversation_state(state, model)

    def supports_native_compaction(self, model: str | None = None) -> bool:
        return self._primary.supports_native_compaction(model)

    def _primary_call_context(
        self,
        provider_context: ProviderCallContext,
        model: str | None,
    ) -> ProviderCallContext:
        context_window_tokens = (
            self._primary_context_window_tokens
            if self._primary_context_window_tokens is not None
            else provider_context.context_window_tokens
        )
        if not self._primary.supports_native_compaction(model):
            context_window_tokens = None
        return ProviderCallContext(
            conversation_state=provider_context.conversation_state,
            context_window_tokens=context_window_tokens,
        )

    def _primary_cooldown_remaining(self) -> float | None:
        """Seconds until the primary is probed again, or None when it is not held back.

        One definition, because two things read it now: the decision to skip the primary, and the
        `Retry-After` on the error returned when nothing else could answer either.
        """
        if self._primary_tripped_at is None:
            return None
        remaining = _PRIMARY_COOLDOWN_S - (time.monotonic() - self._primary_tripped_at)
        return remaining if remaining > 0 else None

    def _primary_available(self) -> bool:
        """Return True if the primary provider is not currently tripped."""
        # Half-open once the cooldown has elapsed: one probe attempt is allowed.
        return self._primary_cooldown_remaining() is None

    async def chat(self, **kwargs: Any) -> LLMResponse:  # pyright: ignore[reportIncompatibleMethodOverride]
        if not self._has_fallbacks:
            return await self._primary.chat(**kwargs)
        return await self._try_with_fallback(
            lambda p, kw: p.chat(**kw), kwargs, has_streamed=None
        )

    async def chat_with_context(
        self,
        *,
        provider_context: ProviderCallContext,
        **kwargs: Any,
    ) -> LLMResponse:
        call_kwargs: dict[str, Any] = dict(kwargs)
        call_kwargs["provider_context"] = self._primary_call_context(
            provider_context,
            kwargs.get("model"),
        )
        if not self._has_fallbacks:
            return await self._primary.chat_with_context(**call_kwargs)
        return await self._try_with_fallback(
            lambda p, kw: p.chat_with_context(**kw),
            call_kwargs,
            has_streamed=None,
        )

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:  # pyright: ignore[reportIncompatibleMethodOverride]
        on_stream_recover = kwargs.pop("on_stream_recover", None)
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
            lambda p, kw: p.chat_stream(**kw),
            kwargs,
            has_streamed=has_streamed,
            on_stream_recover=on_stream_recover,
        )

    async def chat_stream_with_context(
        self,
        *,
        provider_context: ProviderCallContext,
        **kwargs: Any,
    ) -> LLMResponse:
        on_stream_recover = kwargs.pop("on_stream_recover", None)
        call_kwargs: dict[str, Any] = dict(kwargs)
        call_kwargs["provider_context"] = self._primary_call_context(
            provider_context,
            kwargs.get("model"),
        )
        if not self._has_fallbacks:
            return await self._primary.chat_stream_with_context(**call_kwargs)

        has_streamed: list[bool] = [False]
        original_delta = call_kwargs.get("on_content_delta")

        async def _tracking_delta(text: str) -> None:
            if text:
                has_streamed[0] = True
            if original_delta:
                await original_delta(text)

        call_kwargs["on_content_delta"] = _tracking_delta
        return await self._try_with_fallback(
            lambda p, kw: p.chat_stream_with_context(**kw),
            call_kwargs,
            has_streamed=has_streamed,
            on_stream_recover=on_stream_recover,
        )

    async def _try_with_fallback(
        self,
        call: Callable[[LLMProvider, dict[str, Any]], Awaitable[LLMResponse]],
        kwargs: dict[str, Any],
        has_streamed: list[bool] | None,
        on_stream_recover: Callable[[], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        primary_model = kwargs.get("model") or self._primary.get_default_model()
        primary_was_attempted = False
        primary_error = "unknown error"
        # Kept for the end of the chain: if no fallback returns a response, the primary's error is
        # the only real one anybody saw, and it carries the status code and the `Retry-After` that
        # decide what happens next.
        primary_response: LLMResponse | None = None
        # A primary error eligible for failover did not return a replacement
        # continuation, so the incoming primary state remains reusable.
        preserve_primary_state = True

        if self._primary_available():
            primary_was_attempted = True
            # Set and deliberately not reset: the row is written after this returns, in the
            # wrapper's own retry loop, so a value cleared on the way out would be gone by the
            # time the observer reads it -- which is exactly how this first went wrong. Every
            # call sets it before delegating, and each turn is its own Task with its own
            # context, so nothing leaks between two of them.
            _SERVING_PROVIDER.set(self._primary.observed_provider_name())
            try:
                response = await call(self._primary, kwargs)
            except Exception as exc:
                # Classified here rather than allowed to unwind: an exception that leaves this
                # method has left the chain, and the fallback the operator configured is never
                # asked. `CancelledError` is a `BaseException`, so a cancelled turn still
                # cancels.
                logger.warning(
                    "Primary model '{}' raised {}; treating it as a failover-eligible error",
                    primary_model,
                    type(exc).__name__,
                )
                response = self._error_response_from_exception(exc)
            if response.finish_reason != "error":
                self._primary_failures = 0
                self._primary_tripped_at = None
                return response
            primary_error = (response.content or primary_error)[:120]
            primary_response = response

            if has_streamed is not None and has_streamed[0]:
                is_timeout = (response.error_kind or "").lower() == "timeout"
                if is_timeout:
                    logger.warning(
                        "Primary model '{}' stream stalled after content was emitted; "
                        "attempting failover anyway",
                        primary_model,
                    )
                    has_streamed[0] = False
                    if on_stream_recover:
                        await on_stream_recover()
                    else:
                        kwargs["on_content_delta"] = None
                else:
                    logger.warning(
                        "Primary model error but content already streamed; skipping failover"
                    )
                    return response

            if not self._should_fallback(response):
                logger.warning(
                    "Primary model '{}' returned non-fallbackable error: {}",
                    primary_model,
                    (response.content or "")[:120],
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

        last_response: LLMResponse | None = primary_response
        primary_skipped = not primary_was_attempted
        for idx, fallback in enumerate(self._fallback_presets):
            fallback_model = fallback.model
            if has_streamed is not None and has_streamed[0]:
                is_timeout = (
                    last_response is not None
                    and (last_response.error_kind or "").lower() == "timeout"
                )
                if is_timeout and on_stream_recover:
                    logger.warning(
                        "Fallback model '{}' stream stalled after content was emitted; "
                        "starting a new stream segment and trying next fallback",
                        self._fallback_presets[idx - 1].model if idx > 0 else primary_model,
                    )
                    has_streamed[0] = False
                    await on_stream_recover()
                else:
                    break
            if idx == 0 and primary_skipped:
                logger.info(
                    "Primary model '{}' circuit open, trying fallback '{}'",
                    primary_model, fallback_model,
                )
            elif idx == 0:
                logger.info(
                    "Primary model '{}' failed: {}; trying fallback '{}'",
                    primary_model, primary_error, fallback_model,
                )
            else:
                logger.info(
                    "Fallback '{}' also failed, trying next fallback '{}'",
                    self._fallback_presets[idx - 1].model, fallback_model,
                )
            try:
                fallback_provider = self._provider_factory(fallback)
            except Exception as exc:
                logger.warning(
                    "Failed to create provider for fallback '{}': {}", fallback_model, exc
                )
                continue
            # Built after the observer was attached, so it has to be told. Outside the `try`
            # above on purpose: a problem here is not a factory failure and must not be reported
            # as one.
            fallback_provider.set_llm_call_observer(self._llm_call_observer)

            await self._notify_fallback_model(fallback_model)

            fallback_kwargs = {
                **kwargs,
                "model": fallback_model,
                "max_tokens": fallback.max_tokens,
                "temperature": fallback.temperature,
            }
            provider_context = fallback_kwargs.get("provider_context")
            if isinstance(provider_context, ProviderCallContext):
                state = provider_context.conversation_state
                if state is not None and not fallback_provider.can_resume_conversation_state(
                    state,
                    fallback_model,
                ):
                    state = None
                context_window_tokens = (
                    fallback.context_window_tokens
                    if fallback_provider.supports_native_compaction(fallback_model)
                    else None
                )
                fallback_kwargs["provider_context"] = ProviderCallContext(
                    conversation_state=state,
                    context_window_tokens=context_window_tokens,
                )
            if fallback.reasoning_effort is None:
                fallback_kwargs.pop("reasoning_effort", None)
            else:
                fallback_kwargs["reasoning_effort"] = fallback.reasoning_effort
            _SERVING_PROVIDER.set(fallback_provider.observed_provider_name())
            try:
                fallback_response = await call(fallback_provider, fallback_kwargs)
            except Exception as exc:
                # Same reason as the primary above, and one more: a fallback that raises must not
                # take the *remaining* fallbacks down with it.
                logger.warning(
                    "Fallback '{}' raised {}; trying the next one",
                    fallback_model,
                    type(exc).__name__,
                )
                fallback_response = self._error_response_from_exception(exc)

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
            len(self._fallback_presets),
        )
        # Return the last error response we saw (primary or last fallback).
        if last_response is not None:
            return replace(
                last_response,
                preserve_provider_state_on_error=preserve_primary_state,
            )
        # Nothing ever answered: the circuit was open, so the primary was not called, and no
        # fallback could be built. Retryable with the remaining cooldown as the wait, because the
        # thing that stopped this call is a clock -- a caller told this was terminal would fail a
        # turn that would have succeeded a minute later. No `error_kind`: the usage store's
        # vocabulary has no term for "we declined to send this", and a kind it would normalise to
        # `other` labels a metrics row with a guess.
        cooldown_remaining = self._primary_cooldown_remaining()
        return LLMResponse(
            content=f"Primary model '{primary_model}' circuit open and no fallbacks available",
            finish_reason="error",
            preserve_provider_state_on_error=preserve_primary_state,
            error_should_retry=True,
            error_retry_after_s=cooldown_remaining,
        )

    async def _notify_fallback_model(self, model: str) -> None:
        if self._fallback_model_observer is None:
            return
        try:
            await self._fallback_model_observer(model)
        except Exception:
            logger.exception("fallback model observer failed for '{}'", model)

    @staticmethod
    def _error_response_from_exception(exc: Exception) -> LLMResponse:
        """Turn a raised provider exception into a classified error response.

        A provider that raises rather than returns used to unwind straight past this wrapper to
        `_safe_chat`, which produced `LLMResponse(content="Error calling LLM: ...")` with no
        metadata at all: the fallback was never tried, and the retry loop had nothing but a
        substring to classify on, so it was not retried either. Both live triggers raise from
        *outside* the provider's own `try` -- the Copilot token refresh, and `_ensure_client()` in
        the OpenAI-compatible provider -- so this is the ordinary shape of a bad credential or an
        unreachable endpoint, not an exotic one.

        Deliberately unable to raise: it is the conversion of a failure, and a failure here would
        replace an answerable error with an unanswerable one.
        """
        response = getattr(exc, "response", None)
        headers: Any = None
        payload: Any = None
        # Neither lookup may be trusted to merely return -- `.text` on an unread streaming
        # response raises -- and losing a header is a smaller loss than losing the error.
        with suppress(Exception):
            headers = getattr(response, "headers", None)
        with suppress(Exception):
            payload = (
                getattr(exc, "body", None)
                or getattr(exc, "doc", None)
                or getattr(response, "text", None)
            )
        error_type, error_code = LLMProvider._extract_error_type_code(payload)

        status_value = getattr(exc, "status_code", None)
        if status_value is None:
            status_value = getattr(response, "status_code", None)
        status_code: int | None = None
        if status_value is not None:
            try:
                status_code = int(status_value)
            except (TypeError, ValueError):
                status_code = None

        # `str(exc)`, and the class name when that is empty: `httpx.ReadError()` stringifies to
        # nothing, and "Error calling LLM: " with an empty tail is an error message that names no
        # error -- unreadable in a log and unmatchable by the text classifiers below.
        detail = str(exc).strip()
        message = f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__
        retry_after = LLMProvider._extract_retry_after_from_headers(headers)
        if retry_after is None:
            retry_after = LLMProvider._extract_retry_after(message)
        return LLMResponse(
            content=f"Error calling LLM: {message}",
            finish_reason="error",
            error_status_code=status_code,
            error_kind=LLMProvider.error_kind_from_exception(exc),
            error_type=error_type,
            error_code=error_code,
            error_retry_after_s=retry_after,
        )

    @staticmethod
    def _should_fallback(response: LLMResponse) -> bool:
        if LLMProvider.is_arrearage_response(response):
            return True
        status = response.error_status_code
        kind = (response.error_kind or "").lower()
        error_type = (response.error_type or "").lower()
        code = (response.error_code or "").lower()
        text = (response.content or "").lower()
        structured_values = (kind, error_type, code)

        if kind in _AUTHENTICATION_ERROR_KINDS:
            return True
        if any(
            token in value
            for value in structured_values
            for token in _AUTHENTICATION_ERROR_TOKENS
        ):
            return True
        if kind in _NON_FALLBACK_ERROR_KINDS:
            return False
        if any(
            token in value
            for value in structured_values
            for token in _NON_FALLBACK_ERROR_KINDS
        ):
            return False
        if status in {401, 403}:
            return True
        if any(token in text for token in _AUTHENTICATION_ERROR_TOKENS):
            return True
        if response.error_should_retry is False:
            return False
        if status in {400, 404, 422}:
            return False
        if response.error_should_retry is True:
            return True
        if status is not None and (status in {408, 409, 429} or 500 <= status <= 599):
            return True
        if kind in _FALLBACK_ERROR_KINDS:
            return True
        return any(token in value for value in (kind, error_type, code, text) for token in _FALLBACK_ERROR_TOKENS)
