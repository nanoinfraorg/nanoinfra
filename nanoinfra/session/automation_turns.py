"""Shared handling for session-bound automation turns."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
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
#: Which agent a turn asks to be answered by. The seam ``AgentLoop._acting_agent_for`` resolves
#: against ``agents.named``, and the composer writes the same key for a chosen agent (#254) -- so
#: an automation names its agent the same way a person does, and the loop decides in one place
#: who actually answered. A name here is a *request*; the roster in config is the authority.
#:
#: Absent, never blank, when no agent is named: absent and "the deployment's default agent" have
#: to be one state, or a reader would have two ways to spell the same thing.
TURN_AGENT_META = "agent"
#: The tool groups the acting agent declared, as ``{"tool_groups": [str, ...]}`` (#257).
#:
#: Beside the name rather than derived from it, for the reason ``DelegateBinding`` is carried: the
#: turn crosses the bus, and by the time it runs the config lookup that resolved it is over. What
#: it caps is read by the tool-availability filter, which has no route to config.
AUTOMATION_AGENT_META = "_automation_agent"


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


def automation_agent_metadata(
    name: str,
    tool_groups: Iterable[str],
) -> dict[str, Any]:
    """Both keys an automation writes for the agent it runs as, written together.

    One constructor rather than two literals per runner, so the name the loop resolves and the
    ceiling the tool filter reads cannot come apart -- a turn capped by an agent's groups while
    the record says the default agent answered would be the misattribution #248 exists to stop.
    """
    return {
        TURN_AGENT_META: name,
        AUTOMATION_AGENT_META: {"tool_groups": [str(group) for group in tool_groups]},
    }


def turn_agent(metadata: Mapping[str, Any] | None) -> str | None:
    """The agent this turn asked for, or ``None`` for the deployment's default agent.

    The request, not the verdict: ``AgentLoop`` is what checks the name against the roster, and
    ``RequestContext.agent`` is where the answer lands. This reads the key for the callers that
    only need to name the agent in a message, and have no route to config to validate it.
    """
    raw = cast(object, (metadata or {}).get(TURN_AGENT_META))
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip()


def automation_agent_tool_groups(metadata: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The tool groups the acting agent declared.

    Empty means every group, which is both the reading ``DelegateBinding.tool_groups`` has and
    the reading a deployment with one agent already gets. It is *not* "no groups": an agent that
    declared nothing is unrestricted, and an agent that declared something is capped by it.
    """
    raw = cast(object, (metadata or {}).get(AUTOMATION_AGENT_META))
    if not isinstance(raw, Mapping):
        return ()
    groups = cast(object, cast(Mapping[str, object], raw).get("tool_groups"))
    if not isinstance(groups, (list, tuple)):
        return ()
    entries = list(cast("list[object] | tuple[object, ...]", groups))
    return tuple(str(name).strip() for name in entries if str(name).strip())


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
