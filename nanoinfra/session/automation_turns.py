"""Shared handling for session-bound automation turns."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, cast

AUTOMATION_HISTORY_META = "_automation_turn"
#: Skills the automation running this turn declared. Written by the runner, read when the prompt is
#: built. Absent or empty means the full catalogue, which is what every automation had before.
AUTOMATION_SKILLS_META = "_automation_skills"
#: The MCP servers an automation declares (#204). Named for the key the composer already writes,
#: because `attached_servers()` reads one key and a mention and a declaration must not diverge.
AUTOMATION_PRESETS_META = "mcp_presets"


@dataclass(frozen=True)
class AutomationTurnSpec:
    """Source-specific wiring for one session-bound automation turn type."""

    kind: str
    trigger_meta_key: str
    #: Key inside the trigger metadata holding this automation's own id. It is what scopes
    #: per-automation state, so a spec that omits it gets no state rather than a shared bucket.
    id_field: str = ""
    legacy_history_meta_key: str | None = None
    history_fields: Mapping[str, str] = field(default_factory=dict[str, str])
    text_builder: Callable[[Mapping[str, Any]], str | None] | None = None


def automation_trigger(
    metadata: Mapping[str, Any] | None,
    spec: AutomationTurnSpec,
) -> dict[str, Any] | None:
    """Return source trigger metadata for *spec* when present."""
    raw = (metadata or {}).get(spec.trigger_meta_key)
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else None


def automation_history_overrides_for_spec(
    metadata: Mapping[str, Any] | None,
    spec: AutomationTurnSpec,
) -> tuple[str | None, dict[str, Any]]:
    """Return hidden session-history text/metadata overrides for *spec*."""
    trigger = automation_trigger(metadata, spec)
    if not trigger:
        return None, {}

    details: dict[str, Any] = {"kind": spec.kind}
    extra: dict[str, Any] = {AUTOMATION_HISTORY_META: details}
    if spec.legacy_history_meta_key:
        extra[spec.legacy_history_meta_key] = True
    for history_key, trigger_key in spec.history_fields.items():
        value = trigger.get(trigger_key)
        extra[history_key] = value
        details[history_key] = value

    text = spec.text_builder(trigger) if spec.text_builder else None
    return text, extra


@lru_cache(maxsize=1)
def _automation_specs() -> tuple[AutomationTurnSpec, ...]:
    # Source modules import the generic helpers above, so keep spec loading lazy.
    from nanoinfra.cron.session_turns import CRON_AUTOMATION_SPEC
    from nanoinfra.triggers.local_session_turns import LOCAL_TRIGGER_AUTOMATION_SPEC

    return (CRON_AUTOMATION_SPEC, LOCAL_TRIGGER_AUTOMATION_SPEC)




def automation_declared_skills(metadata: Mapping[str, Any] | None) -> list[str] | None:
    """Return the skills this automation declared, or ``None`` for the full catalogue.

    ``None`` rather than an empty list, because the two mean different things to the prompt
    builder: no declaration is "show everything", and an empty declaration would be "show
    nothing", which no automation record can currently express and which would be a footgun.
    """
    raw = cast(object, (metadata or {}).get(AUTOMATION_SKILLS_META))
    if not isinstance(raw, (list, tuple)):
        return None
    entries = list(cast("list[object] | tuple[object, ...]", raw))
    names = [str(name).strip() for name in entries if str(name).strip()]
    return names or None

def automation_declared_presets(metadata: Mapping[str, Any] | None) -> list[str] | None:
    """Return the MCP servers this automation declared, or ``None`` for none.

    A cron job has no person to type `@server`, so a `mention` server would be unreachable from an
    unattended turn -- which is the case that makes the advertisement useless rather than helpful.
    Declaring it is the same act, written down in advance.
    """
    raw = cast(object, (metadata or {}).get(AUTOMATION_PRESETS_META))
    if not isinstance(raw, (list, tuple)):
        return None
    entries = list(cast("list[object] | tuple[object, ...]", raw))
    names = [str(name).strip() for name in entries if str(name).strip()]
    return names or None


def automation_identity(
    metadata: Mapping[str, Any] | None,
) -> tuple[str, str] | None:
    """Return ``(kind, automation_id)`` for an automation-driven turn, or ``None``.

    ``None`` for an interactive turn is the useful answer, not a missing one: it is how a caller
    scoped to one automation refuses to act when there is no automation to be scoped to.
    """
    for spec in _automation_specs():
        if not spec.id_field:
            continue
        trigger = automation_trigger(metadata, spec)
        if not trigger:
            continue
        value = trigger.get(spec.id_field)
        if isinstance(value, str) and value.strip():
            return spec.kind, value.strip()
    return None

def automation_history_overrides(
    metadata: Mapping[str, Any] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Return session-history text/metadata overrides for supported automation turns."""
    for spec in _automation_specs():
        text, extra = automation_history_overrides_for_spec(metadata, spec)
        if extra:
            return text, extra
    return None, {}


def is_automation_history_message(message: Mapping[str, Any] | None) -> bool:
    """True for hidden automation trigger records in session history."""
    if not message:
        return False
    marker = message.get(AUTOMATION_HISTORY_META)
    if marker is True or isinstance(marker, Mapping):
        return True
    return any(
        spec.legacy_history_meta_key
        and message.get(spec.legacy_history_meta_key) is True
        for spec in _automation_specs()
    )


def is_automation_kind(value: Any) -> bool:
    return isinstance(value, str) and (
        value == "trigger" or any(spec.kind == value for spec in _automation_specs())
    )
