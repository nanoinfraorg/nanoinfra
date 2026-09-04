"""Configuration loading utilities."""

import json
import os
import re
from pathlib import Path
from typing import Any, cast, overload

from loguru import logger
from pydantic import BaseModel, ValidationError
from pydantic_settings import SettingsError

from nanoinfra.config.errors import ConfigIssue, ConfigLoadError, validation_issues
from nanoinfra.config.schema import AgentDefaults, Config, ensure_tool_config_refs
from nanoinfra.utils.helpers import _write_text_atomic  # pyright: ignore[reportPrivateUsage]

# Global variable to store current config path (for multi-instance support)
_current_config_path: Path | None = None


def _as_config_object(value: object) -> dict[str, Any] | None:
    """Narrow an untrusted JSON configuration value to an object."""
    return cast(dict[str, Any], value) if isinstance(value, dict) else None


def set_config_path(path: Path) -> None:
    """Set the current config path (used to derive data directory)."""
    global _current_config_path
    _current_config_path = path


def get_config_path() -> Path:
    """Get the configuration file path."""
    if _current_config_path:
        return _current_config_path
    return Path.home() / ".nanoinfra" / "config.json"


def load_config(config_path: Path | None = None) -> Config:
    """
    Load configuration from file or create default.

    Args:
        config_path: Optional path to config file. Uses default if not provided.

    Returns:
        Loaded configuration object.
    """
    # One source of truth for "the references are resolved" (#57). This module kept its own flag
    # beside the state of the two model classes, and two notions of ready can disagree: the flag
    # said nothing about a class the eager attempt left incomplete.
    ensure_tool_config_refs()

    path = config_path or get_config_path()

    if not path.exists():
        try:
            config = Config()
        except SettingsError as exc:
            raise ConfigLoadError(
                path,
                kind="invalid_schema",
                summary=(
                    "Environment-based configuration could not be parsed. "
                    "Check that complex NANOINFRA_* values use valid JSON."
                ),
            ) from exc
        except ValidationError as exc:
            raise ConfigLoadError(
                path,
                kind="invalid_schema",
                summary="Environment-based configuration is invalid.",
                issues=validation_issues(exc),
            ) from exc
        _apply_ssrf_whitelist(config)
        return config

    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(
            path,
            kind="invalid_json",
            summary=(
                f"JSON syntax error at line {exc.lineno}, column {exc.colno}: "
                f"{_sentence(exc.msg)}"
            ),
        ) from exc
    except UnicodeDecodeError as exc:
        raise ConfigLoadError(
            path,
            kind="io_error",
            summary="The file is not valid UTF-8.",
        ) from exc
    except OSError as exc:
        detail = exc.strerror or type(exc).__name__
        raise ConfigLoadError(
            path,
            kind="io_error",
            summary=f"Unable to read the file: {_sentence(detail)}",
        ) from exc

    if not isinstance(data, dict):
        root_type = type(data).__name__
        raise ConfigLoadError(
            path,
            kind="invalid_root",
            summary="The top level of config.json must be a JSON object.",
            issues=(
                ConfigIssue(
                    path=(),
                    message=f"Expected an object, but found {root_type}.",
                ),
            ),
        )

    data = _migrate_config(cast(dict[str, Any], data))
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        issues = validation_issues(exc)
        raise ConfigLoadError(
            path,
            kind="invalid_schema",
            summary=f"Found {len(issues)} invalid setting(s).",
            issues=issues,
        ) from exc

    _apply_ssrf_whitelist(config)
    return config


def _apply_ssrf_whitelist(config: Config) -> None:
    """Apply SSRF whitelist from config to the network security module."""
    from nanoinfra.security.network import configure_ssrf_whitelist

    configure_ssrf_whitelist(config.tools.ssrf_whitelist)


def save_config(config: Config, config_path: Path | None = None) -> None:
    """
    Save configuration to file.

    Args:
        config: Configuration to save.
        config_path: Optional path to save to. Uses default if not provided.
    """
    path = config_path or get_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump(mode="json", by_alias=True)
    # OAuth credentials live in dedicated token stores. Persist only the
    # non-credential request settings consumed by these provider backends.
    for alias, provider in (
        ("openaiCodex", config.providers.openai_codex),
        ("xaiGrok", config.providers.xai_grok),
    ):
        settings = provider.model_dump(
            mode="json",
            by_alias=True,
            include={"proxy", "extra_body"},
            exclude_none=True,
        )
        if settings:
            data.setdefault("providers", {})[alias] = settings

    _drop_dead_model_choice(data, config)

    # Temp + replace so a crash mid-write cannot leave a truncated config.json.
    _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


