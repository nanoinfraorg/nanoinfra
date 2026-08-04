"""LLM provider abstraction module."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from nanoinfra.providers.base import LLMProvider, LLMResponse

__all__ = [
    "LLMProvider",
    "LLMResponse",
    "AnthropicProvider",
    "OpenAICompatProvider",
    "OpenAICodexProvider",
    "XAIGrokProvider",
    "GitHubCopilotProvider",
    "AzureOpenAIProvider",
    "BedrockProvider",
]

_LAZY_IMPORTS = {
    "AnthropicProvider": ".anthropic_provider",
    "OpenAICompatProvider": ".openai_compat_provider",
    "OpenAICodexProvider": ".openai_codex_provider",
    "XAIGrokProvider": ".xai_grok_provider",
    "GitHubCopilotProvider": ".github_copilot_provider",
    "AzureOpenAIProvider": ".azure_openai_provider",
    "BedrockProvider": ".bedrock_provider",
}

if TYPE_CHECKING:
    from nanoinfra.providers.anthropic_provider import AnthropicProvider
    from nanoinfra.providers.azure_openai_provider import AzureOpenAIProvider
    from nanoinfra.providers.bedrock_provider import BedrockProvider
    from nanoinfra.providers.github_copilot_provider import GitHubCopilotProvider
    from nanoinfra.providers.openai_codex_provider import OpenAICodexProvider
    from nanoinfra.providers.openai_compat_provider import OpenAICompatProvider
    from nanoinfra.providers.xai_grok_provider import XAIGrokProvider


def __getattr__(name: str):
    """Lazily expose provider implementations without importing all backends up front."""
    module_name = _LAZY_IMPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(module_name, __name__)
    return getattr(module, name)
