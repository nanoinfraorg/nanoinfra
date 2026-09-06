"""The columns the store recorded and nobody read (#235).

Six of `llm_calls`' twenty-one columns reached no reader before this: `duration_ms` was not even
aggregated, `stream` and the two error columns were never queried, and the two cache counters were
summed into a total nobody displayed. `finish_reason` was collapsed to a boolean that counted
`error` and `cancelled` and treated `length` -- a **truncated** answer -- as a success.

Each test below is one of those, because the point of the metrics work is that a stored column
with no reader is a column that does not exist.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanoinfra.llm_usage.models import LLMCallRecord
from nanoinfra.llm_usage.store import MAX_DAYS_RETAINED, LLMUsageStore
from nanoinfra.providers.base import LLMUsage


def _record(
    *,
    started_at_ms: int,
    provider: str = "moonshot",
    model: str = "kimi-k3",
    finish_reason: str = "stop",
    stream: bool = False,
    duration_ms: int = 900,
    usage: LLMUsage | None = None,
    error_kind: str | None = None,
    error_status_code: int | None = None,
    source: str = "user",
) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=started_at_ms,
        duration_ms=duration_ms,
        provider=provider,
        model=model,
        source=source,  # pyright: ignore[reportArgumentType]
        stream=stream,
        finish_reason=finish_reason,
        usage=usage,
        error_kind=error_kind,
        error_status_code=error_status_code,
    )


@pytest.fixture
def store(tmp_path: Path) -> LLMUsageStore:
    return LLMUsageStore(tmp_path / "llm-usage.sqlite3")


def _now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


# --- the columns that had no reader -----------------------------------------------------------


def test_wall_clock_is_aggregated_and_generation_time_is_not_it(store: LLMUsageStore) -> None:
    """`duration_ms` was on every row and summed nowhere, so "how long do calls take" had no
    answer while the data sat there. It is not `generation_ms`: one includes the wait before the
    first token and the other does not."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, duration_ms=1_500))
    store.record(_record(started_at_ms=now, duration_ms=2_500))

    row = store.usage_payload()["providers_30d"][0]

    assert row["duration_ms"] == 4_000


def test_a_truncated_answer_is_counted_rather_than_read_as_a_success(
    store: LLMUsageStore,
) -> None:
    """`finish_reason='length'` means the model was cut off mid-sentence. `failed_requests` counts
    `error` and `cancelled` only, so a deployment hitting its `maxTokens` saw a clean 0% failure
    rate over answers that were all truncated."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, finish_reason="stop"))
    store.record(_record(started_at_ms=now, finish_reason="length"))
    store.record(_record(started_at_ms=now, finish_reason="length"))

    row = store.usage_payload()["providers_30d"][0]

    assert row["requests"] == 3
    assert row["truncated_requests"] == 2
    # And it is still not a failure, because it is not one: the answer arrived, cut short.
    assert row["failed_requests"] == 0


def test_streamed_calls_are_counted_so_time_to_first_token_has_a_denominator(
    store: LLMUsageStore,
) -> None:
    """`ttft_ms` only means anything for a streamed call. Averaging it over every call divides by
    a denominator the reader cannot see."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, stream=True))
    store.record(_record(started_at_ms=now, stream=True))
    store.record(_record(started_at_ms=now, stream=False))

    row = store.usage_payload()["providers_30d"][0]

    assert row["requests"] == 3
    assert row["streamed_requests"] == 2


def test_a_failure_says_why_rather_than_only_how_many(store: LLMUsageStore) -> None:
    """`error_kind` and `error_status_code` were stored on every failed row and queried by
    nothing, so "16 failed (4%)" could not distinguish a rate limit from an expired key."""
    now = _now_ms()
    for _ in range(3):
        store.record(
            _record(
                started_at_ms=now,
                finish_reason="error",
                error_kind="rate_limit",
                error_status_code=429,
            )
        )
    store.record(
        _record(
            started_at_ms=now,
            finish_reason="error",
            error_kind="authentication",
            error_status_code=401,
        )
    )

    failures = store.usage_payload()["failures"]

    assert [(row["error_kind"], row["status_code"], row["requests"]) for row in failures] == [
        ("rate_limit", 429, 3),
        ("authentication", 401, 1),
    ]
    assert failures[0]["provider"] == "moonshot"


