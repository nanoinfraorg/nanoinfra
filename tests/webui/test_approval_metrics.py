"""Whether the gate is working, rather than merely running (#274).

The definitions here are not the decision names they look like, and each was measured against a
live log of 423 records rather than inferred from the vocabulary:

* `approve` is the **ask** -- the gate suspended the action and wants a person. `actor` is `None`
  on every one of them because nobody has answered yet.
* the answer is a later `allow` carrying an `approval_path`.
* `denied` holds two different events, and only one is an approver's work. On that deployment the
  split was 1 human denial against 16 policy refusals, so a panel that merged them would claim an
  approver rejected sixteen actions nobody ever showed them.

Every test below states the record shape it is about, because getting these definitions wrong is
the failure mode -- not the arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from nanoinfra.webui.approval_metrics import approvals_payload

_NOW = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)


def _at(seconds: float) -> str:
    return (_NOW - timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _ask(*, session: str = "s1", digest: str = "d1", seconds: float = 100.0) -> dict[str, Any]:
    """What the gate writes when it suspends an action. No actor: nobody has answered."""
    return {
        "ts": _at(seconds),
        "decision": "approve",
        "session_id": session,
        "command_digest": digest,
        "reason": "an operator must approve this action on a path other than 'websocket'.",
        "actor": None,
        "approval_path": None,
    }


def _answer_yes(
    *, session: str = "s1", digest: str = "d1", seconds: float = 90.0, same_path: bool = False
) -> dict[str, Any]:
    """A person said yes. The record is an `allow` naming who and on which path."""
    return {
        "ts": _at(seconds),
        "decision": "allow",
        "session_id": session,
        "command_digest": digest,
        "actor": "webui:alberto",
        "approval_path": "telegram",
        "same_path": same_path,
    }


def _answer_no(
    *, session: str = "s1", digest: str = "d1", seconds: float = 90.0
) -> dict[str, Any]:
    """A person said no."""
    return {
        "ts": _at(seconds),
        "decision": "denied",
        "session_id": session,
        "command_digest": digest,
        "actor": "webui:alberto",
        "approval_path": "telegram",
    }


def _policy_refusal(*, seconds: float = 95.0) -> dict[str, Any]:
    """The gate refused on policy. Nobody was asked, so nobody answered."""
    return {
        "ts": _at(seconds),
        "decision": "denied",
        "session_id": "s9",
        "command_digest": "d9",
        "reason": "gates.approvers lists nobody on a second authenticated path",
        "actor": None,
        "approval_path": None,
    }


def _expired(*, session: str = "s1", digest: str = "d1", seconds: float = 60.0) -> dict[str, Any]:
    return {
        "ts": _at(seconds),
        "decision": "expired",
        "session_id": session,
        "command_digest": digest,
        "reason": "no operator answered before the deadline, so the action expired.",
    }


# --- the four numbers -------------------------------------------------------------------


def test_an_ask_answered_yes_counts_as_answered_and_not_as_an_allow() -> None:
    payload = approvals_payload([_ask(), _answer_yes()], now=_NOW)

    assert payload["asked"] == 1
    assert payload["answered"] == 1
    assert payload["refused"] == 0
    assert payload["expired"] == 0
    assert payload["unanswered"] == 0


def test_a_human_denial_and_a_policy_refusal_are_counted_apart() -> None:
    """The split that would otherwise overstate an approver's denials by 16x."""
    payload = approvals_payload(
        [_ask(), _answer_no(), _policy_refusal(), _policy_refusal(seconds=94.0)], now=_NOW
    )

    assert payload["refused"] == 1
    assert payload["policy_refusals"] == 2


def test_an_expired_ask_is_neither_answered_nor_refused() -> None:
    """Nobody saw it. Counting it as a denial would blame an approver for a deadline."""
    payload = approvals_payload([_ask(), _expired()], now=_NOW)

    assert payload["expired"] == 1
    assert payload["answered"] == 0
    assert payload["refused"] == 0
    assert payload["median_seconds_to_answer"] is None


