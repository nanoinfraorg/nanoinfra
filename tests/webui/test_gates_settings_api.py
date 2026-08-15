"""Gate policy settings API -- nanoinfraorg/nanoinfra#26.

The panel reads the effective policy plus a per-field origin marker. It writes the policy back
through ``GatesConfig``, so an invalid policy never reaches config.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from websockets.datastructures import Headers

from nanoinfra.config.loader import load_config, save_config
from nanoinfra.config.schema import Config
from nanoinfra.webui.http_utils import http_json_response
from nanoinfra.webui.settings_api import (
    WebUISettingsError,
    settings_payload,
    update_gates_settings,
)
from nanoinfra.webui.settings_routes import WebUISettingsRouter

GATES_ROUTE = "/api/settings/gates/update"
GATES_HEADER = "X-Nanoinfra-Gates-Values"


def _use_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: object) -> Path:
    """Point the loader at a config.json that holds exactly ``raw``."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", path)
    return path


def _gates_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: object) -> dict[str, object]:
    _use_config(tmp_path, monkeypatch, raw)
    gates = settings_payload()["advanced"]["gates"]
    assert isinstance(gates, dict)
    return gates


def _policy_query(policy: object) -> dict[str, list[str]]:
    return {"policy": [json.dumps(policy)]}


