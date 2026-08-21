"""Tests for the shared retry backoff."""

from __future__ import annotations

import random

import pytest

from nanoinfra.utils.backoff import (
    DEFAULT_BASE_DELAY_MS,
    DEFAULT_MAX_DELAY_MS,
    BackoffPolicy,
    next_attempt_at_ms,
)


def test_first_retry_waits_the_base_delay() -> None:
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=60_000, jitter=False)

    assert policy.delay_ms(1) == 1_000


def test_delay_doubles_per_attempt() -> None:
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=60_000, jitter=False)

    assert [policy.delay_ms(n) for n in (1, 2, 3, 4)] == [1_000, 2_000, 4_000, 8_000]


def test_delay_stops_at_the_ceiling() -> None:
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=5_000, jitter=False)

    assert [policy.delay_ms(n) for n in (3, 4, 20)] == [4_000, 5_000, 5_000]


def test_a_drifted_attempt_count_does_not_build_a_huge_integer() -> None:
    """A hand-edited delivery file or a corrupted record must not cost memory."""
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=5_000, jitter=False)

    assert policy.delay_ms(10_000_000) == 5_000


def test_a_missing_attempt_count_is_treated_as_the_first_retry() -> None:
    """Raising here would turn a bookkeeping slip into a lost delivery."""
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=60_000, jitter=False)

    assert policy.delay_ms(0) == 1_000
    assert policy.delay_ms(-5) == 1_000


def test_jitter_spreads_across_the_whole_window() -> None:
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=64_000)
    rng = random.Random(1234)

    samples = [policy.delay_ms(4, rng=rng) for _ in range(200)]

    # Full jitter means [0, ceiling], so a low sample is expected rather than a bug.
    assert min(samples) < 8_000
    assert max(samples) <= 8_000
    assert len(set(samples)) > 100


def test_jitter_never_exceeds_the_ceiling() -> None:
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=3_000)
    rng = random.Random(7)

    assert all(policy.delay_ms(n, rng=rng) <= 3_000 for n in range(1, 40))


def test_zero_base_delay_disables_waiting() -> None:
    """A caller that wants the old immediate-requeue behaviour can still ask for it."""
    policy = BackoffPolicy(base_delay_ms=0, max_delay_ms=0)

    assert policy.delay_ms(9) == 0


@pytest.mark.parametrize(
    ("base", "cap"),
    [(-1, 10), (10, -1), (10_000, 1_000)],
)
def test_a_nonsense_policy_is_rejected_at_construction(base: int, cap: int) -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(base_delay_ms=base, max_delay_ms=cap)


def test_next_attempt_at_ms_is_relative_to_now() -> None:
    policy = BackoffPolicy(base_delay_ms=1_000, max_delay_ms=60_000, jitter=False)

    assert next_attempt_at_ms(now_ms=5_000, attempts=2, policy=policy) == 7_000


def test_defaults_are_sane_for_an_unattended_automation() -> None:
    assert DEFAULT_BASE_DELAY_MS == 2_000
    assert DEFAULT_MAX_DELAY_MS == 300_000
    # The defaults reach the cap in nine attempts, not dozens: 2s doubling to 256s, then capped.
    policy = BackoffPolicy(jitter=False)
    assert policy.delay_ms(8) == 256_000
    assert policy.delay_ms(9) == DEFAULT_MAX_DELAY_MS
