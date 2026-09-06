"""Rates and tails, and the one property that has to hold between them and the tables (#274).

`gauges.py` reports levels. A Prometheus install also wants `rate()` and
`histogram_quantile()`, and neither works on a gauge — so these are process-lifetime
accumulators, monotonic between restarts, which is what a counter is.

**The reconciliation test is the reason this module exists rather than a `SELECT`.** A counter and
the Usage tab must never be derived from each other: a counter computed from the table falls when
the pruner runs, and a table derived from a counter loses everything on a restart. They are both
derived from the same event, so one turn must move both by the same amount — and they are *not*
equal in general, which is a difference this file states rather than hides.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from nanoinfra.llm_usage.counters import (
    DURATION_BUCKETS_MS,
    counter_exposition,
    record_llm_call_metrics,
    record_tool_call_metrics,
    reset_metrics,
    snapshot,
)
from nanoinfra.llm_usage.models import LLMCallRecord, ToolCallRecord
from nanoinfra.providers.base import LLMUsage


@pytest.fixture(autouse=True)
def _clean() -> None:
    """The accumulator is process-global, so a test that fills it must not leak into the next."""
    reset_metrics()
    yield
    reset_metrics()


def _call(
    *,
    provider: str = "moonshot",
    model: str = "kimi-k3",
    finish_reason: str = "stop",
    duration_ms: int = 1_200,
    usage: LLMUsage | None = None,
) -> LLMCallRecord:
    return LLMCallRecord(
        started_at_ms=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        duration_ms=duration_ms,
        provider=provider,
        model=model,
        source="user",  # pyright: ignore[reportArgumentType]
        stream=False,
        finish_reason=finish_reason,
        usage=usage,
    )


def _tool(
    *, tool: str = "exec", outcome: str = "ok", gate_decision: str | None = None
) -> ToolCallRecord:
    return ToolCallRecord(
        ts_ms=1_760_000_000_000,
        tool=tool,
        source="user",  # pyright: ignore[reportArgumentType]
        outcome=outcome,
        duration_ms=42,
        gate_decision=gate_decision,
    )


def _counter(name: str, **labels: str) -> int:
    wanted = tuple(sorted(labels.items()))
    return snapshot()["counters"].get((name, wanted), 0)


# --- the reconciliation property --------------------------------------------------------


def test_one_turn_moves_the_counter_and_the_table_by_the_same_amount(tmp_path: Path) -> None:
    """The property that makes two consumers of one event safe.

    They are not equal -- the counter resets on restart and the table survives it -- so what is
    asserted is that a single event moves both by one, from the same call, neither derived from
    the other.
    """
    from nanoinfra.llm_usage.store import LLMUsageStore

    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    call = _call(usage=LLMUsage(1_000, 100, 1_100, reported_tokens=1_100))

    store.record(call)
    record_llm_call_metrics(call)

    assert _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="stop"
    ) == 1
    assert store.usage_payload()["providers_30d"][0]["requests"] == 1


def test_a_tool_call_moves_both_by_one(tmp_path: Path) -> None:
    from nanoinfra.llm_usage.store import LLMUsageStore

    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    call = _tool(tool="read_file", outcome="ok")

    store.record_tool_call(call)
    record_tool_call_metrics(call)

    assert _counter(
        "nanoinfra_tool_calls_total", tool="read_file", outcome="ok", gate_decision="none"
    ) == 1
    assert len(store.tool_call_page()["calls"]) == 1


def test_the_public_entry_point_records_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`record_llm_call` is the seam, so the wiring is asserted there and not only per module."""
    from nanoinfra.llm_usage import record_llm_call
    from nanoinfra.llm_usage.store import LLMUsageStore

    store = LLMUsageStore(tmp_path / "llm-usage.sqlite3")
    monkeypatch.setattr("nanoinfra.llm_usage.get_llm_usage_store", lambda: store)

    record_llm_call(_call())

    assert _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="stop"
    ) == 1
    assert store.usage_payload()["providers_30d"][0]["requests"] == 1


