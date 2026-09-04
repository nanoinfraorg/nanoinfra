"""One row per tool call, and the address of its arguments rather than the arguments (#232).

The tests are organised around the two claims the table makes: that "who ran `execute_on_server`
yesterday, did it succeed and how long did it take" is one indexed query, and that the row cannot
become a second copy of the conversation.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.llm_usage.models import LLMCallRecord, ToolCallRecord
from nanoinfra.llm_usage.store import (
    MAX_TOOL_CALL_DAYS_RETAINED,
    MAX_TOOL_CALL_PURGES_RETAINED,
    SCHEMA_VERSION,
    LLMUsageStore,
)
from nanoinfra.providers.base import LLMUsage

#: The 13 columns #232 names, and `id`. The test below compares against this list rather than a
#: count, because the point of the list is which columns are *absent*.
_EXPECTED_COLUMNS = [
    "id",
    "ts_ms",
    "session_key",
    "turn_id",
    "seq",
    "tool",
    "source",
    "actor",
    "capability_class",
    "gate_decision",
    "gate_reason",
    "outcome",
    "duration_ms",
    "error_kind",
]


def _call(
    *,
    ts_ms: int | None = None,
    tool: str = "execute_on_server",
    source: str = "user",
    outcome: str = "ok",
    duration_ms: int = 1_200,
    session_key: str | None = "webui:alberto",
    turn_id: str | None = "turn-1",
    seq: int | None = 0,
    actor: str | None = "alberto",
    capability_class: str | None = "mutate.remote",
    gate_decision: str | None = None,
    gate_reason: str | None = None,
    error_kind: str | None = None,
) -> ToolCallRecord:
    return ToolCallRecord(
        ts_ms=ts_ms if ts_ms is not None else int(time.time() * 1000),
        tool=tool,
        source=source,  # pyright: ignore[reportArgumentType]
        outcome=outcome,
        duration_ms=duration_ms,
        session_key=session_key,
        turn_id=turn_id,
        seq=seq,
        actor=actor,
        capability_class=capability_class,
        gate_decision=gate_decision,
        gate_reason=gate_reason,
        error_kind=error_kind,
    )


@pytest.fixture
def store(tmp_path: Path) -> LLMUsageStore:
    return LLMUsageStore(tmp_path / "llm-usage.sqlite3")


# --- the question this exists for --------------------------------------------------------


def test_one_row_carries_the_class_the_outcome_and_the_duration(store: LLMUsageStore) -> None:
    store.record_tool_call(_call())

    rows = store.tool_calls()

    assert len(rows) == 1
    assert rows[0]["tool"] == "execute_on_server"
    assert rows[0]["capability_class"] == "mutate.remote"
    assert rows[0]["outcome"] == "ok"
    assert rows[0]["duration_ms"] == 1_200


def test_a_row_addresses_the_arguments_instead_of_holding_them(store: LLMUsageStore) -> None:
    """`session_key + turn_id + seq` is where a reader goes to expand the row."""
    store.record_tool_call(_call(session_key="webui:alberto", turn_id="turn-7", seq=3))

    row = store.tool_calls()[0]

    assert (row["session_key"], row["turn_id"], row["seq"]) == ("webui:alberto", "turn-7", 3)


def test_the_schema_has_no_column_an_argument_could_be_written_to(
    store: LLMUsageStore,
) -> None:
    store.record_tool_call(_call())
    connection = store._read_connection()  # pyright: ignore[reportPrivateUsage]
    try:
        columns = [
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(tool_calls)").fetchall()
        ]
    finally:
        connection.close()

    assert columns == _EXPECTED_COLUMNS


def test_a_denied_call_is_recorded_as_denied_rather_than_as_an_error(
    store: LLMUsageStore,
) -> None:
    """A gate refusing an action is the deployment working, and not a tool that broke (#233)."""
    store.record_tool_call(_call(outcome="denied", gate_decision="denied", gate_reason="no grant"))
    store.record_tool_call(_call(outcome="error", error_kind="tool_error"))

    assert [row["outcome"] for row in store.tool_calls(outcome="denied")] == ["denied"]
    assert [row["outcome"] for row in store.tool_calls(outcome="error")] == ["error"]


def test_the_decision_column_is_empty_when_no_gate_answered(store: LLMUsageStore) -> None:
    """A deployment with no gate configured still gets rows -- with a blank, not an `allow`."""
    store.record_tool_call(_call())

    row = store.tool_calls()[0]

    assert row["gate_decision"] is None
    assert row["gate_reason"] is None


def test_rows_filter_by_the_names_a_reader_asks_for(store: LLMUsageStore) -> None:
    store.record_tool_call(_call(tool="read_file", capability_class="read", source="user"))
    store.record_tool_call(_call(tool="execute_on_server", source="cron", actor=None))

    assert [row["tool"] for row in store.tool_calls(tool="read_file")] == ["read_file"]
    assert [row["source"] for row in store.tool_calls(source="cron")] == ["cron"]
    assert [row["actor"] for row in store.tool_calls(actor="alberto")] == ["alberto"]
    assert len(store.tool_calls(session_key="webui:alberto")) == 2


def test_rows_come_back_newest_first(store: LLMUsageStore) -> None:
    now = int(time.time() * 1000)
    store.record_tool_calls([
        _call(ts_ms=now - 3_000, tool="first"),
        _call(ts_ms=now - 1_000, tool="second"),
    ])

    assert [row["tool"] for row in store.tool_calls()] == ["second", "first"]


# --- what a row may not carry ------------------------------------------------------------


def test_an_unrecognised_error_kind_becomes_other_rather_than_being_kept(
    store: LLMUsageStore,
) -> None:
    """The same rule as the provider error kinds: a kind, never the message."""
    store.record_tool_call(
        _call(outcome="error", error_kind="rm -rf /srv/app failed: permission denied")
    )

    assert store.tool_calls()[0]["error_kind"] == "other"


def test_a_record_refuses_an_outcome_it_does_not_know() -> None:
    with pytest.raises(ValueError, match="not one of"):
        _call(outcome="succeeded")


def test_a_record_refuses_a_source_the_heatmap_does_not_know() -> None:
    with pytest.raises(ValueError, match="not one of"):
        _call(source="telegram")


def test_a_gate_reason_is_bounded_so_it_cannot_become_a_document(store: LLMUsageStore) -> None:
    store.record_tool_call(_call(gate_decision="denied", gate_reason="x" * 4_000))

    assert len(str(store.tool_calls()[0]["gate_reason"])) == 240


# --- the window, and the count of what it took -------------------------------------------


def test_rows_past_the_window_are_purged_and_the_count_is_kept(store: LLMUsageStore) -> None:
    """#234: the count is what stops a gap in the history from looking like a quiet week."""
    now = time.time()
    old_ms = int((now - (MAX_TOOL_CALL_DAYS_RETAINED + 1) * 86_400) * 1000)
    store.record_tool_calls([_call(ts_ms=old_ms) for _ in range(3)])
    store.record_tool_call(_call())

    purged = store._prune_tool_calls(  # pyright: ignore[reportPrivateUsage]
        store._connect(),  # pyright: ignore[reportPrivateUsage]
        now=now,
    )

    assert purged == 3
    assert store.tool_call_count() == 1
    ledger = store.tool_call_purges()
    assert ledger["rows_purged"] == 3
    assert ledger["purges"] == 1
    assert ledger["retention_days"] == MAX_TOOL_CALL_DAYS_RETAINED
    assert ledger["last_purge_ms"] is not None


def test_a_purge_that_took_nothing_writes_no_entry(store: LLMUsageStore) -> None:
    """Otherwise the ledger fills with zeros and the number a reader wants is buried."""
    store.record_tool_call(_call())

    store._prune_tool_calls(store._connect())  # pyright: ignore[reportPrivateUsage]

    assert store.tool_call_purges() == {
        "purges": 0,
        "rows_purged": 0,
        "last_purge_ms": None,
        "retention_days": MAX_TOOL_CALL_DAYS_RETAINED,
    }


def test_the_purge_ledger_is_bounded(store: LLMUsageStore) -> None:
    now = time.time()
    old_ms = int((now - (MAX_TOOL_CALL_DAYS_RETAINED + 1) * 86_400) * 1000)
    connection = store._connect()  # pyright: ignore[reportPrivateUsage]
    for _ in range(MAX_TOOL_CALL_PURGES_RETAINED + 5):
        store.record_tool_call(_call(ts_ms=old_ms))
        store._prune_tool_calls(connection, now=now)  # pyright: ignore[reportPrivateUsage]

    ledger = store.tool_call_purges()

    assert ledger["purges"] == MAX_TOOL_CALL_PURGES_RETAINED
    # The entries went; the rows they counted are still counted by the ones that stayed.
    assert ledger["rows_purged"] == MAX_TOOL_CALL_PURGES_RETAINED


def test_the_window_is_shorter_than_the_usage_window(store: LLMUsageStore) -> None:
    from nanoinfra.llm_usage.store import MAX_DAYS_RETAINED

    assert MAX_TOOL_CALL_DAYS_RETAINED == 180, "the window is a promise the docs make"
    assert MAX_TOOL_CALL_DAYS_RETAINED < MAX_DAYS_RETAINED


# --- one store per workspace -------------------------------------------------------------


def test_the_table_is_per_workspace_like_the_database_that_holds_it(tmp_path: Path) -> None:
    first = LLMUsageStore(tmp_path / "one" / "llm-usage.sqlite3")
    second = LLMUsageStore(tmp_path / "two" / "llm-usage.sqlite3")

    first.record_tool_call(_call(tool="read_file"))

    assert [row["tool"] for row in first.tool_calls()] == ["read_file"]
    assert second.tool_calls() == []


def test_a_missing_database_reads_as_empty_rather_than_raising(tmp_path: Path) -> None:
    store = LLMUsageStore(tmp_path / "nested" / "llm-usage.sqlite3")

    assert store.tool_calls() == []
    assert store.tool_call_count() == 0


def test_the_read_connection_cannot_write_a_tool_call(store: LLMUsageStore) -> None:
    store.record_tool_call(_call())
    connection = store._read_connection()  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Exception, match="readonly|read-only|query_only|attempt to write"):
        connection.execute("DELETE FROM tool_calls")


