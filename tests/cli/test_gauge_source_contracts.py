"""The attributes the gateway's gauges read (#235).

`_gauge_sources` in `gateway_runtime.py` is a set of closures over objects the gateway already
built. That is the right shape — a registry somebody has to remember to populate is a registry
that reads `None` on the day it is forgotten — but it moves the risk somewhere a type checker
cannot see it: `getattr(agent, "_last_usage")` type-checks whatever the attribute turns out to be,
and a sampler that swallows every exception turns a renamed attribute into a permanent dash.

That is not hypothetical. The first cut of the context gauge read `_last_usage` as a **dict** when
it is an `LLMUsage` dataclass, so it returned `None` on every sample and looked exactly like a
provider that reports no context figure.

So this file asserts the contract of each source, against the real classes, one test per gauge.
"""

from __future__ import annotations

import asyncio
import inspect

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.bus.queue import MessageBus
from nanoinfra.channels.websocket.runtime import WebSocketChannel
from nanoinfra.config.schema import Config
from nanoinfra.gates.approval_delivery import ApprovalDeliveryWatcher
from nanoinfra.providers.base import LLMUsage


def test_the_loop_records_the_last_usage_the_context_gauge_reads() -> None:
    assert "_last_usage" in inspect.getsource(AgentLoop.__init__)


def test_the_last_usage_is_a_dataclass_with_a_context_field_not_a_dict() -> None:
    """The exact shape the first cut of this gauge got wrong.

    An `LLMUsage` read as a mapping yields nothing, and a sampler that returns `None` on anything
    unexpected reports that as "the provider did not say", which is a different fact.
    """
    assert "context_tokens" in LLMUsage.__dataclass_fields__
    usage = LLMUsage(10, 5, 15, reported_tokens=15, context_tokens=1_234)
    assert usage.context_tokens == 1_234
    assert not isinstance(usage, dict)


def test_a_provider_that_reports_no_context_leaves_the_field_none() -> None:
    """Which is why the gauge reads `None` there rather than zero."""
    assert LLMUsage(10, 5, 15, reported_tokens=15).context_tokens is None


def test_the_active_preset_carries_the_context_limit() -> None:
    assert Config().resolve_preset().context_window_tokens > 0


def test_the_websocket_channel_holds_the_connection_set() -> None:
    assert "_webui_connections" in inspect.getsource(WebSocketChannel)


def test_the_bus_exposes_both_queues_with_a_size() -> None:
    async def _check() -> None:
        bus = MessageBus()
        assert bus.inbound.qsize() == 0
        assert bus.outbound.qsize() == 0

    asyncio.run(_check())


def test_the_approval_watcher_starts_with_an_unknown_count_rather_than_zero() -> None:
    """`None` until the watcher has read the executor once.

    Zero would say "nothing is pending" before anything had been asked, which is the one lie this
    gauge exists to avoid.
    """
    source = inspect.getsource(ApprovalDeliveryWatcher)
    assert "self.pending_count: int | None = None" in source
    # And it goes back to unknown when the executor cannot be reached, rather than to zero.
    assert "self.pending_count = None" in source
