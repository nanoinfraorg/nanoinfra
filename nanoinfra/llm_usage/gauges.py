"""The numbers rows cannot answer: point-in-time runtime state (#235, phase 2).

`llm_calls` and `tool_calls` say what *happened*. They cannot say how many sockets are open right
now, or how many suspended actions are waiting for a person — and those are the numbers an
operator watches rather than reviews.

**One sampler, two consumers.** The `Live` panel in the WebUI and the `/metrics` endpoint both
read this, so the page and a Prometheus scrape cannot disagree. That is the same rule the Metrics
page states about a number appearing twice, applied to the number's source rather than its
display.

Every value here is already held by the gateway process. Nothing is stored, nothing is polled from
a database, and a sample that cannot be taken reports `None` rather than zero: "no executor is
reachable" and "no approvals are pending" are different facts, and a dashboard that renders them
identically is a dashboard that lies on the day it matters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from nanoinfra.llm_usage.counters import counter_exposition


@dataclass(frozen=True, slots=True)
class Gauge:
    """One sampled value, with the words a reader needs and the name a scraper needs."""

    name: str
    value: int | None
    label: str
    help: str
    #: True when a non-zero value is something a person should look at rather than a health sign.
    alerting: bool = False


@dataclass(slots=True)
class GaugeSources:
    """What to sample, injected rather than imported.

    Each is a zero-argument callable so a gauge costs nothing until something asks for it, and so
    this module imports neither the gateway nor the executor -- both of which import the agent
    tree, and a metrics sampler that closes an import cycle is a metrics sampler nobody can call
    from a route.
    """

    ws_connections: Any = None
    active_sessions: Any = None
    pending_approvals: Any = None
    inbound_queue_depth: Any = None
    outbound_queue_depth: Any = None
    context_tokens_used: Any = None
    context_tokens_limit: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


#: The container the running gateway published, or `None` in a process that is not one.
#:
#: Lower-case because it is reassigned once at startup. An upper-case name would read as a
#: constant, and the type checker enforces that reading.
#:
#: Process-global rather than an attribute, and for a reason rather than convenience: the WebUI's
#: HTTP surface is a **frozen** dataclass, so it cannot be handed a sampler after construction --
#: and it should not be, because the objects a gauge reads are built in an order that surface knows
#: nothing about. One gateway process holds one set of live numbers; this is that set.
#:
#: `None` is a real answer. `nanoinfra webui` pointed at a remote gateway has no bus and no channel
#: manager to sample, and the route says "not available here" rather than rendering seven zeros.
_active_sources: "GaugeSources | None" = None


def set_active_gauge_sources(sources: "GaugeSources | None") -> None:
    """Publish what this process can sample. Called once, by the gateway, at startup."""
    global _active_sources
    _active_sources = sources


def active_gauge_sources() -> "GaugeSources | None":
    """What this process can sample, or `None` when it is not a gateway."""
    return _active_sources


def _sample(source: Any) -> int | None:
    """One value, or `None` when it cannot be read.

    A sampler may not raise. A `/metrics` scrape that 500s because one gauge's source went away
    takes every other gauge with it, and the reason somebody is scraping is usually that something
    is already wrong.
    """
    if source is None:
        return None
    try:
        value = source() if callable(source) else source
    except Exception:
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _resident_bytes() -> int | None:
    """RSS, from `/proc` and with no dependency.

    `resource.getrusage` reports a *maximum* rather than a current value, which is a different
    question: a process that peaked at 2 GB an hour ago and holds 300 MB now is healthy, and the
    maximum says it is not. `/proc/self/statm` is the current one, and its absence -- a
    non-Linux host -- reads as unknown rather than as zero.
    """
    try:
        with open("/proc/self/statm", encoding="ascii") as handle:
            resident_pages = int(handle.read().split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


def _event_loop_lag_ms() -> int | None:
    """How late the loop's last scheduled tick was, or `None` when nothing is measuring.

    Sampled from a value the running loop maintains rather than measured here: measuring it would
    mean awaiting inside a synchronous sampler, and a `/metrics` scrape that yields to the loop it
    is measuring perturbs the number it reports.
    """
    lag = _loop_lag_ms
    return None if lag is None else max(0, int(lag))


#: Milliseconds the loop was last observed running behind, or `None` while nobody measures it.
#: Written by the gateway's own monitor; read here. A plain module value rather than a callable on
#: `GaugeSources`, because it is a property of the process and not of an injected object.
_loop_lag_ms: float | None = None


def set_event_loop_lag_ms(value: float | None) -> None:
    """Publish the loop's observed lag. Called by the gateway's monitor, once per interval."""
    global _loop_lag_ms
    _loop_lag_ms = value


def sample_gauges(sources: GaugeSources) -> list[Gauge]:
    """Every gauge, in the order the Live panel reads top to bottom."""
    return [
        Gauge(
            name="nanoinfra_pending_approvals",
            value=_sample(sources.pending_approvals),
            label="Approvals waiting",
            help="Suspended actions waiting for a person to answer.",
            # The one gauge here that means *somebody is blocked*, rather than a level to watch.
            # nanoinfra's answer to the stuck-session heuristic: a precise, meaningful stall.
            alerting=True,
        ),
        Gauge(
            name="nanoinfra_ws_connections",
            value=_sample(sources.ws_connections),
            label="WebUI sockets",
            help="Open WebSocket connections from the WebUI.",
        ),
        Gauge(
            name="nanoinfra_sessions_active",
            value=_sample(sources.active_sessions),
            label="Active sessions",
            help="Sessions with recorded history in this workspace.",
        ),
        Gauge(
            name="nanoinfra_inbound_queue_depth",
            value=_sample(sources.inbound_queue_depth),
            label="Inbound queue",
            help="Messages waiting for the agent loop. A rising number is the agent falling behind.",
            alerting=True,
        ),
        Gauge(
            name="nanoinfra_outbound_queue_depth",
            value=_sample(sources.outbound_queue_depth),
            label="Outbound queue",
            help="Replies waiting for a channel. A rising number is a channel that is not draining.",
            alerting=True,
        ),
        Gauge(
            name="nanoinfra_context_tokens_used",
            value=_sample(sources.context_tokens_used),
            label="Context used",
            help="Tokens in the last turn's prompt.",
        ),
        # Process health (#274). Not specific to nanoinfra and that is the point: an operator
        # setting a memory request or chasing a stall needs the same two numbers every runtime
        # exposes, and neither has an event to be driven by -- there is no diagnostic for "this
        # process is 300 MB". Sampled at read, like everything else here.
        Gauge(
            name="nanoinfra_rss_bytes",
            value=_sample(_resident_bytes),
            label="Resident memory",
            help="Resident set size of the gateway process.",
        ),
        Gauge(
            name="nanoinfra_event_loop_lag_ms",
            value=_sample(_event_loop_lag_ms),
            label="Event loop lag",
            help="How far behind the asyncio loop is running. A rising number is a blocked loop.",
            alerting=True,
        ),
        Gauge(
            name="nanoinfra_context_tokens_limit",
            value=_sample(sources.context_tokens_limit),
            label="Context limit",
            help="The active preset's context window.",
        ),
    ]


def gauges_payload(sources: GaugeSources) -> dict[str, Any]:
    """The Live panel's shape: one row per gauge, with the words already resolved."""
    rows = sample_gauges(sources)
    return {
        "gauges": [
            {
                "name": gauge.name,
                "value": gauge.value,
                "label": gauge.label,
                "help": gauge.help,
                "alerting": gauge.alerting,
            }
            for gauge in rows
        ],
    }