def test_a_cancelled_call_is_a_failure_with_its_own_reason(store: LLMUsageStore) -> None:
    """Both `error` and `cancelled` count as failed, and the breakdown has to cover both or the
    numbers on the page and in the table disagree."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, finish_reason="cancelled"))

    payload = store.usage_payload()

    assert payload["failed_requests_30d"] == 1
    assert sum(row["requests"] for row in payload["failures"]) == 1


# --- the window -------------------------------------------------------------------------------


def test_the_breakdown_window_is_the_callers_choice(store: LLMUsageStore) -> None:
    """Thirty days was hard-coded while the store keeps four hundred, so a reader could not ask a
    question the data already answered."""
    now = datetime.now(tz=timezone.utc)
    recent = int(now.timestamp() * 1000)
    old = recent - 45 * 86_400_000
    store.record(_record(started_at_ms=recent, model="recent"))
    store.record(_record(started_at_ms=old, model="old"))

    thirty = {row["model"] for row in store.usage_payload(window_days=30)["providers_30d"]}
    ninety = {row["model"] for row in store.usage_payload(window_days=90)["providers_30d"]}

    assert thirty == {"recent"}
    assert ninety == {"recent", "old"}


def test_the_window_is_clamped_to_what_the_store_actually_keeps(store: LLMUsageStore) -> None:
    """A window wider than retention would promise rows that were purged."""
    assert store.usage_payload(window_days=10_000)["window_days"] == MAX_DAYS_RETAINED
    assert store.usage_payload(window_days=0)["window_days"] == 1


def test_the_window_travels_on_the_payload(store: LLMUsageStore) -> None:
    """A table headed "by model" over an unstated window is a table nobody can check."""
    assert store.usage_payload(window_days=7)["window_days"] == 7


# --- what started the turns -------------------------------------------------------------------


def test_the_window_is_broken_down_by_source(store: LLMUsageStore) -> None:
    """`source` was aggregated per day and read only inside a heatmap cell's tooltip.

    "What does automation cost me this month" is the question the column exists for, and answering
    it meant opening thirty tooltips and adding them up.
    """
    now = _now_ms()
    store.record(_record(started_at_ms=now, source="user", usage=LLMUsage(1_000, 100, 1_100, reported_tokens=1_100)))
    store.record(_record(started_at_ms=now, source="cron", usage=LLMUsage(4_000, 200, 4_200, reported_tokens=4_200)))
    store.record(_record(started_at_ms=now, source="cron", usage=LLMUsage(2_000, 100, 2_100, reported_tokens=2_100)))

    rows = {row["source"]: row for row in store.usage_payload()["sources_window"]}

    assert rows["cron"]["total_tokens"] == 6_300
    assert rows["cron"]["requests"] == 2
    assert rows["user"]["total_tokens"] == 1_100
    assert rows["user"]["requests"] == 1


def test_the_source_breakdown_is_ordered_by_what_it_cost(store: LLMUsageStore) -> None:
    """The expensive source first, because that is the one a reader is looking for."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, source="user", usage=LLMUsage(10, 1, 11, reported_tokens=11)))
    store.record(_record(started_at_ms=now, source="cron", usage=LLMUsage(9_000, 100, 9_100, reported_tokens=9_100)))

    assert [row["source"] for row in store.usage_payload()["sources_window"]] == ["cron", "user"]


def test_the_source_breakdown_respects_the_window(store: LLMUsageStore) -> None:
    now = datetime.now(tz=timezone.utc)
    recent = int(now.timestamp() * 1000)
    old = recent - 45 * 86_400_000
    store.record(_record(started_at_ms=recent, source="user"))
    store.record(_record(started_at_ms=old, source="cron"))

    thirty = {row["source"] for row in store.usage_payload(window_days=30)["sources_window"]}
    ninety = {row["source"] for row in store.usage_payload(window_days=90)["sources_window"]}

    assert thirty == {"user"}
    assert ninety == {"user", "cron"}


def test_an_unattributed_call_reads_as_system_rather_than_as_a_person(
    store: LLMUsageStore,
) -> None:
    """Over-counting `user` would flatter the figure that matters most."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, source="system"))

    assert [row["source"] for row in store.usage_payload()["sources_window"]] == ["system"]


def test_an_empty_store_reports_no_sources_rather_than_a_fabricated_zero_row(
    store: LLMUsageStore,
) -> None:
    assert store.usage_payload()["sources_window"] == []


# --- the per-model measurements the table checks itself against -------------------------------


def test_the_reported_and_estimated_call_split_is_per_model(store: LLMUsageStore) -> None:
    """Both counts were computed in SQL and displayed nowhere, so "is this figure a bill or a
    guess" could not be answered per model."""
    now = _now_ms()
    store.record(
        _record(started_at_ms=now, usage=LLMUsage(1_000, 100, 1_100, reported_tokens=1_100))
    )
    # Estimated: our own tokenizer counted it, because the provider reported nothing.
    store.record(
        _record(started_at_ms=now, usage=LLMUsage(1_000, 100, 1_100, estimated_tokens=1_100))
    )

    row = store.usage_payload()["providers_30d"][0]

    assert row["provider_requests"] == 1
    assert row["estimated_requests"] == 1
    assert row["requests"] == 2


def test_generation_time_and_measured_output_are_both_per_model(store: LLMUsageStore) -> None:
    """`generation_ms` is the denominator of throughput and `measured_output_tokens` is what the
    token calibration learns from. Both were summed and shown to nobody."""
    now = _now_ms()
    store.record(_record(started_at_ms=now, usage=LLMUsage(100, 50, 150, reported_tokens=150)))

    row = store.usage_payload()["providers_30d"][0]

    # Present rather than a specific figure: a record with no measured streaming telemetry sums
    # to zero here, and the point of the test is that the columns reach the payload at all.
    assert "generation_ms" in row
    assert "measured_completion_tokens" in row
    assert "timed_requests" in row
