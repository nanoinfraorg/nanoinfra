"""What a token is worth, and which answer that came from (#235).

Two levels, and the order between them is the whole module:

1. **The model's own rates**, keyed the way the usage rows are keyed: `"<provider>/<model>"`.
2. **The provider's default**, for the case where every model on a provider costs the same thing —
   which in practice means a local provider, where the same thing is nothing.

A model's own rates always win. Nothing else is inferred: a provider with no default and a model
with no entry is **unpriced**, which the page renders as a dash and never as `$0.00`.

The `source` half of the answer matters as much as the rates. A cost inherited from a provider
default is a weaker claim than one somebody typed for that model, and a reader comparing a figure
against an invoice needs to know which of the two they are looking at.
"""

from __future__ import annotations

from typing import Literal

from nanoinfra.config.schema import Config, ModelPricing

#: Where a rate came from. `None` is a real answer and means nothing priced this model.
PricingSource = Literal["model", "provider"] | None


def pricing_key(provider: str, model: str) -> str:
    """The key `pricing` is keyed by, in one place so a caller cannot spell it differently."""
    return f"{provider}/{model}"


def resolve_pricing(
    config: Config,
    provider: str,
    model: str,
) -> tuple[ModelPricing | None, PricingSource]:
    """The rates that apply to one model, and where they came from.

    `is_stated()` rather than a truthiness check on the rates, because **an explicit zero is a
    price** and means free. The distinction is not academic: it is the difference between an Ollama
    model reading `$0.00` and reading `—`, and only one of those is true.
    """
    own = config.pricing.get(pricing_key(provider, model))
    if own is not None and own.is_stated():
        return own, "model"

    provider_config = getattr(config.providers, provider, None)
    default = getattr(provider_config, "pricing", None) if provider_config is not None else None
    if isinstance(default, ModelPricing) and default.is_stated():
        return default, "provider"

    return None, None


def uncached_input_tokens(
    *,
    prompt_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
) -> int:
    """The prompt tokens billed at the full input rate.

    **`prompt_tokens` includes the cached ones.** That is rule 1 of the `LLMUsage` contract -- "the
    logical input includes cache reads and writes" -- and both provider families implement it:
    `openai_compat` takes `max(prompt, cached)` because OpenAI and DeepSeek already fold cached
    reads into `prompt_tokens`, and the Anthropic backend adds `cache_creation` and `cache_read`
    to `input_tokens` by hand.

    So the three buckets have to be *disjoint* before they meet three different rates. Charging
    `prompt_tokens` at the input rate **and** `cached_tokens` at the cache rate bills every cached
    token twice, at the expensive rate first. On Kimi K3 ($3.00 input, $0.30 cache hit) a 90%-warm
    million-token prompt is $0.57 done this way and $3.27 done the other -- a 5.7x over-bill, and
    the exact failure the four-rate design exists to prevent.

    Clamped at zero: a provider that reports a cache count larger than its own prompt total is
    stating something impossible, and a negative bucket would credit the bill.
    """
    return max(0, prompt_tokens - cached_tokens - cache_write_tokens)


def cost_usd(
    price: ModelPricing,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int,
    cache_write_tokens: int,
) -> float:
    """USD for one bucket of tokens, at four rates over three disjoint input buckets.

    Four rates and not one because the cheap tokens are most of the volume: multiplying a total by
    a single blended rate over-bills a deployment with a warm cache by more than half.

    See `uncached_input_tokens` for why the input bucket is a subtraction rather than
    `prompt_tokens`.
    """
    uncached = uncached_input_tokens(
        prompt_tokens=prompt_tokens,
        cached_tokens=cached_tokens,
        cache_write_tokens=cache_write_tokens,
    )
    return (
        uncached * price.input_per_mtok
        + completion_tokens * price.output_per_mtok
        + cached_tokens * price.cache_read_per_mtok
        + cache_write_tokens * price.cache_write_per_mtok
    ) / 1_000_000


__all__ = [
    "PricingSource",
    "cost_usd",
    "pricing_key",
    "resolve_pricing",
    "uncached_input_tokens",
]
