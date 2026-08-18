"""Our prompt estimate is corrected against what the provider actually charged.

No provider we ship implements a token counter, so every estimate comes from tiktoken -- an OpenAI
tokenizer -- including for Anthropic. The consolidation trigger compares that against a real context
window, so a systematic under-estimate means consolidation never fires and the provider rejects the
request instead. See nanoinfraorg/nanoinfra#153.
"""

from __future__ import annotations

import pytest

from nanoinfra.utils import token_calibration as tc


@pytest.fixture(autouse=True)
def _clean() -> None:
    tc.reset()


def test_no_observation_means_no_correction() -> None:
    assert tc.factor("k") == 1.0
    assert tc.corrected("k", 1000) == 1000


def test_an_under_estimate_scales_the_estimate_up() -> None:
    """The failure being corrected: we guessed low and the provider charged more."""
    for _ in range(40):
        tc.record_observation("k", estimated=1000, observed=1300)

    assert tc.factor("k") == pytest.approx(1.3, abs=0.02)
    assert tc.corrected("k", 1000) == pytest.approx(1300, abs=20)


def test_an_over_estimate_never_scales_down() -> None:
    """Over-estimating costs context; under-estimating costs a rejected request."""
    for _ in range(40):
        tc.record_observation("k", estimated=2000, observed=1000)

    assert tc.factor("k") == 1.0
    assert tc.corrected("k", 2000) == 2000


def test_an_absurd_sample_is_clamped() -> None:
    """One anomalous turn must not make the agent consolidate constantly."""
    for _ in range(40):
        tc.record_observation("k", estimated=100, observed=100_000)

    assert tc.factor("k") <= 2.0


def test_one_outlier_barely_moves_the_factor() -> None:
    """Smoothing: the factor tracks a tokenizer difference, not one long tool result."""
    tc.record_observation("k", estimated=1000, observed=2000)

    assert tc.factor("k") < 1.3


def test_factors_are_per_provider_and_model() -> None:
    for _ in range(40):
        tc.record_observation("A:m1", estimated=1000, observed=1400)

    assert tc.factor("A:m1") > 1.2
    assert tc.factor("A:m2") == 1.0
    assert tc.factor("B:m1") == 1.0


@pytest.mark.parametrize(
    ("estimated", "observed"),
    [(0, 100), (100, 0), (-5, 100), (100, -5)],
)
def test_useless_samples_are_ignored(estimated: int, observed: int) -> None:
    tc.record_observation("k", estimated=estimated, observed=observed)

    assert tc.factor("k") == 1.0


def test_a_zero_estimate_is_returned_unchanged() -> None:
    for _ in range(10):
        tc.record_observation("k", estimated=100, observed=150)

    assert tc.corrected("k", 0) == 0


def test_the_key_separates_provider_types() -> None:
    class A:
        pass

    class B:
        pass

    assert tc.calibration_key(A(), "m") != tc.calibration_key(B(), "m")
    assert tc.calibration_key(A(), "m1") != tc.calibration_key(A(), "m2")
    assert tc.calibration_key(A(), None) == tc.calibration_key(A(), None)