def test_a_store_that_raises_still_moves_the_counter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Telemetry fails open, and the two halves fail independently.

    A counter that vanished because the database was unwritable would hide exactly the incident an
    operator is scraping to find.
    """
    from nanoinfra.llm_usage import record_llm_call

    def _explode() -> object:
        raise RuntimeError("no store")

    monkeypatch.setattr("nanoinfra.llm_usage.get_llm_usage_store", _explode)

    record_llm_call(_call())

    assert _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="stop"
    ) == 1


# --- what the counters count -------------------------------------------------------------


def test_calls_are_counted_by_provider_model_and_outcome() -> None:
    record_llm_call_metrics(_call(finish_reason="stop"))
    record_llm_call_metrics(_call(finish_reason="stop"))
    record_llm_call_metrics(_call(finish_reason="length"))

    assert _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="stop"
    ) == 2
    assert _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="length"
    ) == 1


def test_tokens_are_counted_per_kind_and_never_summed_into_one() -> None:
    """The four kinds are priced differently, so a single `tokens_total` would be unusable."""
    record_llm_call_metrics(
        _call(
            usage=LLMUsage(
                1_000, 100, 1_100, reported_tokens=1_100, cache_read_tokens=400,
                cache_write_tokens=50,
            )
        )
    )

    base = {"provider": "moonshot", "model": "kimi-k3"}
    assert _counter("nanoinfra_llm_tokens_total", **base, kind="input") == 1_000
    assert _counter("nanoinfra_llm_tokens_total", **base, kind="output") == 100
    assert _counter("nanoinfra_llm_tokens_total", **base, kind="cache_read") == 400
    assert _counter("nanoinfra_llm_tokens_total", **base, kind="cache_write") == 50


def test_an_ungated_tool_call_carries_a_gate_label_of_none() -> None:
    """A series that disappears when nothing is gated reads as an outage, not a configuration."""
    record_tool_call_metrics(_tool(gate_decision=None))

    assert _counter(
        "nanoinfra_tool_calls_total", tool="exec", outcome="ok", gate_decision="none"
    ) == 1


def test_counters_only_go_up() -> None:
    for _ in range(3):
        record_llm_call_metrics(_call())
    first = _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="stop"
    )
    record_llm_call_metrics(_call())

    assert first == 3
    assert _counter(
        "nanoinfra_llm_calls_total", provider="moonshot", model="kimi-k3", outcome="stop"
    ) == 4


# --- the histogram ----------------------------------------------------------------------


def test_a_duration_lands_in_its_bucket_and_the_buckets_are_cumulative() -> None:
    record_llm_call_metrics(_call(duration_ms=400))
    record_llm_call_metrics(_call(duration_ms=3_000))

    text = "\n".join(counter_exposition())
    assert 'nanoinfra_llm_duration_ms_bucket{model="kimi-k3",provider="moonshot",le="500.0"} 1' in text
    # 400 and 3000 both fall at or below 5000, so the cumulative count is 2 by then.
    assert 'nanoinfra_llm_duration_ms_bucket{model="kimi-k3",provider="moonshot",le="5000.0"} 2' in text
    assert 'nanoinfra_llm_duration_ms_count{model="kimi-k3",provider="moonshot"} 2' in text
    assert 'nanoinfra_llm_duration_ms_sum{model="kimi-k3",provider="moonshot"} 3400.0' in text


def test_the_bucket_edges_are_the_ones_written_down() -> None:
    """A bucket edit silently invalidates every quantile already recorded against the old one."""
    assert DURATION_BUCKETS_MS == (
        500.0, 1_000.0, 2_500.0, 5_000.0, 10_000.0, 30_000.0, 60_000.0, 120_000.0, 300_000.0
    )


def test_a_duration_past_the_last_edge_lands_in_the_overflow() -> None:
    record_llm_call_metrics(_call(duration_ms=600_000))

    text = "\n".join(counter_exposition())
    assert 'le="+Inf"} 1' in text
    assert 'le="300000.0"} 0' in text


# --- the exposition ---------------------------------------------------------------------


def test_an_empty_accumulator_exports_nothing_rather_than_a_zero_series() -> None:
    """A counter that has never fired has no label set to report, and inventing one lies."""
    assert counter_exposition() == []


def test_no_series_carries_an_unbounded_label() -> None:
    """Asserted over the whole registry rather than per metric, because the risk is a new one."""
    record_llm_call_metrics(_call(usage=LLMUsage(10, 5, 15, reported_tokens=15)))
    record_tool_call_metrics(_tool(gate_decision="allow"))

    text = "\n".join(counter_exposition())
    for forbidden in ("session_key", "turn_id", "actor", "session_id", "request_id"):
        assert forbidden not in text, forbidden


def test_there_is_no_gate_decisions_counter() -> None:
    """Deliberate, and the module says why: those decisions happen in the executor's process,
    which this one cannot read, and the gate's own log drops whole expired segments so counting
    its records is not monotonic either. The Approvals tab answers that question as a window."""
    record_tool_call_metrics(_tool(gate_decision="allow"))

    text = "\n".join(counter_exposition())
    assert "nanoinfra_gate_decisions_total" not in text


def test_a_label_value_with_a_quote_cannot_break_the_format() -> None:
    record_tool_call_metrics(_tool(tool='ex"ec'))

    text = "\n".join(counter_exposition())
    assert 'tool="ex\\"ec"' in text


# --- the payload the charts read ---------------------------------------------------------


def test_the_browser_payload_carries_the_bucket_edges_with_the_counts() -> None:
    """A client that hardcoded the ladder would mislabel every bar the day it changed."""
    from nanoinfra.llm_usage.counters import counters_payload

    record_llm_call_metrics(_call(duration_ms=400))

    payload = counters_payload()
    histogram = payload["histograms"][0]
    edges = [bucket["le"] for bucket in histogram["buckets"]]

    assert edges[: len(DURATION_BUCKETS_MS)] == list(DURATION_BUCKETS_MS)
    # The overflow, spelled as `null` the way Prometheus spells `+Inf`.
    assert edges[-1] is None


def test_the_payload_buckets_are_per_bucket_and_not_cumulative() -> None:
    """The chart scales each bar against the widest, so a cumulative reading would make every bar
    wider than the last and say nothing about the distribution."""
    from nanoinfra.llm_usage.counters import counters_payload

    record_llm_call_metrics(_call(duration_ms=400))
    record_llm_call_metrics(_call(duration_ms=400))
    record_llm_call_metrics(_call(duration_ms=3_000))

    histogram = counters_payload()["histograms"][0]
    counts = {bucket["le"]: bucket["count"] for bucket in histogram["buckets"]}

    assert counts[500.0] == 2
    assert counts[5_000.0] == 1
    assert counts[1_000.0] == 0
    assert histogram["count"] == 3


def test_a_label_set_becomes_an_object_because_json_cannot_key_by_a_tuple() -> None:
    from nanoinfra.llm_usage.counters import counters_payload

    record_llm_call_metrics(_call())

    row = next(
        row for row in counters_payload()["counters"]
        if row["name"] == "nanoinfra_llm_calls_total"
    )
    assert row["labels"] == {"provider": "moonshot", "model": "kimi-k3", "outcome": "stop"}


def test_an_empty_accumulator_answers_two_empty_lists() -> None:
    """The panel renders nothing from that, rather than an axis with no data on it."""
    from nanoinfra.llm_usage.counters import counters_payload

    assert counters_payload() == {"counters": [], "histograms": []}
