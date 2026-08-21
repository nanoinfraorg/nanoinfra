"""Retry backoff shared by the two automation subsystems.

Cron and local triggers both record runs through :mod:`nanoinfra.utils.run_records` and, before
this module, disagreed completely about failure: triggers requeued a delivery immediately and cron
did not retry at all. One helper, so the disagreement cannot come back as two implementations.

The delay is exponential with full jitter. Full jitter rather than a fixed fraction because the
callers that matter are many gateways retrying against one host -- a backup target, an API, a
hypervisor -- and equal-spaced retries from several senders re-converge into the same thundering
herd the backoff exists to avoid.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

#: Chosen so a first retry is quick enough to ride out a restart without feeling stuck.
DEFAULT_BASE_DELAY_MS = 2_000
#: An automation that has been failing for five minutes is not going to be fixed by a tighter loop.
DEFAULT_MAX_DELAY_MS = 300_000


@dataclass(frozen=True)
class BackoffPolicy:
    """How long to wait before attempt ``n + 1``."""

    base_delay_ms: int = DEFAULT_BASE_DELAY_MS
    max_delay_ms: int = DEFAULT_MAX_DELAY_MS
    #: Set false in tests that assert an exact schedule. Production always wants jitter.
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.base_delay_ms < 0:
            raise ValueError("base_delay_ms must not be negative")
        if self.max_delay_ms < 0:
            raise ValueError("max_delay_ms must not be negative")
        if self.max_delay_ms < self.base_delay_ms:
            raise ValueError("max_delay_ms must not be below base_delay_ms")

    def delay_ms(self, attempts: int, *, rng: random.Random | None = None) -> int:
        """Delay before the attempt that follows ``attempts`` completed attempts.

        ``attempts`` is the count already made, so the first retry passes ``1`` and waits
        ``base_delay_ms``. A zero or negative count is treated as the first retry rather than
        rejected: a caller that has not recorded an attempt yet still wants a sane delay, and
        raising there would turn a bookkeeping slip into a lost delivery.
        """
        if self.base_delay_ms == 0:
            return 0
        exponent = max(0, attempts - 1)
        # Cap the exponent before shifting. Without this an attempt count that has drifted -- a
        # hand-edited delivery file, a corrupted record -- produces an integer with millions of
        # bits before min() ever sees it.
        if exponent > _MAX_EXPONENT:
            uncapped = self.max_delay_ms
        else:
            uncapped = self.base_delay_ms * (2**exponent)
        ceiling = min(uncapped, self.max_delay_ms)
        if not self.jitter:
            return ceiling
        source = rng or random
        # Full jitter: anywhere in [0, ceiling]. The floor is deliberately 0 rather than
        # base_delay_ms -- spreading retries across the whole window is the property being
        # bought, and a lower bound narrows it for no benefit.
        return source.randint(0, ceiling)


#: 2**62 ms is already past any sane ceiling, so shifting further only costs memory.
_MAX_EXPONENT = 62

DEFAULT_POLICY = BackoffPolicy()


def next_attempt_at_ms(
    *,
    now_ms: int,
    attempts: int,
    policy: BackoffPolicy | None = None,
    rng: random.Random | None = None,
) -> int:
    """Absolute time the next attempt becomes eligible."""
    return now_ms + (policy or DEFAULT_POLICY).delay_ms(attempts, rng=rng)
