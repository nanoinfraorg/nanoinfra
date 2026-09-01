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
    dead_model_field_warning,
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


def test_a_field_overridden_by_a_preset_says_which_wins() -> None:
    warning = dead_model_field_warning(
        _config(
            model="anthropic/claude-opus-4-5",
            preset="kimi-general",
            presets={"kimi-general": "moonshot/kimi-k3"},
        )
    )

    assert "agents.defaults.model" in warning
    assert "anthropic/claude-opus-4-5" in warning
    assert "kimi-general" in warning
    assert "moonshot/kimi-k3" in warning


def test_a_field_that_agrees_with_the_preset_is_not_a_contradiction() -> None:
    """The warning is about two settings disagreeing. A file that repeats itself is not wrong."""
    assert (
        dead_model_field_warning(
            _config(model="moonshot/kimi-k3", preset="k", presets={"k": "moonshot/kimi-k3"})
        )
        == ""
    )


def test_no_preset_means_the_field_is_the_answer() -> None:
    assert dead_model_field_warning(_config(model="anthropic/claude-opus-4-5")) == ""


def test_an_untouched_field_is_not_a_stale_field() -> None:
    assert dead_model_field_warning(_config(preset="k", presets={"k": "x"})) == ""


def test_a_preset_that_resolves_to_nothing_is_left_to_its_own_error() -> None:
    """Saying both would bury the louder problem, which has an error at the point of use."""
    assert dead_model_field_warning(_config(model="anthropic/claude-opus-4-5", preset="ghost")) == ""


# --- the credential ------------------------------------------------------------------------


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


def test_both_lines_come_back_together_and_in_a_stable_order() -> None:
    config = _config(
        model="anthropic/claude-opus-4-5", preset="k", presets={"k": "anthropic/claude-sonnet-5"}
    )
    config.providers.anthropic.api_key = ""

    lines = config_warnings(config)

    assert len(lines) == 2
    assert lines[0].startswith("agents.defaults.model")


def test_a_config_with_nothing_to_say_says_nothing() -> None:
    """A quiet boot for the deployments that are fine is the point of the empty case."""
    config = _config(preset="k", presets={"k": "anthropic/claude-sonnet-5"})
    config.providers.anthropic.api_key = "sk-test-not-a-real-key"

    assert config_warnings(config) == []