def prometheus_exposition(sources: GaugeSources) -> str:
    """The text format, from the same sample the panel reads.

    Gauges, plus the counters and the histogram `counters.py` accumulates (#274). A Prometheus
    **counter must be monotonic**, and a count queried from a table with a purge is not: it falls
    when the pruner runs, and a `rate()` over a falling counter is nonsense. So the counters live
    in memory for the life of the process, which is what Prometheus expects and handles with
    `resets()`. `gate_decisions_total` is still absent, and `counters.py` says why: those
    decisions happen in the executor, whose process this one cannot read.

    No `session_key`, `turn_id` or `actor` labels either, at any point. They are unbounded, and a
    label per session is how a Prometheus install falls over. That detail is the Calls table's job.
    """
    lines: list[str] = []
    for gauge in sample_gauges(sources):
        if gauge.value is None:
            # An unreadable gauge is omitted rather than exported as zero. Prometheus treats an
            # absent series as absent and a zero as a measurement.
            continue
        lines.append(f"# HELP {gauge.name} {gauge.help}")
        lines.append(f"# TYPE {gauge.name} gauge")
        lines.append(f"{gauge.name} {gauge.value}")
    # The counters and the histogram (#274), from the same function, so one place owns the format.
    # They are process-lifetime accumulators rather than queries: a count over a table with a
    # purge is not monotonic, and a `rate()` over a falling counter is nonsense.
    lines.extend(counter_exposition())
    return "\n".join(lines) + ("\n" if lines else "")


__all__ = [
    "Gauge",
    "GaugeSources",
    "active_gauge_sources",
    "set_active_gauge_sources",
    "set_event_loop_lag_ms",
    "gauges_payload",
    "prometheus_exposition",
    "sample_gauges",
]
