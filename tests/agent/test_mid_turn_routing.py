"""A message that arrives mid-turn becomes its own turn (#209).

Observed on the demo: three corrections arrived in the first 47 seconds of a turn, the transcript
holds **three `user` records and one `turn_end`**, and the agent worked for seven more minutes after
being told nobody had mentioned Kubernetes.

The queue was never the problem — `_pending_queues` exists, drains with a budget, and carries a
`had_injections` flag all the way to the turn result. What was missing was a *choice*: every
non-command message was folded into the running turn, and there was no way to say otherwise.

The decision recorded on the issue is the request→response shape of the API itself. A message folded
into somebody else's turn has no response of its own, and its `turn_id` belongs to whoever started
that turn — which is the attribution `gates.identityIndependence` depends on.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanoinfra.agent.loop import mid_turn_route
from nanoinfra.config.schema import AgentDefaults


class _Commands:
    def __init__(self, dispatchable: bool = False) -> None:
        self.dispatchable = dispatchable

    def is_dispatchable_command(self, text: str) -> bool:
        return self.dispatchable


# --- the config ---------------------------------------------------------------------------


def test_the_default_is_a_turn_of_its_own() -> None:
    assert AgentDefaults().mid_turn_messages == "queue"


def test_the_old_behaviour_is_still_expressible() -> None:
    """A deployment that wants a correction to reach the running turn can say so."""
    assert AgentDefaults(midTurnMessages="inject").mid_turn_messages == "inject"


def test_an_unknown_mode_is_refused_rather_than_read_as_a_default() -> None:
    """Silently reading a typo as `queue` would change behaviour without saying so."""
    with pytest.raises(ValidationError):
        AgentDefaults(midTurnMessages="fold")


# --- the routing --------------------------------------------------------------------------


def test_a_message_with_no_turn_running_is_dispatched() -> None:
    assert mid_turn_route("hello", turn_active=False, mode="queue", commands=_Commands()) == (
        "dispatch"
    )


def test_a_message_arriving_mid_turn_becomes_its_own_turn() -> None:
    """`_dispatch` takes the per-session lock, so a second task waits rather than competing."""
    assert mid_turn_route("correction", turn_active=True, mode="queue", commands=_Commands()) == (
        "dispatch"
    )


def test_inject_mode_still_folds_into_the_running_turn() -> None:
    assert mid_turn_route("correction", turn_active=True, mode="inject", commands=_Commands()) == (
        "inject"
    )


def test_a_dispatchable_command_answers_now_in_either_mode() -> None:
    """`/status` and `/model` must not wait behind a seven-minute turn: a command that answers only
    after the work it was asking about is a command nobody can use."""
    for mode in ("queue", "inject"):
        route = mid_turn_route(
            "/status", turn_active=True, mode=mode, commands=_Commands(dispatchable=True)
        )
        assert route == "command", mode


def test_a_command_with_no_turn_running_takes_the_normal_path() -> None:
    """Dispatching inline is the mid-turn accommodation; with nothing running it is unnecessary."""
    assert mid_turn_route(
        "/status", turn_active=False, mode="queue", commands=_Commands(dispatchable=True)
    ) == "dispatch"


def test_an_unknown_mode_at_runtime_keeps_the_configured_default() -> None:
    """The schema refuses a bad value, so this only fires if one reaches the loop another way --
    and a message routed nowhere would be a message silently dropped."""
    assert mid_turn_route("hi", turn_active=True, mode="nonsense", commands=_Commands()) == (
        "dispatch"
    )
