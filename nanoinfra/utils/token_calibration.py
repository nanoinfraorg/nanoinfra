"""Correct our prompt-token estimate against what the provider actually charged.

``estimate_prompt_tokens_chain`` prefers a provider-supplied counter, but no provider we ship
implements one, so every estimate comes from tiktoken -- an OpenAI tokenizer -- including for
Anthropic. The consolidation trigger compares that estimate against
``context_window - max_tokens - 1024``, so a systematic under-estimate means consolidation does not
fire, the real prompt exceeds the window, and the provider rejects the request. Nothing archives,
so the next attempt fails the same way (nanoinfraorg/nanoinfra#153).

A provider response does carry the true prompt size. Comparing it against our estimate for the same
messages gives a per-provider correction factor, learned locally with no extra network call.

Deliberately conservative:

- The factor only ever scales *up*. Over-estimating costs some usable context; under-estimating
  costs a rejected request and a session that cannot recover.
- It is clamped, so one anomalous sample cannot make the agent consolidate constantly.
- It is smoothed, so the factor tracks a real tokenizer difference rather than one long tool result.
"""

from __future__ import annotations

# Never scale below 1.0: a tokenizer that under-counts is the failure we are correcting, and
# trusting an estimate *lower* than observed reality has no upside.
_MIN_FACTOR = 1.0
# A tokenizer mismatch of more than this is not a mismatch, it is a bug somewhere else.
_MAX_FACTOR = 2.0
# Weight of a new sample. Low enough that one outsized turn does not move the factor much.
_SMOOTHING = 0.25

_factors: dict[str, float] = {}


def calibration_key(provider: object, model: str | None) -> str:
    """Factors are per provider *and* model, since tokenizers differ across both."""
    return f"{type(provider).__name__}:{model or ''}"


def record_observation(key: str, estimated: int, observed: int) -> None:
    """Learn from one turn where the provider told us the real prompt size."""
    if estimated <= 0 or observed <= 0:
        return
    sample = observed / estimated
    if sample < _MIN_FACTOR:
        # Our estimate was already at or above reality. Nothing to correct.
        sample = _MIN_FACTOR
    sample = min(sample, _MAX_FACTOR)
    current = _factors.get(key, _MIN_FACTOR)
    _factors[key] = current + (sample - current) * _SMOOTHING


def factor(key: str) -> float:
    """The learned correction, or 1.0 before anything has been observed."""
    return _factors.get(key, _MIN_FACTOR)


def corrected(key: str, estimated: int) -> int:
    """Apply the learned correction to one estimate."""
    if estimated <= 0:
        return estimated
    return int(estimated * factor(key))


def reset() -> None:
    """Forget every learned factor. For tests."""
    _factors.clear()
