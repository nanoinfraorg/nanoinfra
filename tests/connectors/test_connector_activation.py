"""Activation: what config says a connector may do, and what it refuses to activate.

Every case here is a mismatch an operator can write, and the property under test is always
the same one: it is refused **now**, with both halves named, rather than at 03:00 in a run
record. A connector that half-works is the failure being designed out.
"""

from __future__ import annotations

from typing import Any

import pytest

from nanoinfra.agent.tools.capabilities import capability_class_of
from nanoinfra.agent.tools.context import ToolContext
from nanoinfra.agent.tools.registry import ToolRegistry
from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.config.schema import Config, ToolsConfig
from nanoinfra.connectors.registration import register_connector_tools
from nanoinfra.connectors.setup import resolve_active, startup_summary

READONLY = "https://www.googleapis.com/auth/calendar.readonly"
EVENTS = "https://www.googleapis.com/auth/calendar.events"


def _cfg(**over: Any) -> ConnectorRuntimeConfig:
    payload: dict[str, Any] = {
        "credentials": {
            "google-workspace": {
                "clientId": "cid.apps.googleusercontent.test",
                "secretRef": "google-refresh",
                "clientSecretRef": "google-client-secret",
                "scopes": [READONLY, EVENTS],
            }
        },
        "connectors": {
            "google-calendar": {
                "credential": "google-workspace",
                "settings": {"calendarId": "primary"},
            }
        },
        "active": ["google-calendar"],
    }
    payload.update(over)
    return ConnectorRuntimeConfig.model_validate(payload)


def _config(connectors: ConnectorRuntimeConfig) -> Config:
    """A whole config carrying one connectors block, for a caller that loads config itself."""
    config = Config()
    config.connectors = connectors
    return config


def test_a_configured_connector_activates_with_its_operations_and_defaults() -> None:
    active, problems = resolve_active(_cfg())
    assert problems == []
    assert len(active) == 1
    entry = active[0]
    assert entry.name == "google-calendar"
    assert [op.name for op in entry.operations] == [
        "list_events",
        "list_calendars",
        "get_event",
        "create_event",
    ]
    assert entry.defaults == {"calendarId": "primary"}
    assert entry.credential.name == "google-workspace"


def test_a_manifest_field_default_applies_without_config_repeating_it() -> None:
    """`calendarId: "primary"` is declared by the package, so config need not say it.

    Without this the first call failed with "needs 'calendarId'" for a field the manifest had
    already answered -- a path placeholder with no value.
    """
    cfg = _cfg(
        connectors={"google-calendar": {"credential": "google-workspace"}}
    )
    active, problems = resolve_active(cfg)
    assert problems == []
    assert active[0].defaults == {"calendarId": "primary"}


def test_an_operator_setting_wins_over_the_manifest_default() -> None:
    """Config is the authority over a package."""
    cfg = _cfg(
        connectors={
            "google-calendar": {
                "credential": "google-workspace",
                "settings": {"calendarId": "team@example.test"},
            }
        }
    )
    active, _problems = resolve_active(cfg)
    assert active[0].defaults == {"calendarId": "team@example.test"}


def test_nothing_activates_when_the_operator_asked_for_nothing() -> None:
    active, problems = resolve_active(_cfg(active=[]))
    assert (active, problems) == ([], [])
    assert startup_summary(active, problems) == "connectors: none active"


def test_a_connector_that_is_not_installed_names_what_is() -> None:
    active, problems = resolve_active(_cfg(active=["gmail"]))
    assert active == []
    assert "google-calendar" in problems[0].reason
    assert problems[0].connector == "gmail"


def test_a_connector_with_no_credential_does_not_activate() -> None:
    cfg = _cfg(connectors={"google-calendar": {"credential": ""}})
    active, problems = resolve_active(cfg)
    assert active == []
    assert "names no credential" in problems[0].reason


def test_a_credential_that_does_not_exist_names_the_ones_that_do() -> None:
    cfg = _cfg(connectors={"google-calendar": {"credential": "typo"}})
    active, problems = resolve_active(cfg)
    assert active == []
    assert "google-workspace" in problems[0].reason


