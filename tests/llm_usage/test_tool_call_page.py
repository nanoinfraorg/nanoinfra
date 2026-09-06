"""The reader `tool_calls` never had (#232, #235 phase 3).

The table has been written since 2.0.0 -- fourteen columns, a pruner, a purge log -- and until
`tool_call_page` nothing read it. These tests pin the four properties that make it usable rather
than merely present:

* **keyset, not offset.** Rows arrive while somebody is paging, and an offset skips or repeats.
* **bound parameters.** Every filter is a value a browser sent.
* **facets from the table.** The UI must not be able to offer a filter value the data lacks.
* **the purge is visible.** Without it a reader cannot tell an empty window from a purged one.

And one negative: the page must not grow a field an argument could be written to. That is the
whole design of the row, and a reader is exactly where it would be tempting to break.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from nanoinfra.llm_usage.models import ToolCallRecord
from nanoinfra.llm_usage.store import MAX_TOOL_CALL_DAYS_RETAINED, LLMUsageStore


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


# --- the page itself ---------------------------------------------------------------------


def test_an_empty_table_reads_as_an_empty_page_and_not_an_error(store: LLMUsageStore) -> None:
    page = store.tool_call_page()

    assert page["calls"] == []
    assert page["has_more"] is False
    assert page["next_before_id"] is None
    assert page["retention_days"] == MAX_TOOL_CALL_DAYS_RETAINED
    assert page["last_purge"] is None


def test_newest_first_because_that_is_what_a_reader_opens_it_for(store: LLMUsageStore) -> None:
    now = int(time.time() * 1000)
    for offset, tool in enumerate(("first", "second", "third")):
        store.record_tool_call(_call(ts_ms=now + offset, tool=tool))

    page = store.tool_call_page()

    assert [row["tool"] for row in page["calls"]] == ["third", "second", "first"]


def test_the_row_carries_every_column_the_table_holds(store: LLMUsageStore) -> None:
    store.record_tool_call(
        _call(
            tool="read_file",
            source="cron",
            outcome="error",
            duration_ms=42,
            session_key="telegram:1",
            turn_id="turn-9",
            seq=4,
            actor="alberto",
            capability_class="read.local",
            gate_decision="allow",
            gate_reason="standing grant",
            error_kind="tool_error",
        )
    )

    row = store.tool_call_page()["calls"][0]

    # Fourteen written columns, fourteen read. A column with no reader does not exist.
    assert row["tool"] == "read_file"
    assert row["source"] == "cron"
    assert row["outcome"] == "error"
    assert row["duration_ms"] == 42
    assert row["session_key"] == "telegram:1"
    assert row["turn_id"] == "turn-9"
    assert row["seq"] == 4
    assert row["actor"] == "alberto"
    assert row["capability_class"] == "read.local"
    assert row["gate_decision"] == "allow"
    assert row["gate_reason"] == "standing grant"
    assert row["error_kind"] == "tool_error"
    assert row["ts_ms"] > 0
    assert row["id"] > 0


def test_the_page_holds_no_field_an_argument_could_be_written_to(store: LLMUsageStore) -> None:
    """The row addresses the arguments; the transcript holds them (#232).

    Asserted of the payload and not only of the schema, because a reader is where the temptation
    to "just include the command" lands.
    """
    store.record_tool_call(_call())

    keys = set(store.tool_call_page()["calls"][0])

    forbidden = {
        "arguments",
        "args",
        "input",
        "command",
        "command_text",
        "payload",
        "output",
        "result",
        "content",
        "path",
    }
    assert keys & forbidden == set()


# --- paging -----------------------------------------------------------------------------


def test_paging_is_a_keyset_cursor_and_the_pages_do_not_overlap(store: LLMUsageStore) -> None:
    now = int(time.time() * 1000)
    for index in range(5):
        store.record_tool_call(_call(ts_ms=now + index, tool=f"tool-{index}"))

    first = store.tool_call_page(limit=2)
    assert first["has_more"] is True
    assert first["next_before_id"] is not None

    second = store.tool_call_page(limit=2, before_id=first["next_before_id"])

    seen = [row["id"] for row in first["calls"]] + [row["id"] for row in second["calls"]]
    assert len(seen) == len(set(seen)) == 4
    # Strictly descending across the boundary: the cursor is `id < ?`, not an offset.
    assert seen == sorted(seen, reverse=True)


def test_a_row_written_between_two_pages_cannot_shift_the_second(store: LLMUsageStore) -> None:
    """The bug an `OFFSET` would have: a new row pushes one down and it is read twice."""
    now = int(time.time() * 1000)
    for index in range(4):
        store.record_tool_call(_call(ts_ms=now + index, tool=f"old-{index}"))

    first = store.tool_call_page(limit=2)
    # Somebody keeps working while the page is on screen.
    store.record_tool_call(_call(ts_ms=now + 99, tool="brand-new"))
    second = store.tool_call_page(limit=2, before_id=first["next_before_id"])

    tools = [row["tool"] for row in second["calls"]]
    assert "brand-new" not in tools
    assert set(tools).isdisjoint({row["tool"] for row in first["calls"]})


def test_the_last_page_says_so_rather_than_offering_a_cursor(store: LLMUsageStore) -> None:
    store.record_tool_call(_call())

    page = store.tool_call_page(limit=10)

    assert page["has_more"] is False
    assert page["next_before_id"] is None


def test_a_limit_a_browser_sent_is_clamped(store: LLMUsageStore) -> None:
    for _ in range(3):
        store.record_tool_call(_call())

    # No unbounded page: the route takes this number from a query string.
    assert len(store.tool_call_page(limit=10_000)["calls"]) == 3
    assert len(store.tool_call_page(limit=0)["calls"]) == 1
    assert len(store.tool_call_page(limit=-5)["calls"]) == 1


# --- filters ----------------------------------------------------------------------------


def test_each_filter_narrows_to_its_own_column(store: LLMUsageStore) -> None:
    store.record_tool_call(_call(tool="exec", outcome="ok", gate_decision="allow"))
    store.record_tool_call(_call(tool="read_file", outcome="error", gate_decision=None))
    store.record_tool_call(_call(tool="exec", outcome="denied", gate_decision="denied"))

    assert len(store.tool_call_page(tool="exec")["calls"]) == 2
    assert len(store.tool_call_page(outcome="error")["calls"]) == 1
    assert len(store.tool_call_page(gate_decision="denied")["calls"]) == 1


def test_filters_combine_rather_than_replace_one_another(store: LLMUsageStore) -> None:
    store.record_tool_call(_call(tool="exec", outcome="ok"))
    store.record_tool_call(_call(tool="exec", outcome="denied"))

    page = store.tool_call_page(tool="exec", outcome="denied")

    assert len(page["calls"]) == 1
    assert page["calls"][0]["outcome"] == "denied"


def test_one_turn_is_addressable_which_is_how_a_reader_expands_a_row(
    store: LLMUsageStore,
) -> None:
    store.record_tool_call(_call(session_key="webui:a", turn_id="turn-1", seq=0))
    store.record_tool_call(_call(session_key="webui:a", turn_id="turn-1", seq=1))
    store.record_tool_call(_call(session_key="webui:a", turn_id="turn-2", seq=0))
    store.record_tool_call(_call(session_key="webui:b", turn_id="turn-1", seq=0))

    page = store.tool_call_page(session_key="webui:a", turn_id="turn-1")

    assert len(page["calls"]) == 2
    assert {row["seq"] for row in page["calls"]} == {0, 1}


@pytest.mark.parametrize(
    "hostile",
    [
        "' OR 1=1 --",
        "exec'; DROP TABLE tool_calls; --",
        '" UNION SELECT 1 --',
    ],
)
def test_a_filter_value_is_data_and_never_sql(store: LLMUsageStore, hostile: str) -> None:
    """Every filter arrives from a query string, so this is the property that matters most."""
    store.record_tool_call(_call(tool="exec"))

    page = store.tool_call_page(tool=hostile)

    assert page["calls"] == []
    # The table is still there, and still holds the row.
    assert len(store.tool_call_page()["calls"]) == 1


def test_an_empty_filter_means_no_filter_rather_than_a_match_on_empty(
    store: LLMUsageStore,
) -> None:
    store.record_tool_call(_call(tool="exec"))

    assert len(store.tool_call_page(tool="")["calls"]) == 1
    assert len(store.tool_call_page(outcome="")["calls"]) == 1


# --- facets -----------------------------------------------------------------------------


def test_the_facets_are_what_the_table_holds_and_nothing_else(store: LLMUsageStore) -> None:
    store.record_tool_call(_call(tool="exec", outcome="ok", gate_decision="allow"))
    store.record_tool_call(_call(tool="read_file", outcome="denied", gate_decision="denied"))

    page = store.tool_call_page()

    assert page["tools"] == ["exec", "read_file"]
    assert page["outcomes"] == ["denied", "ok"]
    assert page["gate_decisions"] == ["allow", "denied"]


def test_an_ungated_call_contributes_no_gate_facet(store: LLMUsageStore) -> None:
    """A blank is not a decision, and a filter offering it would match nothing."""
    store.record_tool_call(_call(gate_decision=None))

    assert store.tool_call_page()["gate_decisions"] == []


def test_the_facets_ignore_the_filters_so_a_reader_can_widen_again(
    store: LLMUsageStore,
) -> None:
    store.record_tool_call(_call(tool="exec"))
    store.record_tool_call(_call(tool="read_file"))

    page = store.tool_call_page(tool="exec")

    assert len(page["calls"]) == 1
    # A facet list narrowed by the current filter is a dead end: the only value left is the one
    # already chosen, and nothing in the UI can get back.
    assert page["tools"] == ["exec", "read_file"]


# --- retention --------------------------------------------------------------------------


def test_the_page_names_the_retention_window(store: LLMUsageStore) -> None:
    store.record_tool_call(_call())

    assert store.tool_call_page()["retention_days"] == MAX_TOOL_CALL_DAYS_RETAINED


def test_the_last_purge_is_readable_at_last(store: LLMUsageStore) -> None:
    """Written by the pruner since #234 and read by nobody until this page.

    Without it, an empty result and a purged window render identically -- and one of those two is
    a deployment doing nothing while the other is a deployment whose evidence expired.
    """
    now = time.time()
    old_ms = int((now - (MAX_TOOL_CALL_DAYS_RETAINED + 1) * 86_400) * 1000)
    store.record_tool_call(_call(ts_ms=old_ms))
    store.record_tool_call(_call(ts_ms=int(now * 1000)))

    store._prune_tool_calls(  # pyright: ignore[reportPrivateUsage]
        store._connect(),  # pyright: ignore[reportPrivateUsage]
        now=now,
    )
    page = store.tool_call_page()

    assert len(page["calls"]) == 1
    assert page["last_purge"] is not None
    assert page["last_purge"]["rows_purged"] == 1
    assert page["last_purge"]["cutoff_ms"] < int(now * 1000)
