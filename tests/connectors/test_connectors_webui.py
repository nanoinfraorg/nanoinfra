"""The Apps row's payload, and the one action it offers.

The row has to answer what an operator cannot get anywhere else, so these tests are about the
three kinds of fact it carries: what the gate will answer per operation and per context, which
scopes the consent actually granted, and whether the thing has ever worked. A payload that only
said "enabled" would pass no test here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.config.connectors import ConnectorRuntimeConfig
from nanoinfra.config.gates import GatesConfig
from nanoinfra.config.schema import Config
from nanoinfra.connectors import state as connector_state
from nanoinfra.gates.executor.protocol import ExecuteResponse
from nanoinfra.webui.connectors_api import (
    STATE_ACTIVE,
    STATE_INACTIVE,
    STATE_NOT_ACTIVATED,
    TEST_RESULT_LIMIT,
    connector_test,
    webui_connectors_payload,
)
from nanoinfra.webui.settings_api import WebUISettingsError

READONLY = "https://www.googleapis.com/auth/calendar.readonly"
EVENTS = "https://www.googleapis.com/auth/calendar.events"
CALENDAR = "google-calendar"


def _connectors(**over: Any) -> ConnectorRuntimeConfig:
    payload: dict[str, Any] = {
        "credentials": {
            "google_workspace": {
                "clientId": "cid.apps.googleusercontent.test",
                "secretRef": "refresh-1",
                "clientSecretRef": "client-1",
                "scopes": [READONLY, EVENTS],
            }
        },
        "connectors": {"google-calendar": {"credential": "google_workspace"}},
        "active": [CALENDAR],
    }
    payload.update(over)
    return ConnectorRuntimeConfig.model_validate(payload)


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A whole config with one connectors block and a policy, loaded from memory."""

    def install(
        connectors: ConnectorRuntimeConfig | None = None, gates: GatesConfig | None = None
    ) -> Config:
        config = Config()
        config.connectors = connectors if connectors is not None else _connectors()
        config.agents.defaults.workspace = str(tmp_path)
        monkeypatch.setattr("nanoinfra.webui.connectors_api.load_config", lambda: config)
        # The mention resolver reads the deployment's config through the loader, because a
        # connector's objects belong to the deployment rather than to a personal workspace.
        monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *_a, **_k: config)
        monkeypatch.setattr(
            "nanoinfra.webui.connectors_api.load_policy", lambda: gates or GatesConfig()
        )
        return config

    return install


def _row(payload: dict[str, Any], name: str = CALENDAR) -> dict[str, Any]:
    return next(row for row in payload["connectors"] if row["name"] == name)


# --- the posture -----------------------------------------------------------------------


def test_the_row_carries_the_gate_answer_per_operation_and_context(
    configured: Any, tmp_path: Path
) -> None:
    """The asymmetry the kind exists for, as data the row can render."""
    configured()
    row = _row(webui_connectors_payload(tmp_path))

    operations = {op["name"]: op for op in row["operations"]}
    assert operations["list_events"]["interactive"]["outcome"] == "allow"
    assert operations["list_events"]["unattended"]["outcome"] == "allow"
    assert operations["create_event"]["interactive"]["outcome"] == "approve"
    assert operations["create_event"]["unattended"]["outcome"] == "deny"
    # The refusal text travels too, because that is what tells an operator what to change --
    # and with the shipped default that is the matrix cell rather than a grant, because a grant
    # beside a `deny` cell changes nothing.
    assert (
        "gates.unattended.mutate.remote.host"
        in operations["create_event"]["unattended"]["reason"]
    )


def test_a_grant_that_covers_a_write_is_named_in_the_row(configured: Any, tmp_path: Path) -> None:
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "cal-write",
                    "contexts": ["unattended"],
                    "connectors": [CALENDAR],
                    "operations": ["create_event"],
                }
            ],
        }
    )
    configured(gates=gates)
    row = _row(webui_connectors_payload(tmp_path))

    write = next(op for op in row["operations"] if op["name"] == "create_event")
    assert write["unattended"]["outcome"] == "allow"
    assert write["unattended"]["grant_id"] == "cal-write"


