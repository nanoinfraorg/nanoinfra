"""A channel names the agent that answers it (``channels.<name>.agent``).

Named agents shipped with three ways to choose one -- the composer's picker, an automation's
binding, and `@agent:<name>` in the text -- and no way to say "everything arriving on Telegram is
answered by `sre`". On nine of the ten channels every turn was answered by the deployment default
unless the sender happened to type a mention, so an operator who built a narrowed agent for a
channel could not bind it.

This module covers the resolver. The config half -- an unknown name refused at load, and the field
refused outright on websocket -- is next door in `tests/config/test_channel_agent_config.py`.

The ordering is the whole design, so each rule gets its own test:

    metadata["agent"]  >  @agent:<name>  >  channels.<name>.agent  >  agents.defaults
"""

from __future__ import annotations

from typing import Any

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.config.schema import ChannelsConfig, NamedAgentConfig


class _Loop:
    """Just enough of the loop to ask it who answers: the roster and the channels config."""

    def __init__(self, *names: str, channels: dict[str, Any] | None = None) -> None:
        self.named_agents: dict[str, NamedAgentConfig] = {
            name: NamedAgentConfig(description=name) for name in names
        }
        self.channels_config = ChannelsConfig(**(channels or {}))

    _acting_agent_for = AgentLoop._acting_agent_for
    _agent_from_mention = AgentLoop._agent_from_mention
    _agent_from_channel = AgentLoop._agent_from_channel


def _who(
    *names: str,
    channel: str | None = None,
    channels: dict[str, Any] | None = None,
    text: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str | None:
    loop = _Loop(*names, channels=channels)
    return loop._acting_agent_for(metadata, text, channel)


# --- the binding answers ------------------------------------------------------------------------


def test_a_channel_binding_answers_a_turn_that_names_nobody() -> None:
    """The gap this field closes: a plain message on Telegram, and the channel's agent answers."""
    assert _who(
        "sre", "db", channel="telegram", channels={"telegram": {"agent": "sre"}}, text="uptime?"
    ) == "sre"


def test_a_channel_with_no_binding_is_the_default_agent() -> None:
    """Which is every channel in every deployment until an operator sets one."""
    assert _who("sre", channel="telegram", channels={"telegram": {"enabled": True}}) is None


def test_the_binding_is_read_per_channel() -> None:
    """Telegram's agent does not answer for Discord."""
    channels = {"telegram": {"agent": "sre"}, "discord": {"agent": "db"}}
    assert _who("sre", "db", channel="discord", channels=channels) == "db"
    assert _who("sre", "db", channel="telegram", channels=channels) == "sre"


def test_a_channel_the_config_never_mentions_is_the_default_agent() -> None:
    assert _who("sre", channel="matrix", channels={"telegram": {"agent": "sre"}}) is None


# --- the ordering -------------------------------------------------------------------------------


def test_a_mention_wins_over_the_channel_binding() -> None:
    """Deliberate, and the reason is #269.

    Before the mention routed anything, "a person typed the name of an agent and the deployment
    default answered, which is what made a narrowed default look broken rather than narrowed". A
    channel binding that swallowed the mention would rebuild that bug one layer up.
    """
    assert _who(
        "sre", "db",
        channel="telegram",
        channels={"telegram": {"agent": "sre"}},
        text="can @agent:db check the replica lag?",
    ) == "db"


def test_the_composer_choice_wins_over_the_channel_binding() -> None:
    assert _who(
        "sre", "db",
        channel="telegram",
        channels={"telegram": {"agent": "sre"}},
        metadata={"agent": "db"},
    ) == "db"


def test_a_mention_this_deployment_cannot_resolve_falls_to_the_binding() -> None:
    """The mention is a *request*. An unresolvable one leaves the next source to answer."""
    assert _who(
        "sre",
        channel="telegram",
        channels={"telegram": {"agent": "sre"}},
        text="@agent:ghost restart it",
    ) == "sre"


def test_an_invented_metadata_name_does_not_fall_through_to_the_binding() -> None:
    """Not a fallthrough, and this is the asymmetry worth pinning.

    A non-empty ``metadata["agent"]`` answers wherever it lands, so a name absent from the roster
    returns the deployment default rather than trying the mention or the binding below it. A
    client that names authority into existence gets the agent that grants least, not the
    next-most-specific one.
    """
    assert _who(
        "sre",
        channel="telegram",
        channels={"telegram": {"agent": "sre"}},
        text="@agent:sre uptime?",
        metadata={"agent": "ghost"},
    ) is None


# --- the roster is authority --------------------------------------------------------------------


def test_a_binding_naming_an_agent_this_loop_does_not_have_is_ignored() -> None:
    """Config refuses this at load. The resolver checks anyway, because the roster is authority
    and a loop holding a config it did not validate must not act on a name it cannot resolve.
    """
    assert _who("sre", channel="telegram", channels={"telegram": {"agent": "ghost"}}) is None


def test_a_blank_binding_is_no_binding() -> None:
    """How the WebUI clears the field, and how a hand-edited config says the same thing."""
    assert _who("sre", channel="telegram", channels={"telegram": {"agent": "   "}}) is None


# --- websocket ----------------------------------------------------------------------------------


def test_websocket_ignores_a_binding_even_when_one_is_present() -> None:
    """Config refuses ``channels.websocket.agent``, and the resolver refuses it a second time.

    Config is where an operator is told why. This is what keeps the invariant true for a
    ``ChannelsConfig`` built directly, by a test or an SDK caller, which never passed the root
    validator. The WebUI omits ``metadata["agent"]`` when its picker sits on *Default agent*, so a
    binding honoured here would answer as that agent for every turn where the operator chose
    nothing -- the opposite of "the WebUI takes what the user sends".
    """
    assert _who("sre", channel="websocket", channels={"websocket": {"agent": "sre"}}) is None


def test_the_websocket_composer_still_chooses_per_message() -> None:
    """What the WebUI does instead: source 1, per turn, unaffected by the refusal above."""
    assert _who(
        "sre", "db",
        channel="websocket",
        channels={"websocket": {"agent": "sre"}},
        metadata={"agent": "db"},
    ) == "db"


# --- no channel ---------------------------------------------------------------------------------


def test_a_turn_with_no_channel_reads_only_the_earlier_sources() -> None:
    """A subagent turn and an SDK call have no channel, and must not raise for want of one."""
    assert _who("sre", channels={"telegram": {"agent": "sre"}}) is None
    assert _who("sre", text="@agent:sre go", channels={"telegram": {"agent": "sre"}}) == "sre"
