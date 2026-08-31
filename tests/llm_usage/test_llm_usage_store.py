"""One row per attempt, and the questions a day row could not answer (#176).

The tests are organised around the two claims the store makes: that it can answer *how many calls
failed and retried, and what did the retry cost*, and that a row cannot become a transcript.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanoinfra.llm_usage.models import LLMCallRecord
from nanoinfra.llm_usage.store import (
    MAX_CALLS_RETAINED,
    LLMUsageStore,
)
from nanoinfra.providers.base import LLMUsage


def _record(
    *,
    started_at_ms: int | None = None,
    provider: str = "anthropic",
    model: str = "claude-sonnet-5",
    source: str = "user",
    finish_reason: str = "stop",
    usage: LLMUsage | None = None,
    stream: bool = False,
    error_kind: str | None = None,
    error_status_code: int | None = None,
    duration_ms: int = 900,
) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=started_at_ms if started_at_ms is not None else int(time.time() * 1000),
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


def _ms(year: int, month: int, day: int, hour: int = 12) -> int:
    return int(datetime(year, month, day, hour, tzinfo=timezone.utc).timestamp() * 1000)


# --- the question this exists for --------------------------------------------------------


def test_a_retry_is_two_rows_and_the_retry_cost_is_answerable(store: LLMUsageStore) -> None:
    """The one thing a daily aggregate cannot do, in principle rather than for want of fields."""
    store.record(_record(finish_reason="error", error_kind="rate_limit", error_status_code=429))
    store.record(_record(usage=LLMUsage.reported(input_tokens=1_000, output_tokens=100)))

    payload = store.usage_payload()

    assert payload["requests_30d"] == 2
    assert payload["failed_requests_30d"] == 1
    assert payload["total_tokens_30d"] == 1_100


def test_the_provider_and_model_of_a_cost_are_answerable(store: LLMUsageStore) -> None:
    """A day row carried no provider at all, so "what is the expensive model" had no answer."""
    store.record(
        _record(
            provider="anthropic",
            model="claude-sonnet-5",
            usage=LLMUsage.reported(input_tokens=900, output_tokens=100),
        )
    )
    store.record(
        _record(
            provider="openai",
            model="gpt-5.6",
            usage=LLMUsage.reported(input_tokens=90, output_tokens=10),
        )
    )

    rows = store.usage_payload()["providers_30d"]

    assert [(row["provider"], row["model"], row["total_tokens"]) for row in rows] == [
        ("anthropic", "claude-sonnet-5", 1_000),
        ("openai", "gpt-5.6", 100),
    ]


# --- what a row may not carry ------------------------------------------------------------


def test_a_provider_error_message_is_reduced_to_a_kind(store: LLMUsageStore) -> None:
    """Error text is the field most likely to quote a prompt back, so it is not stored."""
    store.record(
        _record(
            finish_reason="error",
            error_kind="Request failed: user asked about ACME's Q3 revenue",
            error_status_code=400,
        )
    )

    row = store._read_connection().execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT error_kind FROM llm_calls"
    ).fetchone()

    assert row["error_kind"] == "other"


def test_an_unknown_finish_reason_is_reduced_too(store: LLMUsageStore) -> None:
    store.record(_record(finish_reason="some_new_provider_string"))

    row = store._read_connection().execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT finish_reason FROM llm_calls"
    ).fetchone()

    assert row["finish_reason"] == "other"


def test_a_status_code_that_is_not_a_status_code_is_dropped(store: LLMUsageStore) -> None:
    store.record(_record(finish_reason="error", error_status_code=99_999))

    row = store._read_connection().execute(  # pyright: ignore[reportPrivateUsage]
        "SELECT error_status_code FROM llm_calls"
    ).fetchone()

    assert row["error_status_code"] is None


def test_the_schema_has_no_column_content_could_go_in(store: LLMUsageStore) -> None:
    """The exclusions as a test: no prompts, no responses, no reasoning, no tools, no sessions."""
    store.record(_record())
    columns = {
        row["name"]
        for row in store._read_connection()  # pyright: ignore[reportPrivateUsage]
        .execute("PRAGMA table_info(llm_calls)")
        .fetchall()
    }

    forbidden = {
        "messages",
        "prompt",
        "response",
        "content",
        "reasoning",
        "tool_calls",
        "session_key",
        "chat_id",
        "user",
        "error_message",
    }
    assert columns & forbidden == set()


def test_a_record_refuses_a_source_it_does_not_know() -> None:
    with pytest.raises(ValueError, match="not one of"):
        _record(source="marketing")


# --- the numbers -------------------------------------------------------------------------


def test_an_unmeasured_call_still_counts_as_a_request(store: LLMUsageStore) -> None:
    store.record(_record(usage=None))

    payload = store.usage_payload()

    assert payload["requests_30d"] == 1
    assert payload["total_tokens_30d"] == 0


def test_the_partition_survives_the_round_trip(store: LLMUsageStore) -> None:
    store.record(_record(usage=LLMUsage.reported(input_tokens=100, output_tokens=10)))
    store.record(_record(usage=LLMUsage.estimated(input_tokens=50, output_tokens=5)))

    day = store.usage_payload()["days"][-1]

    assert day["provider_tokens"] == 110
    assert day["estimated_tokens"] == 55
    assert day["provider_tokens"] + day["estimated_tokens"] == day["total_tokens"]
    assert day["provider_requests"] == 1
    assert day["estimated_requests"] == 1


def test_rows_are_grouped_by_local_day(store: LLMUsageStore) -> None:
    """A heatmap of days is read by somebody in a timezone."""
    store.record(
        _record(
            started_at_ms=_ms(2026, 6, 2, hour=18),
            usage=LLMUsage.reported(input_tokens=10, output_tokens=1),
        )
    )

    utc_days = [row["date"] for row in store.usage_payload(timezone_name="UTC")["days"]]
    shanghai_days = [
        row["date"] for row in store.usage_payload(timezone_name="Asia/Shanghai")["days"]
    ]

    assert utc_days == ["2026-06-02"]
    assert shanghai_days == ["2026-06-03"]


def test_each_day_breaks_down_by_source(store: LLMUsageStore) -> None:
    store.record(_record(source="user", usage=LLMUsage.reported(input_tokens=90, output_tokens=10)))
    store.record(_record(source="cron", usage=LLMUsage.reported(input_tokens=9, output_tokens=1)))

    sources = store.usage_payload()["days"][-1]["sources"]

    assert sources["user"]["total_tokens"] == 100
    assert sources["cron"]["total_tokens"] == 10


def test_a_bad_timezone_name_falls_back_to_utc_rather_than_raising(store: LLMUsageStore) -> None:
    store.record(_record(usage=LLMUsage.reported(input_tokens=1, output_tokens=1)))

    assert store.usage_payload(timezone_name="Mars/Olympus")["days"]


# --- bounds ------------------------------------------------------------------------------


def test_rows_past_the_retention_window_are_pruned(store: LLMUsageStore) -> None:
    old_ms = int((time.time() - 500 * 86_400) * 1000)
    store.record_many([
        _record(started_at_ms=old_ms, usage=LLMUsage.reported(input_tokens=1, output_tokens=1))
        for _ in range(2)
    ])
    # Pruning is due after enough writes, so this forces the check rather than writing 500 rows.
    store._prune(store._connect())  # pyright: ignore[reportPrivateUsage]

    assert store.count() == 0


def test_the_row_count_is_bounded(store: LLMUsageStore) -> None:
    assert MAX_CALLS_RETAINED == 100_000, "the cap is a promise the docs make"


# --- reads do not write ------------------------------------------------------------------


def test_the_read_connection_cannot_write(store: LLMUsageStore) -> None:
    """A payload request must not be able to alter telemetry."""
    store.record(_record())
    connection = store._read_connection()  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Exception, match="readonly|read-only|query_only|attempt to write"):
        connection.execute("DELETE FROM llm_calls")


def test_a_missing_database_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    payload = LLMUsageStore(tmp_path / "nested" / "llm-usage.sqlite3").usage_payload()

    assert payload["days"] == []
    assert payload["total_tokens"] == 0
