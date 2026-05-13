"""Tests for FallbackProvider model failover."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.config.schema import ModelFallbackConfig
from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.fallback_provider import FallbackProvider


def _make_response(
    content: str = "ok",
    finish_reason: str = "stop",
    *,
    error_kind: str | None = None,
) -> LLMResponse:
    return LLMResponse(content=content, finish_reason=finish_reason, error_kind=error_kind)


def _error_response(content: str = "api error") -> LLMResponse:
    return _make_response(content, finish_reason="error", error_kind="server_error")


def _fallback(
    model: str,
    provider: str = "fallback",
    *,
    max_tokens: int | None = None,
    context_window_tokens: int | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
) -> ModelFallbackConfig:
    return ModelFallbackConfig(
        model=model,
        provider=provider,
        max_tokens=max_tokens,
        context_window_tokens=context_window_tokens,
        temperature=temperature,
        reasoning_effort=reasoning_effort,
    )


class _FakeProvider(LLMProvider):
    """Fake provider for testing."""

    def __init__(self, name: str = "fake", response: LLMResponse | None = None):
        super().__init__()
        self.name = name
        self._response = response or _make_response()
        self.chat_calls: list[dict[str, Any]] = []
        self.chat_stream_calls: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return f"{self.name}/model"

    async def chat(self, **kwargs: Any) -> LLMResponse:
        self.chat_calls.append(dict(kwargs))
        return self._response

    async def chat_stream(self, **kwargs: Any) -> LLMResponse:
        self.chat_stream_calls.append(dict(kwargs))
        on_delta = kwargs.get("on_content_delta")
        if on_delta and self._response.content:
            await on_delta(self._response.content)
        return self._response


# -- config-level tests --


def test_fallback_models_default_empty() -> None:
    from nanobot.config.schema import ModelPresetConfig
    p = ModelPresetConfig(model="test/model")
    assert p.fallback_models == []


def test_fallback_models_accepts_list() -> None:
    from nanobot.config.schema import ModelPresetConfig
    p = ModelPresetConfig(
        model="test/primary",
        fallback_models=[{"provider": "test", "model": "test/a"}],
    )
    assert p.fallback_models == [_fallback("test/a", provider="test")]


def test_fallback_models_from_camel_case() -> None:
    from nanobot.config.schema import ModelPresetConfig
    p = ModelPresetConfig.model_validate({
        "model": "test/primary",
        "fallbackModels": [{"provider": "test", "model": "test/a"}],
    })
    assert p.fallback_models == [_fallback("test/a", provider="test")]


def test_provider_signature_tracks_fallback_models_and_provider_config() -> None:
    from nanobot.config.schema import Config
    from nanobot.providers.factory import provider_signature

    base = {
        "modelPresets": {
            "prod": {
                "model": "openai/gpt-4.1",
                "fallbackModels": [
                    {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-6"}
                ],
            }
        },
        "providers": {
            "openai": {"apiKey": "primary-key"},
            "anthropic": {"apiKey": "fallback-key"},
        },
    }
    changed_fallback = {
        **base,
        "modelPresets": {
            "prod": {
                "model": "openai/gpt-4.1",
                "fallbackModels": [{"provider": "deepseek", "model": "deepseek/deepseek-chat"}],
            }
        },
        "providers": {
            **base["providers"],
            "deepseek": {"apiKey": "deepseek-key"},
        },
    }
    changed_key = {
        **base,
        "providers": {
            "openai": {"apiKey": "primary-key"},
            "anthropic": {"apiKey": "new-fallback-key"},
        },
    }

    signature = provider_signature(Config.model_validate(base), preset_name="prod")

    assert signature != provider_signature(Config.model_validate(changed_fallback), preset_name="prod")
    assert signature != provider_signature(Config.model_validate(changed_key), preset_name="prod")


def test_agent_defaults_can_define_fallback_models() -> None:
    from nanobot.config.schema import Config

    config = Config.model_validate({
        "agents": {
            "defaults": {
                "model": "primary-model",
                "provider": "custom",
                "fallbackModels": [{"provider": "deepseek", "model": "deepseek-v4-pro"}],
            }
        }
    })

    assert config.resolve_preset().fallback_models == [
        _fallback("deepseek-v4-pro", provider="deepseek")
    ]


def test_provider_snapshot_uses_smallest_fallback_context_window() -> None:
    from nanobot.config.schema import Config
    from nanobot.providers.factory import build_provider_snapshot

    config = Config.model_validate({
        "modelPresets": {
            "prod": {
                "model": "openai/gpt-4.1",
                "provider": "openai",
                "contextWindowTokens": 128000,
                "fallbackModels": [
                    {
                        "provider": "deepseek",
                        "model": "deepseek/deepseek-chat",
                        "contextWindowTokens": 64000,
                    }
                ],
            }
        },
        "providers": {
            "openai": {"apiKey": "primary-key"},
            "deepseek": {"apiKey": "fallback-key"},
        },
    })

    snapshot = build_provider_snapshot(config, preset_name="prod")

    assert snapshot.context_window_tokens == 64000


# -- FallbackProvider tests --


class TestNoFallbackWhenPrimarySucceeds:
    @pytest.mark.asyncio
    async def test(self) -> None:
        primary = _FakeProvider("primary", _make_response("primary ok"))
        factory = MagicMock()
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "primary ok"
        assert result.finish_reason == "stop"
        factory.assert_not_called()


class TestFallbackOnPrimaryError:
    @pytest.mark.asyncio
    async def test_first_fallback_succeeds(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        fallback = _FakeProvider("fallback", _make_response("fallback ok"))
        factory = MagicMock(return_value=fallback)

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}], model="primary-model")
        assert result.content == "fallback ok"
        assert result.finish_reason == "stop"
        factory.assert_called_once_with(_fallback("fallback-a"))
        assert primary.chat_calls[0]["model"] == "primary-model"
        assert fallback.chat_calls[0]["model"] == "fallback-a"


class TestNoFallbackWhenContentStreamed:
    @pytest.mark.asyncio
    async def test(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        factory = MagicMock()
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        async def _delta(text: str) -> None:
            pass

        result = await fb.chat_stream(
            messages=[{"role": "user", "content": "hi"}],
            on_content_delta=_delta,
        )
        # Primary returns error but content was "streamed" (FakeProvider calls delta)
        # so failover should be skipped
        assert result.finish_reason == "error"
        factory.assert_not_called()


class TestFailoverOnTransientError:
    @pytest.mark.asyncio
    async def test_rate_limit(self) -> None:
        primary = _FakeProvider("primary", _error_response("rate limit exceeded"))
        fallback = _FakeProvider("fallback", _make_response("fallback ok"))
        factory = MagicMock(return_value=fallback)
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "fallback ok"
        assert result.finish_reason == "stop"
        factory.assert_called_once_with(_fallback("fallback-a"))

    @pytest.mark.asyncio
    async def test_timeout(self) -> None:
        primary = _FakeProvider(
            "primary",
            _make_response("timed out", finish_reason="error", error_kind="timeout"),
        )
        fallback = _FakeProvider("fallback", _make_response("fallback ok"))
        factory = MagicMock(return_value=fallback)
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "fallback ok"
        assert result.finish_reason == "stop"
        factory.assert_called_once_with(_fallback("fallback-a"))


class TestFallbackTriesModelsInOrder:
    @pytest.mark.asyncio
    async def test(self) -> None:
        primary = _FakeProvider("primary", _error_response("primary fail"))
        fallback_a = _FakeProvider("a", _error_response("a fail"))
        fallback_b = _FakeProvider("b", _make_response("b ok"))
        factory = MagicMock(side_effect=[fallback_a, fallback_b])

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a"), _fallback("fallback-b")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "b ok"
        assert factory.call_count == 2
        factory.assert_any_call(_fallback("fallback-a"))
        factory.assert_any_call(_fallback("fallback-b"))


class TestAllFallbacksFail:
    @pytest.mark.asyncio
    async def test(self) -> None:
        primary = _FakeProvider("primary", _error_response("primary fail"))
        fallback = _FakeProvider("fallback", _error_response("all fail"))
        factory = MagicMock(return_value=fallback)

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.finish_reason == "error"
        assert "all fail" in result.content


class TestFactoryExceptionSkipsModel:
    @pytest.mark.asyncio
    async def test(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        fallback_b = _FakeProvider("b", _make_response("b ok"))
        factory = MagicMock(side_effect=[ValueError("no key"), fallback_b])

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a"), _fallback("fallback-b")],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "b ok"
        assert factory.call_count == 2


class TestFallbackModelParameter:
    @pytest.mark.asyncio
    async def test(self) -> None:
        """Fallback calls should use the fallback model name."""
        primary = _FakeProvider("primary", _error_response())
        fallback = _FakeProvider("fallback", _make_response("ok"))
        factory = MagicMock(return_value=fallback)

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-model")],
            provider_factory=factory,
        )

        await fb.chat(messages=[{"role": "user", "content": "hi"}], model="primary-model")
        assert fallback.chat_calls[0]["model"] == "fallback-model"

    @pytest.mark.asyncio
    async def test_overrides_generation_fields_when_configured(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        fallback = _FakeProvider("fallback", _make_response("ok"))
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[
                _fallback(
                    "fallback-model",
                    max_tokens=1234,
                    temperature=0.4,
                    reasoning_effort="low",
                )
            ],
            provider_factory=MagicMock(return_value=fallback),
        )

        await fb.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="primary-model",
            max_tokens=8192,
            temperature=0.1,
            reasoning_effort="high",
        )

        assert fallback.chat_calls[0]["model"] == "fallback-model"
        assert fallback.chat_calls[0]["max_tokens"] == 1234
        assert fallback.chat_calls[0]["temperature"] == 0.4
        assert fallback.chat_calls[0]["reasoning_effort"] == "low"


class TestNoFallbackWhenEmptyList:
    @pytest.mark.asyncio
    async def test(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        factory = MagicMock()

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[],
            provider_factory=factory,
        )

        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.finish_reason == "error"
        factory.assert_not_called()


class TestChatStreamFailover:
    @pytest.mark.asyncio
    async def test_fallback_succeeds(self) -> None:
        # Use empty content so on_content_delta is not triggered on the error
        primary = _FakeProvider("primary", _error_response(""))
        fallback = _FakeProvider("fallback", _make_response("stream ok"))
        factory = MagicMock(return_value=fallback)

        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        result = await fb.chat_stream(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "stream ok"
        assert result.finish_reason == "stop"


class TestGetDefaultModel:
    def test(self) -> None:
        primary = _FakeProvider("primary")
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("a")],
            provider_factory=MagicMock(),
        )
        assert fb.get_default_model() == "primary/model"


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_skips_primary_after_three_failures(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        fallback = _FakeProvider("fallback", _make_response("fallback ok"))
        factory = MagicMock(return_value=fallback)
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        # 3 failures — primary should still be called each time
        for _ in range(3):
            result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
            assert result.content == "fallback ok"

        assert len(primary.chat_calls) == 3

        # 4th call — primary circuit is open, should be skipped
        primary.chat_calls.clear()
        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "fallback ok"
        assert len(primary.chat_calls) == 0

    @pytest.mark.asyncio
    async def test_resets_on_success(self) -> None:
        primary = _FakeProvider("primary", _error_response())
        fallback = _FakeProvider("fallback", _make_response("fallback ok"))
        factory = MagicMock(return_value=fallback)
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("fallback-a")],
            provider_factory=factory,
        )

        # 2 failures
        for _ in range(2):
            await fb.chat(messages=[{"role": "user", "content": "hi"}])

        # 3rd call: primary succeeds — circuit resets
        primary._response = _make_response("primary ok")
        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "primary ok"

        # 4th call: primary fails again — should still be called (counter reset)
        primary._response = _error_response()
        primary.chat_calls.clear()
        result = await fb.chat(messages=[{"role": "user", "content": "hi"}])
        assert result.content == "fallback ok"
        assert len(primary.chat_calls) == 1


class TestGenerationForwarded:
    def test(self) -> None:
        from nanobot.providers.base import GenerationSettings
        primary = _FakeProvider("primary")
        primary.generation = GenerationSettings(temperature=0.5, max_tokens=1024)
        fb = FallbackProvider(
            primary=primary,
            fallback_models=[_fallback("a")],
            provider_factory=MagicMock(),
        )
        assert fb.generation.temperature == 0.5
        assert fb.generation.max_tokens == 1024