def test_the_row_says_which_scopes_were_granted_and_for_which_class(
    configured: Any, tmp_path: Path
) -> None:
    """A scope that was never granted is invisible until a call fails."""
    configured(
        _connectors(
            credentials={
                "google_workspace": {
                    "clientId": "cid",
                    "secretRef": "refresh-1",
                    "scopes": [READONLY],
                }
            },
            connectors={"google-calendar": {"credential": "google_workspace", "maxClass": "read"}},
        )
    )
    row = _row(webui_connectors_payload(tmp_path))

    scopes = {entry["short"]: entry for entry in row["scopes"]}
    assert scopes["calendar.readonly"]["granted"] is True
    assert scopes["calendar.readonly"]["capability_class"] == "read"
    assert scopes["calendar.events"]["granted"] is False
    assert scopes["calendar.events"]["capability_class"] == "mutate.remote"


def test_a_ceiling_shows_the_operations_it_removed_as_not_enabled(
    configured: Any, tmp_path: Path
) -> None:
    configured(
        _connectors(
            connectors={"google-calendar": {"credential": "google_workspace", "maxClass": "read"}}
        )
    )
    row = _row(webui_connectors_payload(tmp_path))

    enabled = {op["name"]: op["enabled"] for op in row["operations"]}
    assert enabled == {
        "list_events": True,
        "list_calendars": True,
        "get_event": True,
        "freebusy": True,
        "create_event": False,
        "update_event": False,
        "delete_event": False,
    }
    assert row["max_class"] == "read"


def test_the_three_states_are_distinguishable(configured: Any, tmp_path: Path) -> None:
    """`active`, `not_activated` and `inactive` are three different situations.

    Only the middle one is a problem, and it is the one that carries a fix.
    """
    configured()
    assert _row(webui_connectors_payload(tmp_path))["state"] == STATE_ACTIVE

    configured(_connectors(active=[]))
    assert _row(webui_connectors_payload(tmp_path))["state"] == STATE_INACTIVE

    configured(_connectors(connectors={"google-calendar": {"credential": "typo"}}))
    row = _row(webui_connectors_payload(tmp_path))
    assert row["state"] == STATE_NOT_ACTIVATED
    assert "typo" in row["problem"]


def test_the_row_offers_no_enable_and_names_where_activation_lives(
    configured: Any, tmp_path: Path
) -> None:
    """Enabling a connector gives a package a token and a class, so config owns it."""
    configured()
    payload = webui_connectors_payload(tmp_path)

    assert payload["activation_key"] == "connectors.active"
    # No row carries an action that would activate anything. `enabled` per operation is a
    # statement about what config already says, not a control.
    row = _row(payload)
    assert not {"enable", "install_supported", "activate"} & set(row)


def test_the_form_fields_come_from_the_manifest(configured: Any, tmp_path: Path) -> None:
    """Nothing per-connector is hand-written in the shared UI."""
    configured()
    row = _row(webui_connectors_payload(tmp_path))

    fields = {field["name"]: field for field in row["setup_fields"]}
    assert fields["clientId"]["required"] is True
    assert fields["clientSecret"]["secret"] is True
    assert fields["calendarId"]["default"] == "primary"
    assert row["official_url"].startswith("https://console.cloud.google.com")


def test_the_row_carries_no_secret_value(configured: Any, tmp_path: Path) -> None:
    configured()
    text = json.dumps(webui_connectors_payload(tmp_path))

    # References only: the ids of the secrets, never their values, and no token.
    assert "refresh-1" not in text
    assert "client-1" not in text
    assert "access_token" not in text


def test_the_recorded_facts_reach_the_row(configured: Any, tmp_path: Path) -> None:
    configured()
    connector_state.record(
        tmp_path,
        CALENDAR,
        acts_as="alberto@example.test",
        refreshed_at="2026-08-30T03:00:00+00:00",
        tested_at="2026-08-30T03:01:00+00:00",
        test_summary="list_events returned 3 items",
    )
    row = _row(webui_connectors_payload(tmp_path))

    assert row["acts_as"] == "alberto@example.test"
    assert row["refreshed_at"].startswith("2026-08-30")
    assert row["test_summary"] == "list_events returned 3 items"


