"""Whether the gate is working, rather than merely running (#274).

The four numbers #235 asked for: how many actions were held for a person, how many they answered,
how many they refused, and how long they took. The last one is the point — an approval queue with a
median of four hours is a queue nobody reads, and an approver who refuses nothing is either
working in a deployment that gates only safe things or rubber-stamping. Neither is visible from
the audit viewer's page of rows.

**Three things about the log are not what they look like**, and each was measured against a
deployment with 423 records rather than reasoned about:

* ``approve`` is the **ask**, not the answer. Every one of its reasons reads "an operator must
  approve this action on a path other than X", and ``actor`` is ``None`` on all of them because
  nobody has answered yet. It is the record :mod:`nanoinfra.gates.audit` means by "the record that
  suspends an action before an answer exists".
* the answer is a later ``allow`` carrying an ``approval_path`` -- the path a person answered on.
* ``denied`` holds two different events. With ``actor`` and ``approval_path`` a person refused;
  without them the gate refused on policy and nobody was ever asked. On that deployment the split
  was 1 and 16, so charting all of them as denials would claim an approver rejected sixteen
  actions they never saw.

Pairing an ask to its answer needs no new field: ``session_id`` and ``command_digest`` are on
both, and the answer is the first matching record after the ask. Verified at 42 of 43 pairs, and
the one that does not pair is the most interesting row on the page -- an ask with no answer and no
expiry is an approval that fell through.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from loguru import logger

#: The gate suspended the action and wants a person. Not an answer.
DECISION_ASK = "approve"
#: A person answered yes. The record is an `allow` that names the path they answered on.
DECISION_ALLOW = "allow"
#: Either a person refused, or policy did. `_answered_by_a_person` tells them apart.
DECISION_REFUSED = "denied"
#: #38's deadline passed with nobody answering.
DECISION_EXPIRED = "expired"


def _when(record: Mapping[str, Any]) -> datetime | None:
    raw = record.get("ts")
    if not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _answered_by_a_person(record: Mapping[str, Any]) -> bool:
    """Whether a human answered this record, rather than policy deciding it alone.

    Both halves, and not either: ``actor`` names who, ``approval_path`` names where. A record
    carrying one and not the other is a record this function refuses to call an answer, because
    the two together are what #79 and #13 make checkable.
    """
    return bool(record.get("actor")) and bool(record.get("approval_path"))


def _action_key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    """What identifies one action across its ask and its answer.

    `session_id` alone is not enough -- one session can hold two suspended actions -- and
    `command_digest` alone is not either, since the same command in two sessions is two actions.
    """
    session = record.get("session_id")
    digest = record.get("command_digest")
    if not isinstance(session, str) or not isinstance(digest, str):
        return None
    return (session, digest)


def approvals_payload(
    records: Iterable[Mapping[str, Any]],
    *,
    window_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The four numbers, plus the two rates that make them answerable.

    Attributed to the window an ask *landed* in, not the window it was answered in: an approval
    raised on the 30th and answered on the 31st belongs to the 30th, because the question the page
    asks is "what did this deployment hold for a person" and that happened then. The payload says
    so, so a reader is not left to guess which end anchors the count.
    """
    horizon = (now or datetime.now(tz=timezone.utc)) - timedelta(days=max(1, window_days))

    ordered = sorted(
        (record for record in records if _when(record) is not None),
        key=lambda record: _when(record) or horizon,
    )

    asks = [
        record
        for record in ordered
        if record.get("decision") == DECISION_ASK and (_when(record) or horizon) >= horizon
    ]

    answered = 0
    refused_by_a_person = 0
    expired = 0
    unanswered = 0
    waits: list[float] = []

    for ask in asks:
        key = _action_key(ask)
        start = _when(ask)
        if key is None or start is None:
            # An ask with no digest cannot be paired with anything. Counted as held and not as
            # answered, which is the truthful side to fall on.
            unanswered += 1
            continue
        answer = _first_answer_after(ordered, key=key, after=start)
        if answer is None:
            unanswered += 1
            continue
        decision = answer.get("decision")
        if decision == DECISION_EXPIRED:
            expired += 1
            continue
        landed = _when(answer)
        if landed is not None:
            waits.append((landed - start).total_seconds())
        if decision == DECISION_REFUSED:
            refused_by_a_person += 1
        else:
            answered += 1

    # Policy refusals are counted over the window and kept apart on purpose: they are not the
    # approver's work, and adding them to the denial count overstates it by an order of magnitude.
    policy_refusals = sum(
        1
        for record in ordered
        if record.get("decision") == DECISION_REFUSED
        and not _answered_by_a_person(record)
        and (_when(record) or horizon) >= horizon
    )

    same_path = sum(
        1
        for record in ordered
        if record.get("same_path")
        and _answered_by_a_person(record)
        and (_when(record) or horizon) >= horizon
    )

    total_answers = answered + refused_by_a_person
    return {
        "window_days": max(1, window_days),
        "asked": len(asks),
        "answered": answered,
        "refused": refused_by_a_person,
        "expired": expired,
        # An ask with no answer and no expiry. The row worth opening: nothing ran, and nothing
        # said why.
        "unanswered": unanswered,
        "policy_refusals": policy_refusals,
        # `None` rather than 0.0: no answers means the question has no answer, and a zero share
        # would read as "this approver refuses nothing".
        "refusal_share": (
            round(refused_by_a_person / total_answers, 4) if total_answers else None
        ),
        "same_path_answers": same_path,
        "median_seconds_to_answer": round(statistics.median(waits), 1) if waits else None,
        "fastest_seconds": round(min(waits), 1) if waits else None,
        "slowest_seconds": round(max(waits), 1) if waits else None,
        # What anchors every count above, stated rather than left to be inferred.
        "attributed_to": "ask",
    }


def _first_answer_after(
    ordered: Sequence[Mapping[str, Any]],
    *,
    key: tuple[str, str],
    after: datetime,
) -> Mapping[str, Any] | None:
    """The first record that answers this action, or `None` while it is still held.

    An `expired` record counts as an answer to the ask -- the deadline answered it -- and is
    reported separately, because an action that expired is one nobody saw rather than one somebody
    refused.
    """
    for record in ordered:
        landed = _when(record)
        if landed is None or landed <= after:
            continue
        if _action_key(record) != key:
            continue
        decision = record.get("decision")
        if decision == DECISION_EXPIRED:
            return record
        if decision in (DECISION_ALLOW, DECISION_REFUSED) and _answered_by_a_person(record):
            return record
    return None


def approvals_metrics(store: Any, *, window_days: int = 30) -> dict[str, Any]:
    """Read the log and compute the payload. A missing or unreadable log is an empty window.

    The same rule the audit viewer follows: a fresh install has no segments, and a reader must not
    render that as an error.
    """
    records: list[Mapping[str, Any]] = []
    try:
        records = cast("list[Mapping[str, Any]]", store.read_all())
    except OSError as exc:
        logger.warning("approval metrics could not read the log: {}", exc)
    return approvals_payload(records, window_days=window_days)


__all__ = ["approvals_metrics", "approvals_payload"]