def _saved_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Return the policy the panel shows for a config with no gates block."""
    return dict(_gates_block(tmp_path, monkeypatch, {})["policy"])  # type: ignore[arg-type]


def _router() -> WebUISettingsRouter:
    return WebUISettingsRouter(
        bus=SimpleNamespace(),
        logger=SimpleNamespace(exception=lambda *_args: None),
        check_api_token=lambda _request: True,
        parse_query=lambda path: parse_qs(urlsplit(path).query),
        json_response=http_json_response,
        error_response=lambda status, message: http_json_response(
            {"error": message},
            status=status,
        ),
        runtime_surface="browser",
        runtime_capabilities={},
    )


def test_gates_payload_marks_every_shipped_default_as_a_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates = _gates_block(tmp_path, monkeypatch, {})

    assert gates["from_default"] == {
        "approvers": True,
        "approvalPaths": True,
        "interactive.mutate.remote.host": True,
        "interactive.mutate.remote.group": True,
        "interactive.mutate.remote.all": True,
        "interactive.mutate.inventory": True,
        "interactive.credential.access": True,
        "unattended.mutate.remote.host": True,
        "unattended.mutate.remote.group": True,
        "unattended.mutate.remote.all": True,
        "unattended.mutate.inventory": True,
        "unattended.credential.access": True,
        "standingGrants": True,
        "audit.retentionDays": True,
        "audit.recordCommandText": True,
    }


def test_gates_payload_drops_the_default_marker_for_a_configured_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = {
        "gates": {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "audit": {"retentionDays": 30},
        }
    }

    gates = _gates_block(tmp_path, monkeypatch, raw)
    markers = gates["from_default"]
    assert isinstance(markers, dict)

    assert markers["unattended.mutate.remote.host"] is False
    assert markers["audit.retentionDays"] is False
    assert markers["unattended.mutate.remote.group"] is True
    assert markers["audit.recordCommandText"] is True
    assert markers["interactive.mutate.remote.host"] is True


def test_gates_payload_keeps_the_config_key_spelling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates = _gates_block(tmp_path, monkeypatch, {})

    assert gates["policy"] == {
        "approvers": [],
        "approvalPaths": ["webui"],
        "interactive": {
            "mutate.remote": {"host": "approve", "group": "approve", "all": "deny"},
            "mutate.inventory": "allow",
            "credential.access": "approve",
        },
        "unattended": {
            "mutate.remote": {"host": "deny", "group": "deny", "all": "deny"},
            "mutate.inventory": "deny",
            "credential.access": "deny",
        },
        "standingGrants": [],
        "audit": {"retentionDays": 90, "recordCommandText": False},
    }


def test_gates_payload_offers_deny_alone_for_all_hosts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates = _gates_block(tmp_path, monkeypatch, {})
    choices = gates["choices"]
    assert isinstance(choices, dict)

    assert choices["all"] == ["deny"]


def test_gates_payload_takes_decision_choices_from_the_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gates = _gates_block(tmp_path, monkeypatch, {})

    assert gates["choices"] == {
        "mutate.remote": ["allow", "approve", "grant", "deny"],
        "mutate.inventory": ["allow", "deny"],
        "credential.access": ["approve", "deny"],
        "all": ["deny"],
    }


def test_update_gates_settings_writes_a_standing_grant_to_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["standingGrants"] = [
        {
            "contexts": ["unattended"],
            "hosts": ["staging-web-01"],
            "commands": ["systemctl reload nginx"],
        }
    ]

    payload = update_gates_settings(_policy_query(policy))

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["gates"]["standingGrants"] == [
        {
            "id": None,
            "contexts": ["unattended"],
            "hosts": ["staging-web-01"],
            "commands": ["systemctl reload nginx"],
        }
    ]
    grants = load_config(path).gates.standing_grants
    assert [grant.commands for grant in grants] == [["systemctl reload nginx"]]
    reloaded = payload["advanced"]["gates"]["policy"]["standingGrants"]
    assert reloaded == raw["gates"]["standingGrants"]
    assert payload["advanced"]["gates"]["from_default"]["standingGrants"] is False


def test_update_gates_settings_asks_for_a_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["approvers"] = [{"channel": "webui", "sender": "operator-1"}]

    payload = update_gates_settings(_policy_query(policy))

    assert payload["requires_restart"] is True


def test_update_gates_settings_refuses_a_widened_all_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["interactive"]["mutate.remote"]["all"] = "approve"  # type: ignore[index]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.interactive.mutate.remote.all" in excinfo.value.message
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_update_gates_settings_refuses_an_unknown_policy_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["allowCommnads"] = ["systemctl reload nginx"]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.allowCommnads" in excinfo.value.message
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_update_gates_settings_refuses_an_empty_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["standingGrants"] = [
        {"contexts": ["unattended"], "hosts": ["staging-web-01"], "commands": ["   "]}
    ]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.standingGrants[0].commands" in excinfo.value.message
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_update_gates_settings_refuses_a_grant_without_a_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["standingGrants"] = [
        {"contexts": ["unattended"], "hosts": [], "commands": ["systemctl reload nginx"]}
    ]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.standingGrants[0].hosts" in excinfo.value.message


def test_update_gates_settings_refuses_a_grant_without_a_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["standingGrants"] = [
        {"contexts": [], "hosts": ["staging-web-01"], "commands": ["systemctl reload nginx"]}
    ]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.standingGrants[0].contexts" in excinfo.value.message


def test_update_gates_settings_refuses_a_blank_approver_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["approvers"] = [{"channel": " ", "sender": "operator-1"}]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.approvers[0].channel" in excinfo.value.message


def test_update_gates_settings_refuses_an_empty_approval_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["approvalPaths"] = ["webui", ""]

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.approvalPaths[1]" in excinfo.value.message


def test_update_gates_settings_needs_a_policy_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})

    with pytest.raises(WebUISettingsError, match="policy is required"):
        update_gates_settings({})

    with pytest.raises(WebUISettingsError, match="policy must be a JSON object"):
        update_gates_settings({"policy": ["[]"]})

    with pytest.raises(WebUISettingsError, match="policy must be a JSON object"):
        update_gates_settings({"policy": ["not json"]})


def test_update_gates_settings_trims_grant_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["approvalPaths"] = [" webui ", "telegram"]
    policy["standingGrants"] = [
        {
            "contexts": ["unattended"],
            "hosts": [" staging-web-01 "],
            "commands": [" systemctl reload nginx "],
        }
    ]

    update_gates_settings(_policy_query(policy))

    gates = load_config(path).gates
    assert gates.approval_paths == ["webui", "telegram"]
    assert gates.standing_grants[0].hosts == ["staging-web-01"]
    assert gates.standing_grants[0].commands == ["systemctl reload nginx"]


def test_update_gates_settings_refuses_a_retention_below_one_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["audit"] = {"retentionDays": 0, "recordCommandText": False}

    with pytest.raises(WebUISettingsError) as excinfo:
        update_gates_settings(_policy_query(policy))

    assert "gates.audit.retentionDays" in excinfo.value.message


def test_update_gates_settings_reads_a_percent_encoded_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["standingGrants"] = [
        {
            "contexts": ["unattended"],
            "hosts": ["staging-wéb-01"],
            "commands": ["systemctl reload nginx"],
        }
    ]

    update_gates_settings({"policy": [quote(json.dumps(policy))]})

    assert load_config(path).gates.standing_grants[0].hosts == ["staging-wéb-01"]


def test_update_gates_settings_keeps_the_rest_of_the_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config()
    config.api.port = 9911
    path = tmp_path / "config.json"
    save_config(config, path)
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", path)
    policy = dict(settings_payload()["advanced"]["gates"]["policy"])
    policy["audit"] = {"retentionDays": 14, "recordCommandText": True}

    update_gates_settings(_policy_query(policy))

    saved = load_config(path)
    assert saved.api.port == 9911
    assert saved.gates.audit.retention_days == 14
    assert saved.gates.audit.record_command_text is True


@pytest.mark.asyncio
async def test_gates_route_saves_the_policy_from_the_values_header(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["approvers"] = [{"channel": "webui", "sender": "operator-1"}]
    request = SimpleNamespace(
        path=GATES_ROUTE,
        headers=Headers([(GATES_HEADER, json.dumps(policy))]),
    )

    response = await _router().dispatch(None, request, GATES_ROUTE)

    assert response is not None
    assert response.status_code == 200
    body = json.loads(response.body)
    assert body["requires_restart"] is True
    assert body["restart_required_sections"] == ["runtime"]
    assert load_config(path).gates.approvers[0].sender == "operator-1"


@pytest.mark.asyncio
async def test_gates_route_reports_the_named_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _use_config(tmp_path, monkeypatch, {})
    policy = _saved_policy(tmp_path, monkeypatch)
    policy["unattended"]["mutate.remote"]["all"] = "allow"  # type: ignore[index]
    request = SimpleNamespace(
        path=GATES_ROUTE,
        headers=Headers([(GATES_HEADER, json.dumps(policy))]),
    )

    response = await _router().dispatch(None, request, GATES_ROUTE)

    assert response is not None
    assert response.status_code == 400
    assert "gates.unattended.mutate.remote.all" in json.loads(response.body)["error"]
    assert json.loads(path.read_text(encoding="utf-8")) == {}


@pytest.mark.asyncio
async def test_gates_route_refuses_an_oversized_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_config(tmp_path, monkeypatch, {})
    request = SimpleNamespace(
        path=GATES_ROUTE,
        headers=Headers([(GATES_HEADER, "x" * (64 * 1024 + 1))]),
    )

    response = await _router().dispatch(None, request, GATES_ROUTE)

    assert response is not None
    assert response.status_code == 400
    assert "too large" in json.loads(response.body)["error"]