def test_a_connector_with_no_record_says_nothing_rather_than_implying_a_failure(
    configured: Any, tmp_path: Path
) -> None:
    configured()
    row = _row(webui_connectors_payload(tmp_path))

    assert row["acts_as"] == ""
    assert row["refreshed_at"] == ""
    assert row["last_error"] == ""


# --- the test action -------------------------------------------------------------------


class _FakeExecutor:
    def __init__(self, response: ExecuteResponse) -> None:
        self.calls: list[dict[str, Any]] = []
        self._response = response

    def connector_call(self, **kwargs: Any) -> ExecuteResponse:
        self.calls.append(kwargs)
        return self._response


@pytest.fixture
def executor(monkeypatch: pytest.MonkeyPatch):
    def install(response: ExecuteResponse) -> _FakeExecutor:
        fake = _FakeExecutor(response)
        monkeypatch.setattr(
            "nanoinfra.gates.executor.client.ExecutorClient", lambda *_a, **_k: fake
        )
        return fake

    return install


def _ok(output: str) -> ExecuteResponse:
    return ExecuteResponse(ok=True, output=output, exit_code=0, error=None, reason="")


def test_the_test_action_reads_through_the_executor_and_bounds_the_result(
    configured: Any, executor: Any, tmp_path: Path
) -> None:
    """A pass means the real path works, because it uses the real path."""
    configured()
    fake = executor(
        _ok(json.dumps({"items": [{"id": "e1"}, {"id": "e2"}, {"id": "e3"}]}))
    )
    result = connector_test(CALENDAR, workspace_path=tmp_path)

    assert result["ok"] is True
    assert result["operation"] == "list_events"
    assert "3 items" in result["summary"]
    sent = fake.calls[0]
    assert sent["connector"] == CALENDAR
    assert sent["preview_requested"] is False
    assert json.loads(sent["arguments_json"]) == {"maxResults": TEST_RESULT_LIMIT}


def test_the_test_learns_the_account_and_records_it(
    configured: Any, executor: Any, tmp_path: Path
) -> None:
    """`acts as` comes from a call rather than from an identity scope nobody needs."""
    configured()
    # The shape Google actually returns: the primary calendar's own `summary` is the account
    # address, and the projection keeps the envelope's scalars.
    executor(_ok(json.dumps({"summary": "alberto@example.test", "items": [{"id": "e1"}]})))
    result = connector_test(CALENDAR, workspace_path=tmp_path)

    assert result["acts_as"] == "alberto@example.test"
    assert connector_state.read_one(tmp_path, CALENDAR).acts_as == "alberto@example.test"
    assert connector_state.read_one(tmp_path, CALENDAR).tested_at


def test_a_failed_test_records_the_reason_for_the_row(
    configured: Any, executor: Any, tmp_path: Path
) -> None:
    configured()
    executor(
        ExecuteResponse(
            ok=False,
            output="",
            exit_code=None,
            error="the credential no longer works",
            reason="",
            terminal=False,
        )
    )
    result = connector_test(CALENDAR, workspace_path=tmp_path)

    assert result["ok"] is False
    assert "no longer works" in result["message"]
    assert "no longer works" in connector_state.read_one(tmp_path, CALENDAR).last_error


def test_testing_an_inactive_connector_is_a_404_that_says_why(
    configured: Any, tmp_path: Path
) -> None:
    configured(_connectors(connectors={"google-calendar": {"credential": "typo"}}))

    with pytest.raises(WebUISettingsError) as raised:
        connector_test(CALENDAR, workspace_path=tmp_path)
    assert raised.value.status == 404
    assert "typo" in raised.value.message


def test_a_connector_capped_to_writes_only_is_not_testable(
    configured: Any, tmp_path: Path
) -> None:
    """The test is one read. Nothing here writes to a real account to prove a credential."""
    configured(
        _connectors(
            connectors={
                "google-calendar": {
                    "credential": "google_workspace",
                    "enabledOperations": ["create_event"],
                }
            }
        )
    )
    row = _row(webui_connectors_payload(tmp_path))
    assert row["testable"] is False

    with pytest.raises(WebUISettingsError) as raised:
        connector_test(CALENDAR, workspace_path=tmp_path)
    assert raised.value.status == 400
    assert "read operation" in raised.value.message


