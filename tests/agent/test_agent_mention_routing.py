"""`@agent:<name>` in a message routes the turn to that agent (#269).

The token was offered by the composer, completed for the operator, and then ignored. A deployment
typed `@agent:sre What's my uptime on @server:barrahome`, the **default** agent answered, and
because that default had been narrowed to no tool groups and no delegates it could neither run the
command nor ask a peer -- so it shelled out to `/usr/bin/ssh` by hand, failed for want of a key,
and spent three turns inspecting `id` and `~/.ssh`. The report that came back was "asyncssh is
broken in Docker". It was installed and fine.

Three things were true at once and each on its own made the token dead: nothing parsed the text,
no template told the model what the token meant, and the only way to honour it -- delegation --
needed a peer the answering agent usually does not hold. This module covers the first, which is
the one that makes a person's expectation come true: you name an agent, that agent answers.

Read here rather than in the composer on purpose. The token is *text*, and text arrives on every
transport, so a chat channel gets the same behaviour without a second implementation.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.config.schema import NamedAgentConfig


class _Roster:
    """Just enough of the loop to ask it who answers. The resolver reads only the roster."""

    def __init__(self, *names: str) -> None:
        self.named_agents: dict[str, NamedAgentConfig] = {
            name: NamedAgentConfig(description=name) for name in names
        }

    _acting_agent_for = AgentLoop._acting_agent_for
    _agent_from_mention = AgentLoop._agent_from_mention


def _who(text: str | None, *names: str, metadata: dict[str, Any] | None = None) -> str | None:
    return _Roster(*names)._acting_agent_for(metadata, text)


# --- the text ---------------------------------------------------------------------------------


def test_a_mention_answers_as_that_agent() -> None:
    assert _who("@agent:sre What's my uptime on @server:barrahome", "sre", "db") == "sre"


def test_a_mention_anywhere_in_the_sentence_counts() -> None:
    """People write the address where it reads naturally, not only in front."""
    assert _who("can @agent:db check the replica lag?", "sre", "db") == "db"


def test_a_name_this_deployment_does_not_have_is_not_a_mention() -> None:
    """The roster is the authority. An invented name gets the default, which grants nothing."""
    assert _who("@agent:prod restart it", "sre", "db") is None


def test_the_first_name_that_resolves_wins() -> None:
    """One question, one agent. Left to right is the order a person reads their own sentence."""
    assert _who("@agent:db and @agent:sre, who owns this?", "sre", "db") == "db"


def test_trailing_punctuation_is_settled_by_the_roster() -> None:
    """`.` is legal in a name, so stripping dots blindly would be guessing."""
    assert _who("@agent:sre, look at this", "sre") == "sre"
    assert _who("ask @agent:sre.", "sre") == "sre"
    assert _who("ask @agent:net.core about it", "net.core", "sre") == "net.core"


def test_a_longer_name_is_never_truncated_into_a_shorter_one() -> None:
    """The hazard of stripping characters one at a time, which is why it strips a fixed set."""
    assert _who("@agent:dbx has it", "db") is None


def test_no_mention_is_the_default_agent() -> None:
    assert _who("what's my uptime?", "sre", "db") is None
    assert _who(None, "sre") is None
    assert _who("", "sre") is None


def test_an_email_address_is_not_a_mention() -> None:
    """`@` is common in text. The prefix has to be `@agent:` and nothing looser."""
    assert _who("mail sre@example.com about it", "sre") is None


# --- the picker still wins --------------------------------------------------------------------


def test_the_clients_own_choice_beats_the_text() -> None:
    """The composer's picker and an automation's binding are explicit. The text is a fallback."""
    assert _who("@agent:sre do it", "sre", "db", metadata={"agent": "db"}) == "db"


def test_an_invented_choice_does_not_fall_through_to_the_text() -> None:
    """A client that names an agent has made a claim, and a wrong claim is not an invitation to
    read the sentence instead: it gets the default, exactly as it did before mentions routed."""
    assert _who("@agent:sre do it", "sre", metadata={"agent": "nope"}) is None