def test_an_ask_with_no_answer_and_no_expiry_is_reported_as_such() -> None:
    """The most interesting row on the page: nothing ran and nothing said why."""
    payload = approvals_payload([_ask()], now=_NOW)

    assert payload["unanswered"] == 1
    assert payload["answered"] == 0
    assert payload["expired"] == 0


# --- the median -------------------------------------------------------------------------


def test_the_median_pairs_by_session_and_digest_and_needs_no_new_field() -> None:
    payload = approvals_payload(
        [_ask(seconds=100.0), _answer_yes(seconds=90.0)], now=_NOW
    )

    assert payload["median_seconds_to_answer"] == 10.0


def test_two_asks_in_one_session_do_not_pair_across_each_other() -> None:
    """`session_id` alone is not enough, which is why the digest is half the key."""
    payload = approvals_payload(
        [
            _ask(digest="fast", seconds=100.0),
            _answer_yes(digest="fast", seconds=98.0),
            _ask(digest="slow", seconds=90.0),
            _answer_yes(digest="slow", seconds=30.0),
        ],
        now=_NOW,
    )

    assert payload["asked"] == 2
    assert payload["answered"] == 2
    # 2s and 60s -> median 31s. A cross-paired reading would produce neither.
    assert payload["median_seconds_to_answer"] == 31.0


def test_the_median_over_an_odd_and_an_even_count() -> None:
    three = approvals_payload(
        [
            _ask(digest="a", seconds=100.0), _answer_yes(digest="a", seconds=95.0),
            _ask(digest="b", seconds=90.0), _answer_yes(digest="b", seconds=80.0),
            _ask(digest="c", seconds=70.0), _answer_yes(digest="c", seconds=50.0),
        ],
        now=_NOW,
    )
    assert three["median_seconds_to_answer"] == 10.0

    two = approvals_payload(
        [
            _ask(digest="a", seconds=100.0), _answer_yes(digest="a", seconds=95.0),
            _ask(digest="b", seconds=90.0), _answer_yes(digest="b", seconds=75.0),
        ],
        now=_NOW,
    )
    assert two["median_seconds_to_answer"] == 10.0


def test_no_answers_reads_as_unknown_rather_than_as_zero_seconds() -> None:
    payload = approvals_payload([], now=_NOW)

    assert payload["median_seconds_to_answer"] is None
    assert payload["fastest_seconds"] is None
    assert payload["refusal_share"] is None


def test_a_refusal_share_of_zero_is_a_real_answer_and_none_is_not() -> None:
    """An approver who refuses nothing is a fact; an approver who answered nothing is not."""
    answered = approvals_payload([_ask(), _answer_yes()], now=_NOW)
    assert answered["refusal_share"] == 0.0

    assert approvals_payload([_ask()], now=_NOW)["refusal_share"] is None


# --- the window -------------------------------------------------------------------------


def test_the_window_anchors_on_the_ask_and_the_payload_says_so() -> None:
    """An approval raised on the 30th and answered on the 31st belongs to the 30th."""
    payload = approvals_payload(
        [_ask(seconds=100.0), _answer_yes(seconds=90.0)], window_days=30, now=_NOW
    )

    assert payload["attributed_to"] == "ask"
    assert payload["window_days"] == 30


def test_an_ask_outside_the_window_is_not_counted() -> None:
    old = 40 * 24 * 60 * 60
    payload = approvals_payload(
        [_ask(digest="old", seconds=old), _ask(digest="new", seconds=100.0)],
        window_days=30,
        now=_NOW,
    )

    assert payload["asked"] == 1


# --- the same-path rate ------------------------------------------------------------------


def test_an_answer_on_the_channel_that_asked_is_counted() -> None:
    """#13's mark, as a rate: one compromised account holding both halves."""
    payload = approvals_payload(
        [_ask(), _answer_yes(same_path=True)], now=_NOW
    )

    assert payload["same_path_answers"] == 1


def test_a_record_that_names_a_path_but_no_person_is_not_an_answer() -> None:
    """Both halves or neither. `actor` names who and `approval_path` names where."""
    half = {**_answer_yes(), "actor": None}
    payload = approvals_payload([_ask(), half], now=_NOW)

    assert payload["answered"] == 0
    assert payload["unanswered"] == 1