# --- mentions --------------------------------------------------------------------------


def test_the_mention_kinds_include_what_the_connectors_declare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`@calendar:` exists because a manifest says so, not because a list was edited."""
    from nanoinfra.webui.resource_mentions import connector_mention_kinds, mention_kinds

    config = Config()
    config.connectors = _connectors()
    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *_a, **_k: config)

    assert "calendar" in connector_mention_kinds()
    assert {"server", "diagram", "calendar"} <= mention_kinds()


def test_an_inactive_connector_offers_no_mention_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from nanoinfra.webui.resource_mentions import mention_kinds

    config = Config()
    config.connectors = _connectors(active=[])
    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *_a, **_k: config)

    assert "calendar" not in mention_kinds()


def test_a_connector_mention_resolves_from_the_cache_with_the_call_to_make(
    configured: Any, executor: Any, tmp_path: Path
) -> None:
    """A pinned id is useful only if the block says which call takes it."""
    from nanoinfra.webui.connectors_api import connector_objects
    from nanoinfra.webui.resource_mentions import ResourceMentionResolver

    configured()
    executor(
        _ok(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "team@group.calendar.google.com",
                            "summary": "Team calendar",
                            "accessRole": "owner",
                            "timeZone": "America/Mexico_City",
                        }
                    ]
                }
            )
        )
    )
    listed = connector_objects(workspace_path=tmp_path)
    assert [obj["id"] for obj in listed["objects"]] == ["team@group.calendar.google.com"]
    assert listed["objects"][0]["argument"] == "calendarId"
    # The tools that take the id, and never the listing that produced it.
    assert "google_calendar_list_events" in listed["objects"][0]["tools"]
    assert "google_calendar_list_calendars" not in listed["objects"][0]["tools"]

    resolution = ResourceMentionResolver(tmp_path).resolve(
        [
            {"kind": "calendar", "id": "team@group.calendar.google.com"},
            {"kind": "calendar", "id": "gone@group.calendar.google.com"},
        ]
    )
    assert [mention.name for mention in resolution.resolved] == ["Team calendar"]
    assert resolution.resolved[0].summary["use"] == (
        "pass calendarId=team@group.calendar.google.com to google_calendar_list_events"
    )
    # An id the cache does not know is refused, the same way a deleted server is.
    assert resolution.missing == [("calendar", "gone@group.calendar.google.com")]


def test_resolution_makes_no_call(configured: Any, tmp_path: Path) -> None:
    """A mention is resolved on every send, so the send path must not depend on an API."""
    from nanoinfra.webui.resource_mentions import ResourceMentionResolver

    configured()
    connector_state.record(
        tmp_path,
        CALENDAR,
        objects=json.dumps(
            [
                {
                    "connector": CALENDAR,
                    "kind": "calendar",
                    "id": "primary",
                    "name": "Primary",
                    "detail": "owner",
                    "argument": "calendarId",
                    "tool": "google_calendar_list_events",
                }
            ]
        ),
    )
    # No executor is patched here at all: a call would raise rather than resolve.
    resolution = ResourceMentionResolver(tmp_path).resolve([{"kind": "calendar", "id": "primary"}])
    assert [mention.id for mention in resolution.resolved] == ["primary"]


def test_a_listing_that_fails_keeps_the_objects_it_had(
    configured: Any, executor: Any, tmp_path: Path
) -> None:
    """One unreachable API must not empty a menu that worked a minute ago."""
    from nanoinfra.webui.connectors_api import connector_objects

    configured()
    connector_state.record(
        tmp_path,
        CALENDAR,
        objects=json.dumps(
            [{"connector": CALENDAR, "kind": "calendar", "id": "primary", "name": "Primary"}]
        ),
    )
    executor(
        ExecuteResponse(
            ok=False, output="", exit_code=None, error="429 slow down", reason="", terminal=False
        )
    )
    listed = connector_objects(workspace_path=tmp_path)

    assert [obj["id"] for obj in listed["objects"]] == ["primary"]
    assert listed["problems"][0]["message"].startswith("429")
