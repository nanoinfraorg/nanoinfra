"""The gauges, and the two rules they exist to keep (#235, phase 2).

Rule one: **`None` is not zero.** "The source could not be read" and "the value is zero" are
different facts. A sampler that reports the first as the second produces a dashboard that says
nothing is pending on the day the approval watcher is down, which is exactly the day somebody is
blocked.

Rule two: **a sampler may not raise.** A `/metrics` scrape that 500s because one source went away
takes the other six with it, and the reason somebody is scraping is usually that something is
already wrong.

The exposition tests pin what is *absent* as hard as what is present: no counters, because a count
from a table with a purge is not monotonic, and no unbounded labels.
"""

from __future__ import annotations

import pytest

from nanoinfra.llm_usage.gauges import (
    GaugeSources,
    active_gauge_sources,
    gauges_payload,
    prometheus_exposition,
    sample_gauges,
    set_active_gauge_sources,
)

#: Every gauge the sampler offers, in the order the Live panel reads them.
_EXPECTED = [
    "nanoinfra_pending_approvals",
    "nanoinfra_ws_connections",
    "nanoinfra_sessions_active",
    "nanoinfra_inbound_queue_depth",
    "nanoinfra_outbound_queue_depth",
    "nanoinfra_context_tokens_used",
    "nanoinfra_context_tokens_limit",
]


@pytest.fixture(autouse=True)
def _clear_process_holder():
    """The holder is process-global, so a test that sets it must not leak into the next file."""
    yield
    set_active_gauge_sources(None)


# --- what a sample is -------------------------------------------------------------------


def test_every_gauge_is_sampled_and_the_order_is_the_panel_order() -> None:
    rows = sample_gauges(GaugeSources())

    assert [gauge.name for gauge in rows] == _EXPECTED


def test_the_approvals_gauge_comes_first_and_is_the_one_that_alerts() -> None:
    """It is the only gauge here that means *somebody is blocked* rather than a level to watch."""
    rows = sample_gauges(GaugeSources())

    assert rows[0].name == "nanoinfra_pending_approvals"
    assert rows[0].alerting is True


def test_an_unreadable_source_is_none_and_never_zero() -> None:
    rows = {gauge.name: gauge.value for gauge in sample_gauges(GaugeSources())}

    # No source given at all: nothing is known, and nothing is claimed.
    assert rows["nanoinfra_pending_approvals"] is None
    assert 0 not in rows.values()


def test_a_source_that_reads_zero_reports_zero(  ) -> None:
    """The other half of the rule: a real zero must survive, or the distinction is useless."""
    rows = {
        gauge.name: gauge.value
        for gauge in sample_gauges(GaugeSources(pending_approvals=lambda: 0))
    }

    assert rows["nanoinfra_pending_approvals"] == 0


def test_a_source_that_raises_does_not_take_the_others_with_it() -> None:
    def exploding() -> int:
        raise RuntimeError("the executor went away")

    rows = {
        gauge.name: gauge.value
        for gauge in sample_gauges(
            GaugeSources(pending_approvals=exploding, ws_connections=lambda: 4)
        )
    }

    assert rows["nanoinfra_pending_approvals"] is None
    assert rows["nanoinfra_ws_connections"] == 4


def test_a_plain_value_is_accepted_as_well_as_a_callable() -> None:
    rows = {gauge.name: gauge.value for gauge in sample_gauges(GaugeSources(ws_connections=7))}

    assert rows["nanoinfra_ws_connections"] == 7


@pytest.mark.parametrize("junk", [object(), None, [], {}, lambda: object()])
def test_a_value_that_is_not_a_number_reads_as_unknown(junk: object) -> None:
    rows = {gauge.name: gauge.value for gauge in sample_gauges(GaugeSources(ws_connections=junk))}

    assert rows["nanoinfra_ws_connections"] is None


def test_a_float_is_truncated_because_a_gauge_here_counts_things() -> None:
    rows = {gauge.name: gauge.value for gauge in sample_gauges(GaugeSources(ws_connections=3.7))}

    assert rows["nanoinfra_ws_connections"] == 3


# --- the payload the panel reads ---------------------------------------------------------


def test_the_payload_carries_the_words_a_reader_needs() -> None:
    payload = gauges_payload(GaugeSources(pending_approvals=lambda: 2))

    first = payload["gauges"][0]
    assert first["name"] == "nanoinfra_pending_approvals"
    assert first["value"] == 2
    assert first["label"]
    assert first["help"]
    assert first["alerting"] is True


def test_the_payload_lists_every_gauge_even_the_unreadable_ones() -> None:
    """The panel renders a dash. Omitting the row would read as "this gauge does not exist"."""
    payload = gauges_payload(GaugeSources())

    assert [gauge["name"] for gauge in payload["gauges"]] == _EXPECTED
    assert all(gauge["value"] is None for gauge in payload["gauges"])


# --- the exposition ---------------------------------------------------------------------


def test_the_exposition_is_the_text_format_prometheus_expects() -> None:
    text = prometheus_exposition(GaugeSources(ws_connections=lambda: 5))

    assert "# HELP nanoinfra_ws_connections" in text
    assert "# TYPE nanoinfra_ws_connections gauge" in text
    assert "nanoinfra_ws_connections 5" in text
    assert text.endswith("\n")


def test_an_unreadable_gauge_is_omitted_rather_than_exported_as_zero() -> None:
    """Prometheus treats an absent series as absent and a zero as a measurement."""
    text = prometheus_exposition(GaugeSources(ws_connections=lambda: 5))

    assert "nanoinfra_ws_connections 5" in text
    assert "nanoinfra_pending_approvals" not in text


def test_nothing_readable_produces_an_empty_body_and_not_a_broken_one() -> None:
    assert prometheus_exposition(GaugeSources()) == ""


def test_nothing_is_typed_as_a_counter() -> None:
    """A count from a table with a 180-day purge is not monotonic, so it cannot be a counter.

    It falls when the pruner runs, and a `rate()` over a falling counter is nonsense. Counters
    belong to a process-lifetime accumulator, which does not exist yet -- so this endpoint exports
    what it can export honestly.
    """
    text = prometheus_exposition(
        GaugeSources(
            ws_connections=lambda: 1,
            pending_approvals=lambda: 2,
            active_sessions=lambda: 3,
            inbound_queue_depth=lambda: 4,
            outbound_queue_depth=lambda: 5,
            context_tokens_used=lambda: 6,
            context_tokens_limit=lambda: 7,
        )
    )

    assert "counter" not in text
    assert text.count("# TYPE") == len(_EXPECTED)


def test_no_series_carries_a_label_at_all() -> None:
    """`session_key`, `turn_id` and `actor` are unbounded, and a label per session is how a
    Prometheus install falls over. The Calls table answers per-call questions instead."""
    text = prometheus_exposition(
        GaugeSources(ws_connections=lambda: 1, pending_approvals=lambda: 2)
    )

    for line in text.splitlines():
        if line.startswith("#"):
            continue
        assert "{" not in line, line


# --- the process holder -----------------------------------------------------------------


def test_a_process_that_published_nothing_reads_as_none() -> None:
    """`nanoinfra webui` against a remote gateway has no bus and no channels to sample."""
    assert active_gauge_sources() is None


def test_the_gateway_publishes_once_and_the_route_reads_what_it_published() -> None:
    sources = GaugeSources(ws_connections=lambda: 9)

    set_active_gauge_sources(sources)

    assert active_gauge_sources() is sources
    published = active_gauge_sources()
    assert published is not None
    assert gauges_payload(published)["gauges"][1]["value"] == 9
