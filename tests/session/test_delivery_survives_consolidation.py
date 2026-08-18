"""A proactive delivery must survive trimming, so the user's follow-up still has a referent.

Upstream HKUDS/nanobot#4307 reports post-turn consolidation wiping the agent's own delivery
message: the user says "make it shorter" and the thing to shorten is gone from the replayed
history. Measured on this tree, it does not happen -- the retention path anchors on the preceding
`_channel_delivery` in all three of its branches. This pins that, because the bug is invisible
until someone reads a reply that answers nothing. See nanoinfraorg/nanoinfra#147.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.session.manager import MIN_COMPACTED_REPLAY_MESSAGES, SessionManager


@pytest.fixture
def manager(tmp_path: Path) -> SessionManager:
    return SessionManager(tmp_path / "workspace", sessions_root=tmp_path / "sessions")


def _contents(history: list[dict[str, object]]) -> list[str]:
    return [str(message.get("content", "")) for message in history]


def _filled_session(manager: SessionManager, turns: int = 30):
    session = manager.get_or_create("chat:1")
    for i in range(turns):
        session.add_message("user", f"filler {i}")
        session.add_message("assistant", f"reply {i}")
    return session


def test_a_delivery_and_its_follow_up_both_survive(manager: SessionManager) -> None:
    session = _filled_session(manager)
    session.add_message("assistant", "HERE-IS-THE-DRAFT", _channel_delivery=True)
    session.add_message("user", "make it shorter")

    history = session.get_history(
        max_messages=MIN_COMPACTED_REPLAY_MESSAGES, extend_to_user=True
    )
    contents = _contents(history)

    assert "HERE-IS-THE-DRAFT" in contents, "the follow-up would have no referent"
    assert "make it shorter" in contents


def test_the_delivery_stays_immediately_before_its_follow_up(manager: SessionManager) -> None:
    """Order matters as much as presence: the model reads them as a pair."""
    session = _filled_session(manager)
    session.add_message("assistant", "HERE-IS-THE-DRAFT", _channel_delivery=True)
    session.add_message("user", "make it shorter")

    contents = _contents(
        session.get_history(max_messages=MIN_COMPACTED_REPLAY_MESSAGES, extend_to_user=True)
    )

    assert contents[-2:] == ["HERE-IS-THE-DRAFT", "make it shorter"]


def test_it_survives_a_hard_cap_with_no_user_extension(manager: SessionManager) -> None:
    """The other retention branch: a capped window with extend_to_user off."""
    session = _filled_session(manager)
    session.add_message("assistant", "HERE-IS-THE-DRAFT", _channel_delivery=True)
    session.add_message("user", "make it shorter")

    contents = _contents(session.get_history(max_messages=3, extend_to_user=False))

    assert "make it shorter" in contents


def test_a_delivery_with_no_follow_up_is_not_specially_retained(
    manager: SessionManager,
) -> None:
    """The anchor exists to keep a referent for a reply, not to pin deliveries forever."""
    session = _filled_session(manager)
    session.add_message("assistant", "OLD-DELIVERY", _channel_delivery=True)
    for i in range(30, 60):
        session.add_message("user", f"filler {i}")
        session.add_message("assistant", f"reply {i}")

    contents = _contents(
        session.get_history(max_messages=MIN_COMPACTED_REPLAY_MESSAGES, extend_to_user=True)
    )

    assert "OLD-DELIVERY" not in contents


def test_consolidation_progress_does_not_hide_the_delivery(manager: SessionManager) -> None:
    """Archived messages advance last_consolidated; the visible tail must still pair up."""
    session = _filled_session(manager)
    session.add_message("assistant", "HERE-IS-THE-DRAFT", _channel_delivery=True)
    session.add_message("user", "make it shorter")
    # What compact_idle_session does after archiving.
    session.last_consolidated = len(session.messages) - 2

    contents = _contents(
        session.get_history(max_messages=MIN_COMPACTED_REPLAY_MESSAGES, extend_to_user=True)
    )

    assert "HERE-IS-THE-DRAFT" in contents
    assert "make it shorter" in contents
