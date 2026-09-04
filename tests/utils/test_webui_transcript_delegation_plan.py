"""A reloaded thread shows the plan the live turn showed (#252).

The frame and the persisted record are the same body, so the replay reads the same keys the client
read -- the rule `transcript.py` already follows for the turn's usage (#202), its per-step usage
(#208) and its prompt manifest (#203).

What a plan needs back is narrower than a whole turn and stricter in one way: it is *per call*. A
manager that asks two peers made two calls, and the two rows the thread shows come from the two
tool events behind them. So these tests pin three things about the replayed record:

- the delegations survive as separate events, keyed by call id, even when their trace lines are
  identical;
- what became of each one survives -- a peer that failed replays as `error`, never as `end`;
- a delegation's *own* usage survives, and the manager's per-step usage stays on the row where
  #208 put it rather than migrating onto a peer's event.

The rendering itself is asserted on the client, over the exact shape this module produces
(`webui/src/tests/delegation-plan.test.tsx`).
"""

from __future__ import annotations

from typing import Any

from nanoinfra.webui.transcript import replay_transcript_to_ui_messages


def _usage(prompt: int, completion: int, cached: int | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "request_count": 1,
        "estimated_tokens": 0,
    }
    if cached is not None:
        usage["cached_tokens"] = cached
    return usage


def _delegate_event(
    call_id: str,
    agent: str,
    task: str,
    phase: str,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "version": 1,
        "phase": phase,
        "call_id": call_id,
        "name": "delegate_to_agent",
        "arguments": {"agent": agent, "task": task},
        "result": "answered" if phase == "end" else None,
        "error": None,
        "files": [],
        "embeds": [],
        **extra,
    }


def _hint(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event": "message",
        "chat_id": "c",
        "kind": "tool_hint",
        "text": "",
        "tool_events": events,
        "turn_id": "t1",
        "turn_phase": "activity",
    }


def _turn(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"event": "user", "chat_id": "c", "text": "check server X", "turn_id": "t1"},
        *records,
        {
            "event": "message",
            "chat_id": "c",
            "text": "here is what both peers found",
            "turn_id": "t1",
            "turn_phase": "answer",
        },
        {
            "event": "turn_end",
            "chat_id": "c",
            "turn_id": "t1",
            "latency_ms": 4_200,
            "usage": _usage(1_200, 90),
            "agent": "manager",
        },
    ]


def _delegation_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        event
        for message in messages
        for event in (message.get("toolEvents") or [])
        if event.get("name") == "delegate_to_agent"
    ]


def test_a_turn_with_no_delegation_replays_with_no_delegation_in_it() -> None:
    """Every turn today. The plan is an object a turn either has or does not."""
    messages = replay_transcript_to_ui_messages(_turn([
        _hint([
            {
                "version": 1,
                "phase": "end",
                "call_id": "c1",
                "name": "grep",
                "arguments": {"pattern": "disk"},
                "result": "3 matches",
                "error": None,
            }
        ]),
    ]))

    assert _delegation_events(messages) == []
    trace = [m for m in messages if m.get("kind") == "trace"]
    assert len(trace) == 1
    assert trace[0]["traces"] == ['grep({"pattern": "disk"})']


def test_a_two_delegation_plan_replays_as_two_delegations() -> None:
    messages = replay_transcript_to_ui_messages(_turn([
        _hint([
            _delegate_event("c1", "sre-copilot", "check disk", "start"),
            _delegate_event("c2", "db-expert", "check slow queries", "start"),
        ]),
        _hint([
            _delegate_event("c1", "sre-copilot", "check disk", "end"),
            _delegate_event("c2", "db-expert", "check slow queries", "end"),
        ]),
    ]))

    events = _delegation_events(messages)
    assert [event["call_id"] for event in events] == ["c1", "c2"]
    assert [event["arguments"]["agent"] for event in events] == ["sre-copilot", "db-expert"]
    assert {event["phase"] for event in events} == {"end"}


def test_two_delegations_to_the_same_peer_stay_two_delegations() -> None:
    """Their trace lines are indistinguishable; the calls are two, and the call ids say so.

    A plan keyed on the line would report one delegation where two ran -- the line text is the
    same and both paths dedupe lines by it once a row is merged into. The record keeps the events
    apart by call id, which is what the client keys its rows on.
    """
    messages = replay_transcript_to_ui_messages(_turn([
        _hint([
            _delegate_event("c1", "sre-copilot", "check disk", "start"),
            _delegate_event("c2", "sre-copilot", "check disk", "start"),
        ]),
    ]))

    trace = [m for m in messages if m.get("kind") == "trace"][0]
    line = 'delegate_to_agent({"agent": "sre-copilot", "task": "check disk"})'
    assert set(trace["traces"]) == {line}
    assert [event["call_id"] for event in _delegation_events(messages)] == ["c1", "c2"]


def test_a_delegation_that_failed_never_replays_as_one_that_finished() -> None:
    messages = replay_transcript_to_ui_messages(_turn([
        _hint([
            _delegate_event("c1", "sre-copilot", "check disk", "start"),
            _delegate_event("c2", "db-expert", "check slow queries", "start"),
        ]),
        _hint([
            _delegate_event("c1", "sre-copilot", "check disk", "end"),
            _delegate_event(
                "c2",
                "db-expert",
                "check slow queries",
                "error",
                error="Error: the peer could not reach the database",
            ),
        ]),
    ]))

    by_call = {event["call_id"]: event for event in _delegation_events(messages)}
    assert by_call["c1"]["phase"] == "end"
    assert by_call["c2"]["phase"] == "error"
    assert by_call["c2"]["error"] == "Error: the peer could not reach the database"


def test_a_delegations_own_cost_survives_the_reload() -> None:
    """A delegated turn is its own turn with its own usage, so the figure travels on the call.

    Kept whole rather than filtered to the keys this module knows: a reloaded plan has to come to
    the same total as the live one, and a dropped `usage` would make it cheaper after a refresh.
    """
    messages = replay_transcript_to_ui_messages(_turn([
        _hint([_delegate_event("c1", "sre-copilot", "check disk", "start")]),
        _hint([
            _delegate_event(
                "c1",
                "sre-copilot",
                "check disk",
                "end",
                usage=_usage(9_000, 400, 8_000),
            )
        ]),
    ]))

    events = _delegation_events(messages)
    assert len(events) == 1
    assert events[0]["usage"] == _usage(9_000, 400, 8_000)


def test_the_managers_own_step_cost_stays_off_its_peers_events() -> None:
    """The manager's provider call and the peer's turn are two costs, and stay two.

    `stream_end` carries what the manager's own call cost and #208 anchors it on the activity row.
    A peer's cost lives on the delegation. Merging either into the other is how one turn's tokens
    would get printed twice.
    """
    messages = replay_transcript_to_ui_messages(_turn([
        _hint([_delegate_event("c1", "sre-copilot", "check disk", "start")]),
        {
            "event": "stream_end",
            "chat_id": "c",
            "stream_id": "s",
            "turn_id": "t1",
            "resuming": True,
            "usage": _usage(21_000, 1_500, 20_160),
            "duration_ms": 47_300,
        },
    ]))

    trace = [m for m in messages if m.get("kind") == "trace"][0]
    assert trace["stepUsage"] == _usage(21_000, 1_500, 20_160)
    assert trace["stepModelMs"] == 47_300
    assert "usage" not in _delegation_events(messages)[0]