# --- an existing database ----------------------------------------------------------------

#: The schema as version 1 shipped it (#176), reproduced rather than imported: the point of the
#: test is a database that predates the change, and importing today's DDL would prove nothing.
_V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY,
    started_at_ms INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    source TEXT NOT NULL,
    stream INTEGER NOT NULL,
    finish_reason TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    reported_tokens INTEGER,
    estimated_tokens INTEGER,
    generation_ms INTEGER,
    measured_output_tokens INTEGER,
    ttft_ms INTEGER,
    timed_requests INTEGER,
    error_status_code INTEGER,
    error_kind TEXT
);
CREATE INDEX IF NOT EXISTS llm_calls_started_at_idx ON llm_calls(started_at_ms);
PRAGMA user_version = 1;
"""


def _write_a_version_1_database(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executescript(_V1_SCHEMA)
        connection.execute(
            """
            INSERT INTO llm_calls (
                started_at_ms, duration_ms, provider, model, source, stream, finish_reason,
                input_tokens, output_tokens, total_tokens, reported_tokens, estimated_tokens
            ) VALUES (?, 900, 'anthropic', 'claude-sonnet-5', 'user', 0, 'stop', 900, 100,
                      1000, 1000, 0)
            """,
            (int(time.time() * 1000),),
        )
    finally:
        connection.close()


def test_an_existing_database_gains_the_table_and_keeps_its_usage_rows(tmp_path: Path) -> None:
    """The upgrade path. Nobody's history stops being visible because a table arrived."""
    path = tmp_path / "llm-usage.sqlite3"
    _write_a_version_1_database(path)

    store = LLMUsageStore(path)
    store.record_tool_call(_call())

    assert store.count() == 1, "the usage row that was already there survived"
    assert store.usage_payload()["total_tokens"] == 1_000
    assert store.tool_call_count() == 1
    connection = store._read_connection()  # pyright: ignore[reportPrivateUsage]
    try:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    finally:
        connection.close()
    assert version == SCHEMA_VERSION == 2


def test_the_two_tables_do_not_disturb_each_other(store: LLMUsageStore) -> None:
    store.record(
        LLMCallRecord(
            started_at_ms=int(time.time() * 1000),
            duration_ms=900,
            provider="anthropic",
            model="claude-sonnet-5",
            source="user",
            stream=False,
            finish_reason="stop",
            usage=LLMUsage.reported(input_tokens=1_000, output_tokens=100),
        )
    )
    store.record_tool_call(_call())

    payload: dict[str, Any] = store.usage_payload()

    assert payload["requests_30d"] == 1
    assert store.tool_call_count() == 1
