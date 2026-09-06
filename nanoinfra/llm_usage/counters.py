"""Rates and tails: the counters and the histogram a level cannot answer (#274).

`gauges.py` reports what is true right now. A Prometheus install also wants what is true *over
time* -- `rate(...)` and `histogram_quantile(...)` -- and neither works on a gauge.

**Why these are accumulated in memory rather than queried.** A Prometheus counter must be
monotonic. `llm_calls` keeps 400 days and `tool_calls` 180, both with a purge, so a `SELECT
count(*)` over either one *falls* when the pruner runs and a `rate()` over a falling counter is
nonsense. A process-lifetime accumulator is monotonic between restarts, which is exactly what
Prometheus expects of a counter and handles with `resets()`.

An earlier proposal of ours called that accumulator hypothetical. It is not: SICLAW runs one, and
this module is the same shape adapted to a tree with no federation -- business code emits, one
module owns the numbers.

**What is deliberately absent.** There is no `gate_decisions_total`, and it is the metric an
operator would most want. Gate decisions happen in the **executor** process, which has no TCP
listener by design, so a counter incremented there is invisible to the gateway that serves
`/metrics`; and the gate's own audit log drops whole expired segments, so counting its records is
not monotonic either. The question that counter would answer is answered instead by the Approvals
tab, over that log, as a window rather than a rate.

No unbounded label reaches this module. `session_key`, `turn_id` and `actor` are per-turn values
and a label per turn is how a Prometheus install falls over; the Calls table answers per-call
questions.

Every function here swallows its own failure. Telemetry that can break a turn is worse than
telemetry that is missing, which is the rule `record_llm_call` already follows.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanoinfra.llm_usage.models import LLMCallRecord, ToolCallRecord

#: Milliseconds. The same ladder SICLAW uses, and it is fixed on purpose: editing a bucket edge
#: silently invalidates every quantile already recorded against the old one.
DURATION_BUCKETS_MS: tuple[float, ...] = (
    500.0,
    1_000.0,
    2_500.0,
    5_000.0,
    10_000.0,
    30_000.0,
    60_000.0,
    120_000.0,
    300_000.0,
)

#: One label set to one count. The key is sorted so two callers spelling labels in a different
#: order cannot create two series for one thing.
_Labels = tuple[tuple[str, str], ...]

_lock = threading.Lock()
_counters: dict[tuple[str, _Labels], int] = {}
_histograms: dict[tuple[str, _Labels], list[int]] = {}
_histogram_sums: dict[tuple[str, _Labels], float] = {}


def _key(labels: dict[str, str] | None) -> _Labels:
    return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))


def increment(name: str, *, labels: dict[str, str] | None = None, by: int = 1) -> None:
    """Add to a counter. Never raises."""
    if by <= 0:
        return
    try:
        with _lock:
            entry = (name, _key(labels))
            _counters[entry] = _counters.get(entry, 0) + int(by)
    except Exception:
        return


def observe(name: str, value: float, *, labels: dict[str, str] | None = None) -> None:
    """Record one duration into the histogram's buckets. Never raises."""
    try:
        with _lock:
            entry = (name, _key(labels))
            buckets = _histograms.get(entry)
            if buckets is None:
                # One slot per bucket edge plus the +Inf overflow.
                buckets = [0] * (len(DURATION_BUCKETS_MS) + 1)
                _histograms[entry] = buckets
            index = next(
                (i for i, edge in enumerate(DURATION_BUCKETS_MS) if value <= edge),
                len(DURATION_BUCKETS_MS),
            )
            buckets[index] += 1
            _histogram_sums[entry] = _histogram_sums.get(entry, 0.0) + float(value)
    except Exception:
        return


