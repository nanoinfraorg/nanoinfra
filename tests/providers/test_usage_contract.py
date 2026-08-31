"""The four invariants, and the two things the old dict could not say (#172).

Each test here is a habit the `dict[str, int]` relied on. The point is not that the arithmetic is
hard -- it is that every one of these was previously enforced by every caller remembering, and the
two `None`-versus-zero cases were not enforced at all, because an absent key reads as zero.
"""

from __future__ import annotations

import pytest

from nanoinfra.providers.base import LLMUsage

# --- the invariants ---------------------------------------------------------------------


def test_the_total_keeps_a_larger_provider_number() -> None:
    """Hidden reasoning and server-side tools are tokens somebody paid for."""
    usage = LLMUsage.reported(input_tokens=100, output_tokens=20, total_tokens=500)

    assert usage.total_tokens == 500
    assert usage.reported_tokens == 500


def test_the_total_is_never_below_what_is_visible() -> None:
    usage = LLMUsage.reported(input_tokens=100, output_tokens=20, total_tokens=7)

    assert usage.total_tokens == 120


def test_a_total_below_the_visible_sum_is_refused() -> None:
    with pytest.raises(ValueError, match="at least"):
        LLMUsage(input_tokens=100, output_tokens=20, total_tokens=50, reported_tokens=50)


def test_the_partition_must_exhaust_the_total() -> None:
    """The invariant the old dict could not state: a number's origin was a key that happened
    to be present, set with `setdefault`."""
    with pytest.raises(ValueError, match="must equal total_tokens"):
        LLMUsage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            reported_tokens=10,
            estimated_tokens=1,
        )


def test_cache_counts_are_part_of_the_input_and_cannot_exceed_it() -> None:
    """Rule 1 read backwards: a cache total above the input means the boundary mapped a
    provider's shape wrong, which is worth a refusal rather than a silent figure."""
    with pytest.raises(ValueError, match="cannot exceed"):
        LLMUsage.reported(input_tokens=10, output_tokens=1, cache_read_tokens=11)


def test_a_boolean_is_not_a_token_count() -> None:
    """`bool` is an `int`, so a flag that deserialised into a number would otherwise validate."""
    with pytest.raises(ValueError, match="non-negative integer"):
        LLMUsage(input_tokens=True, output_tokens=0, total_tokens=1, reported_tokens=1)  # pyright: ignore[reportArgumentType]


def test_negative_numbers_are_refused_everywhere() -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        LLMUsage(input_tokens=-1, output_tokens=0, total_tokens=0)
    with pytest.raises(ValueError, match="None or a non-negative integer"):
        LLMUsage(input_tokens=1, output_tokens=0, total_tokens=1, reported_tokens=1, context_tokens=-1)


# --- not reported, versus reported as zero ----------------------------------------------


def test_an_unreported_cache_metric_is_none_and_a_reported_zero_is_zero() -> None:
    unreported = LLMUsage.reported(input_tokens=10, output_tokens=1)
    reported_zero = LLMUsage.reported(input_tokens=10, output_tokens=1, cache_read_tokens=0)

    assert unreported.cache_read_tokens is None
    assert reported_zero.cache_read_tokens == 0
    # And the difference survives the trip to a consumer, which it did not as a dict key.
    assert "cached_tokens" not in unreported.to_turn_dict()
    assert reported_zero.to_turn_dict()["cached_tokens"] == 0


def test_aggregating_a_measured_call_with_an_unmeasured_one_reports_nothing() -> None:
    """A percentage over a denominator that includes unmeasured calls is a made-up percentage."""
    measured = LLMUsage.reported(input_tokens=100, output_tokens=10, cache_read_tokens=90)
    unmeasured = LLMUsage.reported(input_tokens=100, output_tokens=10)

    assert (measured + unmeasured).cache_read_tokens is None
    assert (measured + measured).cache_read_tokens == 180


# --- aggregation -----------------------------------------------------------------------


def test_the_partition_survives_aggregation() -> None:
    total = (
        LLMUsage.reported(input_tokens=100, output_tokens=10)
        + LLMUsage.estimated(input_tokens=50, output_tokens=5)
    )

    assert total.reported_tokens == 110
    assert total.estimated_tokens == 55
    assert total.total_tokens == 165
    assert total.source == "mixed"
    assert total.request_count == 2


def test_context_is_a_level_and_not_a_sum() -> None:
    """Two calls carrying 40k each did not carry 80k, and the compaction decision reads this."""
    first = LLMUsage.reported(input_tokens=40_000, output_tokens=10)
    second = LLMUsage.reported(input_tokens=41_000, output_tokens=10)

    assert (first + second).context_tokens == 41_000


def test_source_names_which_half_a_figure_came_from() -> None:
    assert LLMUsage.reported(input_tokens=1, output_tokens=1).source == "reported"
    assert LLMUsage.estimated(input_tokens=1, output_tokens=1).source == "estimated"
    assert LLMUsage.empty_request().source == "reported"


def test_an_empty_request_is_still_a_request() -> None:
    """A completed call with nothing measurable is not the same as no call, and a store that
    counted it as nothing would report a request rate below the one the provider saw."""
    usage = LLMUsage.empty_request()

    assert usage.request_count == 1
    assert usage.total_tokens == 0


# --- timing ----------------------------------------------------------------------------


def test_timing_only_counts_output_it_actually_timed() -> None:
    usage = LLMUsage.reported(input_tokens=10, output_tokens=200).with_timing(
        generation_ms=1_000, ttft_ms=120
    )

    assert usage.measured_output_tokens == 200
    assert usage.timed_requests == 1
    assert usage.to_turn_dict()["generation_ms"] == 1_000


def test_an_untimed_call_measures_no_output() -> None:
    usage = LLMUsage.reported(input_tokens=10, output_tokens=200).with_timing(
        generation_ms=None, ttft_ms=None
    )

    assert usage.measured_output_tokens == 0
    assert usage.timed_requests == 0
    assert "generation_ms" not in usage.to_turn_dict()
    assert "ttft_ms" not in usage.to_turn_dict()


# --- serialisation ---------------------------------------------------------------------


def test_a_record_round_trips_exactly() -> None:
    usage = LLMUsage.reported(
        input_tokens=100, output_tokens=20, cache_read_tokens=40, cache_write_tokens=10
    ).with_timing(generation_ms=900, ttft_ms=90)

    assert LLMUsage.from_dict(usage.to_dict()) == usage


def test_a_record_that_lost_its_partition_reads_as_nothing() -> None:
    """Strict on purpose: a usage value missing its partition is worse than a missing one."""
    broken = LLMUsage.reported(input_tokens=10, output_tokens=1).to_dict()
    broken["reported_tokens"] = 3

    assert LLMUsage.from_dict(broken) is None


@pytest.mark.parametrize(
    "value",
    [None, [], "12", {"input_tokens": "10"}, {"input_tokens": -1}, {"cache_read_tokens": True}],
)
def test_anything_that_is_not_the_contract_reads_as_nothing(value: object) -> None:
    assert LLMUsage.from_dict(value) is None


def test_the_turn_shape_keeps_the_names_a_client_already_reads() -> None:
    """The compact shape travels to the WebUI, `/status` and the OpenAI-compatible API, so its
    keys stay OpenAI-flavoured even though the type's are not."""
    turn = LLMUsage.reported(input_tokens=100, output_tokens=20).to_turn_dict()

    assert turn["prompt_tokens"] == 100
    assert turn["completion_tokens"] == 20
    assert turn["total_tokens"] == 120
