"""Provider credentials must not leak through process-global os.environ.

A key in ``os.environ`` is readable at ``/proc/self/environ`` and is inherited by
any subprocess started without an explicit environment. The agent and the
executor run on separate uids so the agent cannot reach a credential; a
process-global key would hand it back. See nanoinfraorg/nanoinfra#133.
"""

from __future__ import annotations

import os

from nanoinfra.providers.openai_compat_provider import OpenAICompatProvider
from nanoinfra.providers.registry import find_by_name


def test_provider_init_does_not_mutate_shared_env_keys(monkeypatch) -> None:
    """Multi-provider setups must not overwrite or pin each other's keys."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    openai_spec = find_by_name("openai")
    openrouter_spec = find_by_name("openrouter")
    assert openai_spec is not None and openrouter_spec is not None

    OpenAICompatProvider(
        api_key="sk-openai-secret",
        default_model="gpt-4o",
        spec=openai_spec,
    )
    OpenAICompatProvider(
        api_key="sk-or-secret",
        default_model="openrouter/auto",
        spec=openrouter_spec,
        api_base="https://openrouter.ai/api/v1",
    )

    assert "OPENAI_API_KEY" not in os.environ
    assert "OPENROUTER_API_KEY" not in os.environ


def test_provider_init_preserves_preexisting_env_keys(monkeypatch) -> None:
    """A key the operator exported stays theirs; config must not shadow it."""
    monkeypatch.setenv("OPENAI_API_KEY", "preexisting-user-key")

    openai_spec = find_by_name("openai")
    assert openai_spec is not None
    provider = OpenAICompatProvider(
        api_key="sk-from-config",
        default_model="gpt-4o",
        spec=openai_spec,
    )

    assert os.environ["OPENAI_API_KEY"] == "preexisting-user-key"
    assert provider._api_key_for_client == "sk-from-config"


def test_provider_init_does_not_write_env_extras(monkeypatch) -> None:
    """``env_extras`` resolved a key into further variables; none may be set."""
    spec = find_by_name("openrouter")
    assert spec is not None
    for name, _value in spec.env_extras:
        monkeypatch.delenv(name, raising=False)

    OpenAICompatProvider(
        api_key="sk-or-secret",
        default_model="openrouter/auto",
        spec=spec,
        api_base="https://openrouter.ai/api/v1",
    )

    for name, _value in spec.env_extras:
        assert name not in os.environ