def record_llm_call_metrics(call: "LLMCallRecord") -> None:
    """One provider attempt: a call, its tokens, and its wall clock.

    Called from the same place the row is written, so the counter and the table are derived from
    one event rather than from each other. They will not be equal -- a counter resets on restart
    and the table survives it -- and that is the only difference between them that should exist.
    """
    try:
        provider = str(getattr(call, "provider", "") or "unknown")
        model = str(getattr(call, "model", "") or "unknown")
        finish = str(getattr(call, "finish_reason", "") or "unknown")
        base = {"provider": provider, "model": model}
        increment("nanoinfra_llm_calls_total", labels={**base, "outcome": finish})

        usage = getattr(call, "usage", None)
        for kind, attribute in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("cache_read", "cache_read_tokens"),
            ("cache_write", "cache_write_tokens"),
        ):
            value = getattr(usage, attribute, None) if usage is not None else None
            if isinstance(value, int) and value > 0:
                increment(
                    "nanoinfra_llm_tokens_total", labels={**base, "kind": kind}, by=value
                )

        duration = getattr(call, "duration_ms", None)
        if isinstance(duration, (int, float)) and duration >= 0:
            observe("nanoinfra_llm_duration_ms", float(duration), labels=base)
    except Exception:
        return


def record_tool_call_metrics(call: "ToolCallRecord") -> None:
    """One tool call, by tool and outcome, and by what the gate said about it.

    `gate_decision` is `None` on a deployment with no gate configured, and the label carries
    `none` there rather than being dropped: a series that disappears when nothing is gated reads
    as an outage rather than as a configuration.
    """
    try:
        increment(
            "nanoinfra_tool_calls_total",
            labels={
                "tool": str(getattr(call, "tool", "") or "unknown"),
                "outcome": str(getattr(call, "outcome", "") or "unknown"),
                "gate_decision": str(getattr(call, "gate_decision", None) or "none"),
            },
        )
    except Exception:
        return


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _render_labels(labels: _Labels, extra: tuple[tuple[str, str], ...] = ()) -> str:
    pairs = [*labels, *extra]
    if not pairs:
        return ""
    return "{" + ",".join(f'{name}="{_escape(value)}"' for name, value in pairs) + "}"


def counter_exposition() -> list[str]:
    """The counters and the histogram in Prometheus text format.

    Emitted from the same function `gauges.py` renders into, so one place owns the format. An
    empty accumulator produces nothing at all rather than a zero series: a counter that has never
    been incremented has no label set to report, and inventing one would claim a series that does
    not exist.
    """
    lines: list[str] = []
    with _lock:
        counters = dict(_counters)
        histograms = {name: list(buckets) for name, buckets in _histograms.items()}
        sums = dict(_histogram_sums)

    by_name: dict[str, list[tuple[_Labels, int]]] = {}
    for (name, labels), value in counters.items():
        by_name.setdefault(name, []).append((labels, value))
    for name in sorted(by_name):
        lines.append(f"# TYPE {name} counter")
        for labels, value in sorted(by_name[name]):
            lines.append(f"{name}{_render_labels(labels)} {value}")

    hist_by_name: dict[str, list[tuple[_Labels, list[int]]]] = {}
    for (name, labels), buckets in histograms.items():
        hist_by_name.setdefault(name, []).append((labels, buckets))
    for name in sorted(hist_by_name):
        lines.append(f"# TYPE {name} histogram")
        for labels, buckets in sorted(hist_by_name[name]):
            running = 0
            for edge, count in zip(DURATION_BUCKETS_MS, buckets, strict=False):
                running += count
                lines.append(
                    f"{name}_bucket{_render_labels(labels, (('le', str(edge)),))} {running}"
                )
            running += buckets[-1]
            lines.append(f"{name}_bucket{_render_labels(labels, (('le', '+Inf'),))} {running}")
            lines.append(f"{name}_sum{_render_labels(labels)} {sums.get((name, labels), 0.0)}")
            lines.append(f"{name}_count{_render_labels(labels)} {running}")
    return lines


def snapshot() -> dict[str, Any]:
    """Every accumulated value, for a test or a reader that is not Prometheus."""
    with _lock:
        return {
            "counters": {
                (name, labels): value for (name, labels), value in _counters.items()
            },
            "histograms": {
                (name, labels): list(buckets)
                for (name, labels), buckets in _histograms.items()
            },
        }


def reset_metrics() -> None:
    """Drop everything. For tests, and for nothing else -- a counter a process clears mid-life
    is a counter whose `rate()` lies."""
    with _lock:
        _counters.clear()
        _histograms.clear()
        _histogram_sums.clear()


__all__ = [
    "DURATION_BUCKETS_MS",
    "counter_exposition",
    "increment",
    "observe",
    "record_llm_call_metrics",
    "record_tool_call_metrics",
    "reset_metrics",
    "snapshot",
]
