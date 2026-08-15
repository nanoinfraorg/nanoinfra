# tests/agent/test_execution_context_system_turns.py
"""Item 3 (#5), follow-up: a turn nobody typed must not read as attended.

`nanoinfra/agent/subagent.py:571` announces a finished subagent by publishing an
InboundMessage with channel "system" and sender_id "subagent". No person typed that turn,
and its content is model-authored. Attended trust there would let a subagent's own output
drive the parent session at interactive privilege, which is the untrusted-content path this
proposal exists to close.
"""

from __future__ import annotations

from nanoinfra.agent.automation_turns import execution_context_for_turn
from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    is_unattended_execution_context,
)

SYSTEM_CHANNEL = "system"


def test_a_subagent_result_turn_reads_unattended() -> None:
    """The announcement carries no automation metadata and no goal, so the classifier used
    to fall through to interactive."""
    resolved = execution_context_for_turn(
        {"origin_message_id": "abc"},
        None,
        channel=SYSTEM_CHANNEL,
    )

    assert is_unattended_execution_context(resolved)


def test_a_live_channel_turn_still_reads_interactive() -> None:
    resolved = execution_context_for_turn({}, None, channel="telegram")

    assert resolved == EXECUTION_CONTEXT_INTERACTIVE


def test_the_channel_argument_is_optional_and_fails_closed_when_omitted() -> None:
    """A caller that forgets the channel must not gain attended trust for a system turn.

    The default keeps existing callers working, so the classifier treats an omitted channel
    the same as a live channel only when nothing else marks the turn. Every internal caller
    passes the channel.
    """
    resolved = execution_context_for_turn({}, None)

    assert resolved == EXECUTION_CONTEXT_INTERACTIVE