def _drop_dead_model_choice(data: dict[str, Any], config: Config) -> None:
    """Do not write ``agents.defaults.model`` while a preset makes it dead and nobody chose it.

    The field is not dead in general -- it *is* the implicit ``default`` preset, and a deployment
    with no ``modelPresets`` runs on it (see ``Config.resolve_default_preset``). It is dead only
    while ``modelPreset`` names a preset that exists, because then every one of these values comes
    from that preset instead.

    Two conditions, and both are needed. **Dead**, so omitting it cannot change which model
    answers. And **still the packaged value**, so this can never drop a model somebody picked --
    a chosen one differs from the default, and is written as it always was.

    Why bother, when the loader fills an absent key back in from the schema and the runtime is
    identical either way: this is the line that caused a real misdiagnosis. `config.json` read
    ``"model": "anthropic/claude-opus-4-5"`` on a deployment running a Kimi preset, and the
    conclusion drawn from reading it was that the deployment had silently fallen back. It had
    not. The product carried a boot warning about this for a while, and a warning about a value
    nothing acts on is noise -- so the fix is to stop writing the value.

    Only the two fields that name the model. `maxTokens` and the rest of the implicit preset are
    equally overridden, and equally harmless: nobody reads ``maxTokens: 8192`` and concludes
    something false about which model is answering.
    """
    preset = config.agents.defaults.model_preset
    if not preset or preset == "default" or preset not in config.model_presets:
        return
    defaults = cast(
        "dict[str, Any]", cast("dict[str, Any]", data.get("agents", {})).get("defaults", {})
    )
    for alias, field in (("model", "model"), ("provider", "provider")):
        packaged = AgentDefaults.model_fields[field].default
        if defaults.get(alias) == packaged:
            defaults.pop(alias, None)


def merge_missing_defaults(existing: object, defaults: object) -> object:
    """Recursively add missing defaults without replacing configured values."""
    if not isinstance(existing, dict) or not isinstance(defaults, dict):
        return cast(object, existing)

    existing_dict = cast(dict[str, object], existing)
    defaults_dict = cast(dict[str, object], defaults)
    merged = dict(existing_dict)
    for key, value in defaults_dict.items():
        if key not in merged:
            merged[key] = value
        else:
            merged[key] = merge_missing_defaults(merged[key], value)
    return merged


_ENV_REF_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve_config_env_vars(
    config: Config,
    *,
    config_path: Path | None = None,
) -> Config:
    """Return *config* with ``${VAR}`` env-var references resolved.

    Walks in place so fields declared with ``exclude=True`` survive;
    returns the same instance when no references are present.
    Raises ``ConfigLoadError`` if a referenced variable is not set.
    """
    missing = tuple(_missing_env_issues(config))
    if missing:
        raise ConfigLoadError(
            config_path or get_config_path(),
            kind="missing_env",
            summary=f"Found {len(missing)} missing environment variable reference(s).",
            issues=missing,
        )
    return _resolve_in_place(config)


@overload
def resolve_env_refs(value: str) -> str: ...


@overload
def resolve_env_refs(value: object) -> object: ...


def resolve_env_refs(value: object) -> object:
    """Resolve ``${VAR}`` references in a single string, leniently.

    Unlike :func:`resolve_config_env_vars` (which walks a whole ``Config`` and
    raises on a missing variable), this resolves one value and returns an empty
    string if any reference is unset. It is meant for individual, lazily consumed
    fields — e.g. a transcription provider's ``api_key`` or ``api_base`` — so a
    missing variable degrades to "not configured" instead of producing a partial
    value. Non-string input is returned unchanged.
    """
    if not isinstance(value, str):
        return value
    names = _ENV_REF_PATTERN.findall(value)
    if any(name not in os.environ for name in names):
        return ""
    return _ENV_REF_PATTERN.sub(lambda m: os.environ[m.group(1)], value)


def _resolve_in_place(obj: Any) -> Any:
    if isinstance(obj, str):
        new = _ENV_REF_PATTERN.sub(_env_replace, obj)
        return new if new != obj else obj
    if isinstance(obj, BaseModel):
        updates: dict[str, Any] = {}
        for name in type(obj).model_fields:
            old = getattr(obj, name)
            new = _resolve_in_place(old)
            if new is not old:
                updates[name] = new
        extras = obj.__pydantic_extra__
        new_extras: dict[str, Any] | None = None
        if extras:
            resolved = {k: _resolve_in_place(v) for k, v in extras.items()}
            if any(resolved[k] is not extras[k] for k in extras):
                new_extras = resolved
        if not updates and new_extras is None:
            return obj
        copy = obj.model_copy(update=updates) if updates else obj.model_copy()
        if new_extras is not None:
            copy.__pydantic_extra__ = new_extras
        return copy
    if isinstance(obj, dict):
        object_dict = cast(dict[str, Any], obj)
        resolved = {key: _resolve_in_place(value) for key, value in object_dict.items()}
        return (
            resolved
            if any(resolved[key] is not object_dict[key] for key in object_dict)
            else cast(object, obj)
        )
    if isinstance(obj, list):
        object_list = cast(list[Any], obj)
        resolved = [_resolve_in_place(value) for value in object_list]
        return (
            resolved
            if any(new is not old for new, old in zip(resolved, object_list))
            else cast(object, obj)
        )
    return obj


