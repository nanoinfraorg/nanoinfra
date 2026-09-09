"""``channels.<name>.agent`` is refused at load, not at answer time.

Mirroring ``AgentsConfig._delegates_must_exist``: an operator who mistypes should be told by the
config that refuses, not by a channel that answers as the deployment default for a week.

The check lives on the root ``Config`` rather than on ``ChannelsConfig``, for the same reason
``_validate_model_preset`` does: the roster is a sibling of ``channels``, and a validator on the
channel model cannot see it.

The resolver half is next door in `tests/agent/test_channel_agent.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from nanoinfra.channels.contracts import CHANNEL_AGENT_REFUSED
from nanoinfra.config.schema import Config, channel_agent_binding

ROSTER: dict[str, Any] = {"named": {"sre": {"description": "prod, read-only"}}}


def _config(channels: dict[str, Any], agents: dict[str, Any] | None = None) -> Config:
    return Config(channels=channels, agents=agents if agents is not None else ROSTER)


# --- accepted -----------------------------------------------------------------------------------


def test_a_binding_naming_a_configured_agent_loads() -> None:
    config = _config({"telegram": {"agent": "sre"}})
    assert channel_agent_binding(config.channels.model_extra["telegram"]) == "sre"


def test_a_channel_with_no_binding_loads() -> None:
    config = _config({"telegram": {"enabled": True}})
    assert channel_agent_binding(config.channels.model_extra["telegram"]) is None


def test_an_empty_binding_loads_and_reads_as_no_binding() -> None:
    """This is what the WebUI writes when the picker returns to *Default agent*.

    It accepts rather than refuses, because a config that says ``"agent": ""`` by hand means the
    same thing an operator means by choosing the default.
    """
    config = _config({"telegram": {"agent": ""}})
    assert channel_agent_binding(config.channels.model_extra["telegram"]) is None


# --- refused ------------------------------------------------------------------------------------


def test_a_binding_naming_an_agent_that_does_not_exist_is_refused() -> None:
    with pytest.raises(ValidationError) as caught:
        _config({"telegram": {"agent": "ghost"}})
    message = str(caught.value)
    assert "channels.telegram.agent" in message
    # The name is in the message, because an operator who mistyped needs to see what they typed.
    assert "'ghost'" in message


def test_the_binding_is_refused_on_websocket() -> None:
    """The WebUI composer chooses per message, and omits the key when it chooses nothing.

    So a channel-wide default would answer as that agent for every turn where the operator chose
    nothing -- the opposite of "the WebUI takes what the user sends". Refused rather than ignored,
    in the house style: an unknown key is a refusal, not a dropped field.
    """
    with pytest.raises(ValidationError) as caught:
        _config({CHANNEL_AGENT_REFUSED: {"agent": "sre"}})
    message = str(caught.value)
    assert f"channels.{CHANNEL_AGENT_REFUSED}.agent" in message
    # The message says why, rather than only that it was refused.
    assert "composer" in message


def test_websocket_without_the_field_still_loads() -> None:
    assert _config({CHANNEL_AGENT_REFUSED: {"enabled": True}}) is not None


def test_a_binding_is_refused_when_the_deployment_names_no_agent_at_all() -> None:
    """The common first mistake: the field set before the roster exists."""
    with pytest.raises(ValidationError):
        _config({"telegram": {"agent": "sre"}}, agents={})


# --- the reader ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "expected"),
    [
        ({"agent": "sre"}, "sre"),
        ({"agent": "  sre  "}, "sre"),
        ({"agent": ""}, None),
        ({"agent": "   "}, None),
        ({"agent": None}, None),
        ({"agent": 7}, None),
        ({}, None),
    ],
)
def test_the_reader_treats_blank_and_non_string_as_no_binding(
    section: dict[str, Any], expected: str | None
) -> None:
    """One reader for the validator and the loop, so the two cannot disagree about empty."""
    assert channel_agent_binding(section) == expected