def test_a_credential_missing_the_write_scope_refuses_at_activation() -> None:
    """Not at the first write. The mismatch is told to somebody."""
    cfg = _cfg(
        credentials={
            "google-workspace": {
                "clientId": "cid",
                "secretRef": "google-refresh",
                "scopes": [READONLY],
            }
        }
    )
    active, problems = resolve_active(cfg)
    assert active == []
    assert "calendar.events" in problems[0].reason


def test_the_same_read_only_credential_activates_a_connector_capped_at_read() -> None:
    """The ceiling and the credential agree, so this is a working deployment."""
    cfg = _cfg(
        credentials={
            "google-workspace": {
                "clientId": "cid",
                "secretRef": "google-refresh",
                "scopes": [READONLY],
            }
        },
        connectors={"google-calendar": {"credential": "google-workspace", "maxClass": "read"}},
    )
    active, problems = resolve_active(cfg)
    assert problems == []
    assert [op.name for op in active[0].operations] == [
        "list_events",
        "list_calendars",
        "get_event",
    ]
    assert "read" in startup_summary(active, problems)


def test_enabled_operations_naming_something_the_package_lacks_is_refused() -> None:
    cfg = _cfg(
        connectors={
            "google-calendar": {
                "credential": "google-workspace",
                "enabledOperations": ["list_events", "send_mail"],
            }
        }
    )
    active, problems = resolve_active(cfg)
    assert active == []
    assert "send_mail" in problems[0].reason
    assert "list_events" in problems[0].reason


def test_a_configuration_that_leaves_no_operation_is_refused() -> None:
    cfg = _cfg(
        connectors={
            "google-calendar": {
                "credential": "google-workspace",
                "enabledOperations": ["create_event"],
                "maxClass": "read",
            }
        }
    )
    active, problems = resolve_active(cfg)
    assert active == []
    assert "activates no operation" in problems[0].reason


def test_an_unknown_key_in_a_connector_block_is_refused_by_the_schema() -> None:
    """A mistyped key would otherwise become an absent restriction."""
    with pytest.raises(ValueError, match="enabledOperatons"):
        ConnectorRuntimeConfig.model_validate(
            {"connectors": {"google-calendar": {"enabledOperatons": ["list_events"]}}}
        )


# --- registration ----------------------------------------------------------------------


def _ctx(tmp_path: Any) -> ToolContext:
    return ToolContext(config=ToolsConfig(), workspace=str(tmp_path))


def test_registration_adds_one_tool_per_enabled_operation(tmp_path: Any) -> None:
    """Registration builds tools and opens nothing: the executor is named, not contacted."""
    registry = ToolRegistry()
    names = register_connector_tools(_ctx(tmp_path), registry, _cfg())
    assert names == [
        "google_calendar_list_events",
        "google_calendar_list_calendars",
        "google_calendar_get_event",
        "google_calendar_create_event",
    ]
    assert capability_class_of(registry.get("google_calendar_list_events")) == "read"
    assert capability_class_of(registry.get("google_calendar_create_event")) == "mutate.remote"


def test_registration_registers_nothing_when_a_connector_does_not_activate(
    tmp_path: Any,
) -> None:
    registry = ToolRegistry()
    cfg = _cfg(connectors={"google-calendar": {"credential": "typo"}})
    assert register_connector_tools(_ctx(tmp_path), registry, cfg) == []
    assert registry.get("google_calendar_list_events") is None


def test_registration_is_a_no_op_without_config(tmp_path: Any) -> None:
    registry = ToolRegistry()
    assert register_connector_tools(_ctx(tmp_path), registry, None) == []


def test_a_deployment_that_configures_no_connector_is_unchanged() -> None:
    """The default config activates nothing, so nothing about a deployment changes."""
    config = Config()
    assert config.connectors.active == []
    active, problems = resolve_active(config.connectors)
    assert (active, problems) == ([], [])


# --- the skill ---------------------------------------------------------------------------