def _missing_env_issues(
    obj: Any,
    path: tuple[str | int, ...] = (),
) -> list[ConfigIssue]:
    if isinstance(obj, str):
        return [
            ConfigIssue(
                path=path,
                message=f"Environment variable '{name}' is not set.",
            )
            for name in dict.fromkeys(_ENV_REF_PATTERN.findall(obj))
            if name not in os.environ
        ]
    if isinstance(obj, BaseModel):
        issues: list[ConfigIssue] = []
        for name, field in type(obj).model_fields.items():
            alias = field.serialization_alias or field.alias or name
            part = alias
            issues.extend(_missing_env_issues(getattr(obj, name), (*path, part)))
        for name, value in (obj.__pydantic_extra__ or {}).items():
            issues.extend(_missing_env_issues(value, (*path, name)))
        return issues
    if isinstance(obj, dict):
        object_dict = cast(dict[str | int, Any], obj)
        issues = []
        for name, value in object_dict.items():
            part = name
            issues.extend(_missing_env_issues(value, (*path, part)))
        return issues
    if isinstance(obj, list):
        issues = []
        for index, value in enumerate(cast(list[Any], obj)):
            issues.extend(_missing_env_issues(value, (*path, index)))
        return issues
    return []


def _resolve_env_vars(obj: object) -> object:
    """Recursively resolve ``${VAR}`` patterns in plain strings/dicts/lists."""
    if isinstance(obj, str):
        return _ENV_REF_PATTERN.sub(_env_replace, obj)
    if isinstance(obj, dict):
        return {
            key: _resolve_env_vars(value)
            for key, value in cast(dict[str, object], obj).items()
        }
    if isinstance(obj, list):
        return [_resolve_env_vars(value) for value in cast(list[object], obj)]
    return obj


def _env_replace(match: re.Match[str]) -> str:
    name = match.group(1)
    value = os.environ.get(name)
    if value is None:
        raise ValueError(
            f"Environment variable '{name}' referenced in config is not set"
        )
    return value


_WARNED_MIGRATIONS: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """Log a migration notice one time per process.

    The config is re-read on every change by the watcher, so an unconditional log here would
    repeat the same notice for the life of the gateway and train the operator to skip it.
    """
    if key in _WARNED_MIGRATIONS:
        return
    _WARNED_MIGRATIONS.add(key)
    logger.warning(message)


def _migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate old config formats to current.

    This runs only for a config file that was read from disk, never for the environment-only
    path, so "the key is absent" means an existing installation rather than a fresh one.
    """
    # Move tools.exec.restrictToWorkspace → tools.restrictToWorkspace
    tools_value = data.get("tools", {})
    if not isinstance(tools_value, dict):
        return data
    tools = cast(dict[str, Any], tools_value)
    exec_cfg = _as_config_object(tools.get("exec", {}))
    if (
        exec_cfg is not None
        and "restrictToWorkspace" in exec_cfg
        and "restrictToWorkspace" not in tools
    ):
        tools["restrictToWorkspace"] = exec_cfg.pop("restrictToWorkspace")

    # The shipped default for tools.restrictToWorkspace changed from false to true (#135). Flipping
    # it alone would start blocking commands on every installation that has been running without
    # it, so an install that never chose a value keeps the one it has been running with.
    #
    # Absence is a safe signal here because save_config writes a full model_dump: any config this
    # codebase has written carries the key explicitly. A file without it was written by an older
    # release or by hand, and either way its behaviour is the unrestricted one.
    #
    # The cost is stated rather than hidden: a hand-written minimal config on a *new* install also
    # gets the permissive value. Tightening a guard under a config we did not write is the worse
    # error, so the log says what happened and how to opt in.
    if "restrictToWorkspace" not in tools:
        tools["restrictToWorkspace"] = False
        _warn_once(
            "restrict_to_workspace_pinned",
            "tools.restrictToWorkspace is not set in config.json, so it stays false to preserve "
            "current behaviour. New installations default to true. Set it explicitly to confine "
            "tool file access to the workspace.",
        )
        data["tools"] = tools

    # Move tools.myEnabled / tools.mySet → tools.my.{enable, allowSet}.
    # The old flat keys shipped in the initial MyTool landing; wrapping them in a
    # sub-config keeps `web` / `exec` / `my` symmetric and gives room to grow.
    if "myEnabled" in tools or "mySet" in tools:
        my_cfg = tools.get("my")
        if my_cfg is None:
            my_cfg = {}
            tools["my"] = my_cfg
        if not isinstance(my_cfg, dict):
            return data
        my_cfg = cast(dict[str, Any], my_cfg)
        if "myEnabled" in tools and "enable" not in my_cfg:
            my_cfg["enable"] = tools.pop("myEnabled")
        else:
            tools.pop("myEnabled", None)
        if "mySet" in tools and "allowSet" not in my_cfg:
            my_cfg["allowSet"] = tools.pop("mySet")
        else:
            tools.pop("mySet", None)

    return data


def _sentence(message: str) -> str:
    message = message.strip()
    if message and message[-1] not in ".!?":
        message += "."
    return message
