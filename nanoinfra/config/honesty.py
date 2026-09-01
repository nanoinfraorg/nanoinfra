"""Config that is ignored should say so (#205).

Both warnings here come from one afternoon of misdiagnosis. `agents.defaults.model` read
`anthropic/claude-opus-4-5` on a deployment whose `model_preset` pointed at a Kimi preset, so the
field was dead and nothing said so; reading only that field led to telling the operator his
deployment was silently running on a fallback. It was not.

The second is the same shape from the other side: a preset whose provider has no credential fails
over quietly, and a fallback that *works* is the worst kind of misconfiguration -- it never hurts
enough for anybody to look.

Pure functions returning lines, in the shape `nanoinfra/gates/startup.py` already uses: a
diagnostic must not be able to stop a boot, so the caller wraps them and this module raises nothing
it can help.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanoinfra.config.schema import Config

#: What the packaged default config ships as. A field nobody has touched is not a stale field, so
#: it is not worth a line at every boot.
DEFAULT_MODEL_HINTS = ("", "default")


def dead_model_field_warning(config: Config) -> str:
    """One line when ``agents.defaults.model`` is set and something else decides the model.

    Empty when the two agree, when no preset is selected, or when the field was never set: the
    warning is about a *contradiction*, and a config that merely repeats itself is not one.
    """
    defaults = getattr(getattr(config, "agents", None), "defaults", None)
    if defaults is None:
        return ""
    preset_name = str(getattr(defaults, "model_preset", "") or "")
    model = str(getattr(defaults, "model", "") or "")
    if not preset_name or preset_name in DEFAULT_MODEL_HINTS:
        return ""
    if not model or model in DEFAULT_MODEL_HINTS:
        return ""
    try:
        effective = str(config.resolve_preset().model or "")
    except KeyError:
        # A preset name that resolves to nothing is a louder problem than a dead field, and it has
        # its own error at the point of use. Saying both here would bury it.
        return ""
    if not effective or effective == model:
        return ""
    return (
        f"agents.defaults.model is {model!r} and is ignored: "
        f"agents.defaults.modelPreset={preset_name!r} resolves to {effective!r}. "
        "Remove the field or clear the preset, so the file names one model."
    )


def credential_warning(config: Config) -> str:
    """One line when the selected preset's provider has no credential to use.

    Answers *which* preset and *which* provider, because the failure this prevents is an operator
    reading `provider=fallback` in a usage row and concluding the wrong thing about their primary.
    """
    defaults = getattr(getattr(config, "agents", None), "defaults", None)
    if defaults is None:
        return ""
    preset_name = str(getattr(defaults, "model_preset", "") or "") or "default"
    try:
        preset = config.resolve_preset()
    except KeyError:
        return (
            f"agents.defaults.modelPreset={preset_name!r} names no preset in modelPresets, "
            "so every turn falls back."
        )
    model = str(getattr(preset, "model", "") or "")
    if not model:
        return ""
    provider_config, spec_name = config._match_provider(preset=preset)  # pyright: ignore[reportPrivateUsage]
    if provider_config is None or not spec_name:
        return (
            f"model preset {preset_name!r} asks for {model!r} and no configured provider matches "
            "it, so every turn falls back."
        )
    if _provider_needs_no_key(spec_name):
        return ""
    if str(getattr(provider_config, "api_key", "") or ""):
        return ""
    return (
        f"model preset {preset_name!r} resolves to {model!r} on provider {spec_name!r}, "
        "which has no apiKey. Every turn using this preset falls back."
    )


def _provider_needs_no_key(spec_name: str) -> bool:
    """Whether a provider authenticates by some means other than a key in the config."""
    from nanoinfra.providers.registry import find_by_name

    spec: Any = find_by_name(spec_name)
    if spec is None:
        return False
    return bool(
        getattr(spec, "is_oauth", False)
        or getattr(spec, "is_local", False)
        or getattr(spec, "is_direct", False)
    )


def config_warnings(config: Config) -> list[str]:
    """Every line worth saying about this config, in a stable order."""
    lines = [dead_model_field_warning(config), credential_warning(config)]
    return [line for line in lines if line]


__all__ = [
    "config_warnings",
    "credential_warning",
    "dead_model_field_warning",
]