def test_an_active_connector_contributes_its_skill(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest declares a skill, so the loader has to find it.

    It did not, for the first pass of this work: the sources were workspace, plugin and
    builtin, so a connector's SKILL.md was shipped in the wheel and read by nobody -- while
    the docs told the reader it loads when the connector is enabled.
    """
    from nanoinfra.agent.skills import SkillsLoader

    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *_a, **_k: _config(_cfg()))
    rows = {
        entry["name"]: entry
        for entry in SkillsLoader(tmp_path).list_skills(filter_unavailable=False)
    }
    assert rows["google-calendar"]["source"] == "connector"
    assert rows["google-calendar"]["path"].endswith("google_calendar/SKILL.md")


def test_an_inactive_connector_contributes_no_skill(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill for tools that are not in context would describe a capability and then lack it."""
    from nanoinfra.agent.skills import SkillsLoader

    monkeypatch.setattr(
        "nanoinfra.config.loader.load_config", lambda *_a, **_k: _config(_cfg(active=[]))
    )
    names = {
        entry["name"] for entry in SkillsLoader(tmp_path).list_skills(filter_unavailable=False)
    }
    assert "google-calendar" not in names


def test_the_connector_skill_can_be_disabled_by_its_own_name(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same string an operator wrote in connectors.active turns the skill off."""
    from nanoinfra.agent.skills import SkillsLoader

    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *_a, **_k: _config(_cfg()))
    loader = SkillsLoader(tmp_path, disabled_skills={"google-calendar"})
    names = {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}
    assert "google-calendar" not in names


# --- reload (#194) ---------------------------------------------------------------------


def test_a_reload_registers_what_config_now_says(tmp_path: Any) -> None:
    """Registration runs at boot, so activating afterwards left the halves disagreeing."""
    from nanoinfra.connectors.registration import (
        registered_tool_names,
        reload_connector_tools,
    )

    registry = ToolRegistry()
    ctx = _ctx(tmp_path)

    # Nothing activated yet: no tools, and nothing recorded as registered.
    assert register_connector_tools(ctx, registry, _cfg(active=[])) == []

    result = reload_connector_tools(ctx, registry, _cfg())

    assert result["ok"] is True
    assert "google_calendar_list_events" in result["registered"]
    assert registry.get("google_calendar_list_events") is not None
    assert "google_calendar_list_events" in registered_tool_names()


def test_a_reload_removes_a_tool_the_ceiling_dropped(tmp_path: Any) -> None:
    """A tool that would now refuse must leave the context window, not sit there."""
    from nanoinfra.connectors.registration import reload_connector_tools

    registry = ToolRegistry()
    ctx = _ctx(tmp_path)
    register_connector_tools(ctx, registry, _cfg())
    assert registry.get("google_calendar_create_event") is not None

    capped = _cfg(
        connectors={"google-calendar": {"credential": "google-workspace", "maxClass": "read"}},
        credentials={
            "google-workspace": {
                "clientId": "cid",
                "secretRef": "google-refresh",
                "scopes": [READONLY],
            }
        },
    )
    result = reload_connector_tools(ctx, registry, capped)

    assert "google_calendar_create_event" in result["removed"]
    assert registry.get("google_calendar_create_event") is None
    assert registry.get("google_calendar_list_events") is not None


def test_a_reload_replaces_a_stale_instance(tmp_path: Any) -> None:
    """The operation may now carry different defaults, and the old instance holds the old ones."""
    from nanoinfra.connectors.registration import reload_connector_tools

    registry = ToolRegistry()
    ctx = _ctx(tmp_path)
    register_connector_tools(ctx, registry, _cfg())
    first = registry.get("google_calendar_list_events")

    reload_connector_tools(ctx, registry, _cfg())
    second = registry.get("google_calendar_list_events")

    assert first is not None and second is not None
    assert first is not second


def test_the_payload_reports_the_gap_between_config_and_the_registry(tmp_path: Any) -> None:
    """The operator did the documented thing and the tools were still absent."""
    from nanoinfra.connectors.registration import reload_connector_tools
    from nanoinfra.webui.connectors_api import webui_connectors_payload

    registry = ToolRegistry()
    ctx = _ctx(tmp_path)
    reload_connector_tools(ctx, registry, _cfg(active=[]))

    import nanoinfra.webui.connectors_api as api

    original = api.load_config
    config = _config(_cfg())
    api.load_config = lambda: config  # pyright: ignore[reportAttributeAccessIssue]
    try:
        stale = webui_connectors_payload(tmp_path)
        assert stale["requires_reload"] is True
        assert "google_calendar_list_events" in stale["missing_tools"]

        reload_connector_tools(ctx, registry, _cfg())
        fresh = webui_connectors_payload(tmp_path)
        assert fresh["requires_reload"] is False
        assert fresh["missing_tools"] == []
    finally:
        api.load_config = original  # pyright: ignore[reportAttributeAccessIssue]
