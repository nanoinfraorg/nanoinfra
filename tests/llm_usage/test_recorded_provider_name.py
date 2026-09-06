"""A usage row names the provider that was configured, not the class that made the call (#235).

One class, `OpenAICompatProvider`, serves **every** OpenAI-compatible API in the registry. Its
inherited `observed_provider_name()` is derived from the class name, so every row from Moonshot,
DeepSeek, Groq, OpenRouter and forty others was recorded as `openaicompat`.

That broke two things at once:

* the per-model breakdown collapsed forty providers into one name, so "which provider is
  expensive" had no answer;
* `pricing` is keyed `"<provider>/<model>"` from the **configured** provider, so a rate typed for
  `moonshot/kimi-k3` could never match a row stored as `openaicompat/kimi-k3` -- the cost column
  stayed empty no matter what anybody configured.

Found by pricing a real deployment and getting `priced_models: 0` back with three rates set.
"""

from __future__ import annotations

import pytest

from nanoinfra.providers.openai_codex_provider import OpenAICodexProvider
from nanoinfra.providers.openai_compat_provider import OpenAICompatProvider
from nanoinfra.providers.registry import PROVIDERS, find_by_name
from nanoinfra.utils.token_calibration import calibration_key


@pytest.mark.parametrize("key", ["moonshot", "deepseek", "groq", "openrouter", "zhipu"])
def test_a_compat_provider_records_its_configured_name(key: str) -> None:
    spec = find_by_name(key)
    assert spec is not None

    provider = OpenAICompatProvider(api_key="x", spec=spec, default_model="m")

    assert provider.observed_provider_name() == key


def test_two_compat_providers_do_not_share_one_name() -> None:
    """The property the old behaviour violated, stated as a property rather than per provider."""
    moonshot = OpenAICompatProvider(api_key="x", spec=find_by_name("moonshot"), default_model="m")
    deepseek = OpenAICompatProvider(api_key="x", spec=find_by_name("deepseek"), default_model="m")

    assert moonshot.observed_provider_name() != deepseek.observed_provider_name()


def test_a_provider_built_without_a_spec_keeps_the_class_name() -> None:
    """The SDK and the tests construct it bare, and a row still needs some name."""
    provider = OpenAICompatProvider(api_key="x", default_model="m")

    assert provider.observed_provider_name() == "openaicompat"


def test_every_compat_backed_provider_in_the_registry_has_a_distinct_name() -> None:
    """A pricing key has to be unique per provider or two of them share a bill."""
    names = [
        OpenAICompatProvider(api_key="x", spec=spec, default_model="m").observed_provider_name()
        for spec in PROVIDERS
        if spec.backend == "openai_compat" and not spec.settings_alias_for
    ]

    assert len(names) == len(set(names))
    assert "openaicompat" not in names


def test_the_calibration_key_is_deliberately_not_this_name() -> None:
    """It is per *tokenizer*, which is a property of the class and not of the endpoint.

    Stated as a test because the two look interchangeable and are not: changing the calibration
    key would discard every learned correction factor for no gain.
    """
    moonshot = OpenAICompatProvider(api_key="x", spec=find_by_name("moonshot"), default_model="m")
    deepseek = OpenAICompatProvider(api_key="x", spec=find_by_name("deepseek"), default_model="m")

    assert calibration_key(moonshot, "m") == calibration_key(deepseek, "m")
    assert moonshot.observed_provider_name() != deepseek.observed_provider_name()


# --- the stamp, which covers the backends that are not the compat class ------------------


def test_the_configured_name_wins_over_the_class_name() -> None:
    """The factory stamps it at the one point every backend converges on."""
    provider = OpenAICompatProvider(api_key="x", default_model="m")
    assert provider.observed_provider_name() == "openaicompat"

    provider.set_configured_provider_name("moonshot")

    assert provider.observed_provider_name() == "moonshot"


def test_a_one_class_backend_disagreed_with_its_own_config_key() -> None:
    """`openai_codex` derived `openaicodex`, so even a single-provider backend could not match.

    The underscore is the whole bug: the config key, the pricing key and the row have to agree
    character for character or the cost column stays empty.
    """
    provider = OpenAICodexProvider(default_model="m")
    assert provider.observed_provider_name() == "openaicodex"

    provider.set_configured_provider_name("openai_codex")

    assert provider.observed_provider_name() == "openai_codex"


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_stamp_falls_back_rather_than_recording_nothing(blank: str | None) -> None:
    """A row with an empty provider is worse than a coarse one."""
    provider = OpenAICompatProvider(api_key="x", default_model="m")

    provider.set_configured_provider_name(blank)

    assert provider.observed_provider_name() == "openaicompat"


def test_the_factory_stamps_every_provider_it_builds() -> None:
    """Asserted of the factory rather than of one backend, because the point is that it is one
    place: a backend added later gets this without knowing about it."""
    import inspect

    from nanoinfra.providers import factory

    source = inspect.getsource(factory._make_provider_core)  # pyright: ignore[reportPrivateUsage]
    assert "set_configured_provider_name(provider_name)" in source
    # Before `return`, and before the generation settings, so no early return can skip it.
    assert source.index("set_configured_provider_name") < source.rindex("return provider")
