"""A reloaded thread shows the same per-step cost the live one did (#208).

The frame and the persisted record are the same body, so the replay reads the same keys the client
read. What it has to reproduce is the *anchor* rule, because that is the part a reader can get
wrong: one call's cost belongs to the row that call produced, and a call that streamed only text
has no trace row of its own.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.webui.transcript import replay_transcript_to_ui_messages


def _usage(prompt: int = 21_000, cached: int | None = 20_160) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": 1_500,
        "total_tokens": prompt + 1_500,
        "request_count": 1,
        "estimated_tokens": 0,
    }
    if cached is not None:
        usage["cached_tokens"] = cached
    return usage


def _trace(text: str, turn: str = "t1") -> dict[str, Any]:
    return {
        "event": "message",
        "chat_id": "c",
        "kind": "tool_hint",
        "text": text,
        "turn_id": turn,
        "turn_phase": "activity",
    }


def _stream_end(**extra: Any) -> dict[str, Any]:
    return {
        "event": "stream_end",
        "chat_id": "c",
        "stream_id": "s",
        "turn_id": "t1",
        "resuming": True,
        **extra,
    }


def _replay(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return replay_transcript_to_ui_messages(records)


def _with_step_usage(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("stepUsage") or m.get("stepModelMs")]


def test_a_call_cost_lands_on_the_row_that_call_produced() -> None:
    messages = _replay([
        {"event": "user", "chat_id": "c", "text": "hola", "turn_id": "t1"},
        _trace("Running exec"),
        _stream_end(usage=_usage(), duration_ms=47_300),
    ])

    stamped = _with_step_usage(messages)
    assert len(stamped) == 1
    assert stamped[0]["kind"] == "trace"
    assert stamped[0]["stepUsage"]["prompt_tokens"] == 21_000
    assert stamped[0]["stepModelMs"] == 47_300


def test_a_row_that_anchors_two_calls_reports_both() -> None:
    """Consecutive tool hints replay as a single row, so one row can be the anchor for two calls.

    Summing is what keeps a reloaded cluster equal to the live one it has to match: live, the two
    rows exist separately and the cluster adds them, and dropping the second here would make the
    same turn look cheaper after a refresh.
    """
    messages = _replay([
        {"event": "user", "chat_id": "c", "text": "hola", "turn_id": "t1"},
        _trace("Running exec"),
        _stream_end(usage=_usage(prompt=21_000), duration_ms=4_400),
        _trace("Reading a file"),
        _stream_end(usage=_usage(prompt=34_000), duration_ms=71_500),
    ])

    stamped = _with_step_usage(messages)
    assert [m["stepUsage"]["prompt_tokens"] for m in stamped] == [55_000]
    assert [m["stepModelMs"] for m in stamped] == [75_900]


def test_a_merged_row_drops_a_cache_figure_only_one_call_reported() -> None:
    """Mixing a known cache read with an unknown one would print a share for input nobody
    measured -- the same reason an absent `cached_tokens` is not a zero."""
    messages = _replay([
        {"event": "user", "chat_id": "c", "text": "hola", "turn_id": "t1"},
        _trace("Running exec"),
        _stream_end(usage=_usage(prompt=21_000, cached=20_160)),
        _stream_end(usage=_usage(prompt=34_000, cached=None)),
    ])

    stamped = _with_step_usage(messages)
    assert stamped[0]["stepUsage"]["prompt_tokens"] == 55_000
    assert "cached_tokens" not in stamped[0]["stepUsage"]


def test_a_record_with_no_usage_stamps_nothing() -> None:
    messages = _replay([
        {"event": "user", "chat_id": "c", "text": "hola", "turn_id": "t1"},
        _trace("Running exec"),
        _stream_end(),
    ])

    assert _with_step_usage(messages) == []


def test_an_unreported_cache_metric_stays_absent_through_the_replay() -> None:
    """`cached_tokens` was NULL on 3 of the 23 calls. Absent is not zero, at any layer."""
    messages = _replay([
        {"event": "user", "chat_id": "c", "text": "hola", "turn_id": "t1"},
        _trace("Running exec"),
        _stream_end(usage=_usage(cached=None)),
    ])

    stamped = _with_step_usage(messages)
    assert "cached_tokens" not in stamped[0]["stepUsage"]


def test_a_turn_without_traces_stamps_its_answer() -> None:
    """A one-call turn that answered in text has no trace row, and its cost still has a home."""
    messages = _replay([
        {"event": "user", "chat_id": "c", "text": "hola", "turn_id": "t1"},
        {
            "event": "stream_end",
            "chat_id": "c",
            "stream_id": "s",
            "turn_id": "t1",
            "text": "hola!",
            "usage": _usage(),
            "duration_ms": 1_200,
        },
    ])

    stamped = _with_step_usage(messages)
    assert len(stamped) == 1
    assert stamped[0]["role"] == "assistant"
    assert stamped[0].get("kind") != "trace"
    assert stamped[0]["stepModelMs"] == 1_200
