"""Config that is ignored says so (#205).

Both cases came from one misdiagnosis on a real deployment. `agents.defaults.model` read
`anthropic/claude-opus-4-5` while `modelPreset` pointed at a Kimi preset, so the field was dead and
nothing said so -- and reading only that field led to telling the operator his deployment was
silently running on a fallback. It was not.
"""

from __future__ import annotations

from nanoinfra.config.honesty import (
    config_warnings,
    credential_warning,
)
from nanoinfra.config.schema import Config, ModelPresetConfig


def _config(*, model: str = "", preset: str = "", presets: dict[str, str] | None = None) -> Config:
    config = Config()
    config.agents.defaults.model = model
    config.agents.defaults.model_preset = preset
    for name, preset_model in (presets or {}).items():
        config.model_presets[name] = ModelPresetConfig(model=preset_model)
    return config


# --- the dead field ------------------------------------------------------------------------


def test_a_preset_whose_provider_has_no_key_says_so_by_name() -> None:
    config = _config(preset="k", presets={"k": "anthropic/claude-sonnet-5"})
    config.providers.anthropic.api_key = ""

    warning = credential_warning(config)

    assert "'k'" in warning
    assert "anthropic" in warning
    assert "falls back" in warning


def test_a_preset_with_a_key_is_quiet() -> None:
    config = _config(preset="k", presets={"k": "anthropic/claude-sonnet-5"})
    config.providers.anthropic.api_key = "sk-test-not-a-real-key"

    assert credential_warning(config) == ""


def test_a_preset_name_that_does_not_exist_is_named() -> None:
    warning = credential_warning(_config(preset="ghost"))

    assert "ghost" in warning
    assert "falls back" in warning


def test_a_model_no_provider_matches_is_named() -> None:
    warning = credential_warning(_config(preset="k", presets={"k": "acme/does-not-exist"}))

    assert "acme/does-not-exist" in warning


# --- what the caller gets ------------------------------------------------------------------


def test_a_dead_model_field_says_nothing_because_the_primary_preset_decides() -> None:
    """`agents.defaults.model` beside a preset used to produce a warning. It is gone.

    The preset the WebUI marks **Primary** is what runs, always, so the field is inert rather than
    contradictory -- and a line telling an operator to tidy an inert field is noise on every boot,
    with a fix that risked clearing the Primary selection. A config with nothing to *do* about it
    has nothing worth saying about it.
    """
    config = _config(
        model="anthropic/claude-opus-4-5", preset="k", presets={"k": "anthropic/claude-sonnet-5"}
    )
    config.providers.anthropic.api_key = "sk-test-not-a-real-key"

    assert config_warnings(config) == []


def test_a_config_with_nothing_to_say_says_nothing() -> None:
    """A quiet boot for the deployments that are fine is the point of the empty case."""
    config = _config(preset="k", presets={"k": "anthropic/claude-sonnet-5"})
    config.providers.anthropic.api_key = "sk-test-not-a-real-key"

    assert config_warnings(config) == []
