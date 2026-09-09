"""Saving a channel's agent binding through **Settings → Channels**.

The coercion is unit-testable on its own, and the part that is not is the one worth a test: the
*clear*. `_SKIP_FIELD` is the right answer for a blank secret box, which means "keep the stored
one". It is the wrong answer for a picker whose empty option is a choice. So an operator moving
the picker from `sre` back to *Default agent* has to remove the key, and a save that skipped the
field would leave `sre` bound behind a form saying it is not.

`load_config` and `save_config` are both replaced here. The route writes config to disk, and a
test that let it would rewrite the developer's own deployment.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from nanoinfra.config.schema import Config
from nanoinfra.webui import settings_routes as routes
from nanoinfra.webui.http_utils import http_json_response
from nanoinfra.webui.settings_routes import WebUISettingsError, WebUISettingsRouter

AGENT_KEY = "channels.telegram.agent"


def _router() -> WebUISettingsRouter:
    return WebUISettingsRouter(
        bus=SimpleNamespace(),
        logger=SimpleNamespace(exception=lambda *_args: None),
        check_api_token=lambda _request: True,
        parse_query=lambda path: parse_qs(urlsplit(path).query),
        json_response=http_json_response,
        error_response=lambda status, message: http_json_response(
            {"error": message}, status=status
        ),
        runtime_surface="browser",
        runtime_capabilities={},
    )


@pytest.fixture
def saved(monkeypatch: pytest.MonkeyPatch):
    """Serve one config to the route and capture what it writes back."""
    written: list[Config] = []

    def make(telegram: dict[str, Any]) -> None:
        config = Config(
            channels={"telegram": telegram},
            agents={
                "named": {
                    "sre": {"description": "prod, read-only"},
                    "db": {"description": "postgres"},
                }
            },
        )
        monkeypatch.setattr(routes, "load_config", lambda: config)
        monkeypatch.setattr(routes, "save_config", lambda cfg: written.append(cfg))

    return SimpleNamespace(make=make, written=written)


def _telegram(config: Config) -> dict[str, Any]:
    section = getattr(config.channels, "telegram")
    if hasattr(section, "model_dump"):
        return json.loads(section.model_dump_json(by_alias=True))
    return dict(section)


# --- setting ------------------------------------------------------------------------------------


def test_naming_an_agent_writes_the_field(saved) -> None:
    saved.make({"enabled": True, "token": "t"})

    written = _router()._save_channel_config_values("telegram", {AGENT_KEY: "sre"})

    assert written == [AGENT_KEY]
    assert _telegram(saved.written[-1])["agent"] == "sre"


def test_the_route_accepts_the_bare_field_name_too(saved) -> None:
    """The panel sends the prefixed key. The route has always accepted either."""
    saved.make({"enabled": True, "token": "t"})

    _router()._save_channel_config_values("telegram", {"agent": "db"})

    assert _telegram(saved.written[-1])["agent"] == "db"


def test_switching_agents_replaces_the_previous_one(saved) -> None:
    saved.make({"enabled": True, "token": "t", "agent": "sre"})

    _router()._save_channel_config_values("telegram", {AGENT_KEY: "db"})

    assert _telegram(saved.written[-1])["agent"] == "db"


# --- clearing -----------------------------------------------------------------------------------


def test_returning_to_the_default_removes_the_key(saved) -> None:
    """The test this module exists for.

    Not `"agent": null` and not `"agent": ""` -- the key goes, so the config file carries no
    trace of a binding that was withdrawn.
    """
    saved.make({"enabled": True, "token": "t", "agent": "sre"})

    written = _router()._save_channel_config_values("telegram", {AGENT_KEY: ""})

    assert written == [AGENT_KEY]
    assert "agent" not in _telegram(saved.written[-1])


def test_clearing_a_binding_that_was_never_set_is_not_an_error(saved) -> None:
    saved.make({"enabled": True, "token": "t"})

    written = _router()._save_channel_config_values("telegram", {AGENT_KEY: ""})

    assert written == [AGENT_KEY]
    assert "agent" not in _telegram(saved.written[-1])


def test_clearing_leaves_the_other_fields_alone(saved) -> None:
    """A one-field write must not become a rewrite of the channel."""
    saved.make({"enabled": True, "token": "t", "agent": "sre", "groupPolicy": "open"})

    _router()._save_channel_config_values("telegram", {AGENT_KEY: ""})

    section = _telegram(saved.written[-1])
    assert section["token"] == "t"
    assert section["groupPolicy"] == "open"
    assert section["enabled"] is True


# --- refusing -----------------------------------------------------------------------------------


def test_an_agent_absent_from_the_roster_is_refused(saved) -> None:
    """Checked against the roster as it is now, not against a set baked into the field spec.

    The roster is editable in the neighbouring panel, so a form drawn before an agent was deleted
    has to be refused here rather than written.
    """
    saved.make({"enabled": True, "token": "t"})

    with pytest.raises(WebUISettingsError) as caught:
        _router()._save_channel_config_values("telegram", {AGENT_KEY: "ghost"})

    message = str(caught.value)
    assert "ghost" in message
    # The names that would work are in the message, because the operator cannot see the roster
    # from the error alone.
    assert "sre" in message and "db" in message
    assert saved.written == []


def test_websocket_does_not_offer_the_field_at_all(saved) -> None:
    """No agent field on that contract, so the route refuses it as unconfigurable.

    Config refuses `channels.websocket.agent` when it loads. This is the same refusal one layer
    out: the WebUI cannot write the key even by asking.
    """
    saved.make({"enabled": True, "token": "t"})

    with pytest.raises(WebUISettingsError):
        _router()._save_channel_config_values("websocket", {"channels.websocket.agent": "sre"})
