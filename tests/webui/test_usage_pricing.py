"""Tokens become money, and the cache split is why it has to be four rates (#235).

The page answered "how much" and never "how much money", because nothing in the package knew what
a token was worth. This is that, and the reason it is not one rate per model: a cached read costs a
fraction of a fresh prompt token, and the store has carried `cache_read_tokens` on every row since
the table existed. Multiplying `total_tokens` by a single rate over-bills a deployment with a warm
cache -- which is every deployment that works well.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.config.schema import Config
from nanoinfra.webui.settings_api import priced_usage_payload


def _payload(**over: Any) -> dict[str, Any]:
    # `prompt_tokens` is the **logical** input and includes the cached halves -- rule 1 of the
    # `LLMUsage` contract, and what both provider families actually report. So 5.5M of prompt is
    # 1M fresh, 4M read from cache and 0.5M written to it.
    row = {
        "provider": "moonshot",
        "model": "kimi-k3",
        "prompt_tokens": 5_500_000,
        "completion_tokens": 200_000,
        "cached_tokens": 4_000_000,
        "cache_write_tokens": 500_000,
        "total_tokens": 5_700_000,
        "requests": 10,
    }
    row.update(over)
    return {"providers_30d": [row], "window_days": 30}


def _config(pricing: dict[str, Any]) -> Config:
    return Config.model_validate({"pricing": pricing})


def test_each_class_of_token_is_priced_at_its_own_rate() -> None:
    """1M fresh in at $0.60, 200k out at $2.50, 4M cached at $0.15, 500k cache-writes at $0.75."""
    config = _config({
        "moonshot/kimi-k3": {
            "inputPerMTok": 0.60,
            "outputPerMTok": 2.50,
            "cacheReadPerMTok": 0.15,
            "cacheWritePerMTok": 0.75,
        }
    })

    priced = priced_usage_payload(_payload(), config)

    # 0.60 + 0.50 + 0.60 + 0.375
    assert priced["providers_30d"][0]["cost_usd"] == 2.075
    assert priced["cost_usd_window"] == 2.075


def test_one_rate_over_the_total_would_have_been_wrong_by_a_lot() -> None:
    """The reason four rates exist. Same volume, priced as if every token were a prompt token."""
    four_rates = priced_usage_payload(
        _payload(),
        _config({
            "moonshot/kimi-k3": {
                "inputPerMTok": 0.60,
                "outputPerMTok": 2.50,
                "cacheReadPerMTok": 0.15,
                "cacheWritePerMTok": 0.75,
            }
        }),
    )["cost_usd_window"]
    naive = 5_700_000 * 0.60 / 1_000_000

    assert four_rates == 2.075
    assert round(naive, 3) == 3.42
    # A 65% over-bill on a deployment whose cache is working, which is the well-tuned case.
    assert naive > four_rates * 1.6


def test_an_unpriced_model_says_it_does_not_know_rather_than_zero() -> None:
    """`0.00` is a price. A month of real spend shown as free is worse than a blank."""
    priced = priced_usage_payload(_payload(), _config({}))

    assert priced["providers_30d"][0]["cost_usd"] is None
    assert priced["cost_usd_window"] is None
    assert priced["priced_models"] == 0


def test_a_partly_priced_deployment_totals_what_it_knows_and_marks_the_rest() -> None:
    """Pricing the primary and not the fallback is the normal state, not an error."""
    payload = _payload()
    payload["providers_30d"].append({
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "prompt_tokens": 1_000_000,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 1_000_000,
        "requests": 3,
    })

    priced = priced_usage_payload(payload, _config({"moonshot/kimi-k3": {"inputPerMTok": 1.0}}))

    rows = priced["providers_30d"]
    assert rows[0]["cost_usd"] == 1.0
    assert rows[1]["cost_usd"] is None
    assert priced["cost_usd_window"] == 1.0
    assert priced["priced_models"] == 1


def test_pricing_is_keyed_by_the_row_rather_than_by_preset() -> None:
    """`llm_calls` records the provider and model that *answered*, and a fallback answers under
    its own name. Keying by preset would leave every fallback call unpriced, and a fallback is
    exactly the call somebody wants the cost of."""
    priced = priced_usage_payload(
        _payload(provider="deepseek", model="deepseek-v4-pro"),
        _config({"deepseek/deepseek-v4-pro": {"inputPerMTok": 1.0}}),
    )

    assert priced["providers_30d"][0]["cost_usd"] == 1.0


def test_a_missing_rate_is_zero_rather_than_a_refusal() -> None:
    """A deployment that knows its input price and not its cache price should see the input cost,
    not a page that will not render."""
    priced = priced_usage_payload(_payload(), _config({"moonshot/kimi-k3": {"inputPerMTok": 1.0}}))

    assert priced["providers_30d"][0]["cost_usd"] == 1.0


def test_the_rest_of_the_payload_travels_untouched() -> None:
    """Pricing annotates; it does not rebuild. Everything the store answered stays answered."""
    priced = priced_usage_payload({**_payload(), "total_tokens": 999, "failures": []}, _config({}))

    assert priced["total_tokens"] == 999
    assert priced["window_days"] == 30
    assert priced["failures"] == []


# --- the key a reader actually types ----------------------------------------------------


def test_the_documented_camel_case_key_actually_prices() -> None:
    """The spelling in `docs/configuration.md`, asserted against the schema.

    This failed once. The only accepted alias was `inputPerMTok` while the documentation said
    `inputPerMtok`, and pydantic alias matching is case-sensitive -- so pasting the documented
    block left every rate at its `0.0` default, and the page reported a confident `$0.00` over a
    month of real spend with "1 of 1 models priced" beside it. A config example that silently does
    nothing is worse than no example.
    """
    config = Config.model_validate(
        {
            "pricing": {
                "openai/gpt-4o": {
                    "inputPerMtok": 2.5,
                    "outputPerMtok": 10.0,
                    "cacheReadPerMtok": 1.25,
                    "cacheWritePerMtok": 3.125,
                }
            }
        }
    )

    price = config.pricing["openai/gpt-4o"]
    assert price.input_per_mtok == 2.5
    assert price.output_per_mtok == 10.0
    assert price.cache_read_per_mtok == 1.25
    assert price.cache_write_per_mtok == 3.125


def test_the_older_spelling_keeps_working() -> None:
    """It was the only one for a while, and a config that used it must not start pricing at zero."""
    config = Config.model_validate({"pricing": {"openai/gpt-4o": {"inputPerMTok": 2.5}}})

    assert config.pricing["openai/gpt-4o"].input_per_mtok == 2.5


def test_snake_case_works_too_like_every_other_key_in_the_file() -> None:
    config = Config.model_validate({"pricing": {"openai/gpt-4o": {"input_per_mtok": 2.5}}})

    assert config.pricing["openai/gpt-4o"].input_per_mtok == 2.5


def test_a_config_round_trips_through_the_house_spelling() -> None:
    """What `save_config` writes must be what `load_config` reads, or a UI edit is lost."""
    config = Config.model_validate({"pricing": {"openai/gpt-4o": {"inputPerMtok": 2.5}}})

    dumped = config.model_dump(by_alias=True, mode="json")
    assert dumped["pricing"]["openai/gpt-4o"]["inputPerMtok"] == 2.5
    assert Config.model_validate(dumped).pricing["openai/gpt-4o"].input_per_mtok == 2.5


# --- an entry that states nothing -------------------------------------------------------


def test_an_entry_with_a_mistyped_rate_key_reads_as_unpriced() -> None:
    """The failure this guards is the expensive one: a typo that reports spend as free.

    A rate key nobody recognises leaves an entry whose four rates are all at their default. The
    entry exists, so a naive reader calls the model priced and multiplies by zero.
    """
    payload = _payload()
    config = _config({"moonshot/kimi-k3": {"inputPerMillionTokens": 5.0}})

    priced = priced_usage_payload(payload, config)

    assert priced["providers_30d"][0]["cost_usd"] is None
    assert priced["cost_usd_window"] is None
    assert priced["priced_models"] == 0


def test_an_empty_entry_is_not_a_price_of_zero() -> None:
    payload = _payload()
    config = _config({"moonshot/kimi-k3": {}})

    assert priced_usage_payload(payload, config)["cost_usd_window"] is None


def test_an_explicit_zero_is_a_price_and_means_free() -> None:
    """A local model costs nothing, and that is a fact worth stating rather than a gap."""
    payload = _payload()
    config = _config({"moonshot/kimi-k3": {"inputPerMtok": 0.0}})

    priced = priced_usage_payload(payload, config)

    assert priced["providers_30d"][0]["cost_usd"] == 0.0
    assert priced["cost_usd_window"] == 0.0
    assert priced["priced_models"] == 1


# --- the two levels, on the usage page --------------------------------------------------


def test_a_provider_default_prices_a_row_and_says_where_it_came_from() -> None:
    """A cost inherited from a provider is a weaker claim than one typed for that model."""
    config = Config.model_validate(
        {"providers": {"moonshot": {"pricing": {"inputPerMtok": 0.60}}}}
    )

    priced = priced_usage_payload(_payload(), config)

    row = priced["providers_30d"][0]
    assert row["cost_source"] == "provider"
    assert row["cost_usd"] == 0.6
    assert priced["priced_models"] == 1


def test_a_models_own_rates_win_and_the_source_says_so() -> None:
    config = Config.model_validate(
        {
            "providers": {"moonshot": {"pricing": {"inputPerMtok": 99.0}}},
            "pricing": {"moonshot/kimi-k3": {"inputPerMtok": 0.60}},
        }
    )

    row = priced_usage_payload(_payload(), config)["providers_30d"][0]

    assert row["cost_source"] == "model"
    assert row["cost_usd"] == 0.6


def test_an_unpriced_row_carries_no_source() -> None:
    row = priced_usage_payload(_payload(), Config())["providers_30d"][0]

    assert row["cost_usd"] is None
    assert row["cost_source"] is None


def test_a_provider_priced_free_reports_zero_rather_than_a_dash() -> None:
    """Four explicit zeros on a local provider are a price, and the truth."""
    config = Config.model_validate(
        {
            "providers": {
                "ollama": {
                    "pricing": {
                        "inputPerMtok": 0.0,
                        "outputPerMtok": 0.0,
                        "cacheReadPerMtok": 0.0,
                        "cacheWritePerMtok": 0.0,
                    }
                }
            }
        }
    )

    priced = priced_usage_payload(_payload(provider="ollama", model="qwen3"), config)

    assert priced["providers_30d"][0]["cost_usd"] == 0.0
    assert priced["providers_30d"][0]["cost_source"] == "provider"
    assert priced["cost_usd_window"] == 0.0


# --- the buckets must be disjoint -------------------------------------------------------


def test_a_cached_token_is_not_billed_twice() -> None:
    """The bug this guards cost 5.7x on real rates.

    `prompt_tokens` is the logical input and **includes** the cached reads -- rule 1 of the
    `LLMUsage` contract, implemented as `max(prompt, cached)` in the openai-compat backend and as
    `base + cache_creation + cache_read` in the Anthropic one. Charging `prompt_tokens` at the
    input rate *and* `cached_tokens` at the cache rate therefore bills every cached token twice,
    at the expensive rate first.

    Kimi K3's published rates, on a 90%-warm million-token prompt.
    """
    payload = _payload(
        prompt_tokens=1_000_000,
        cached_tokens=900_000,
        cache_write_tokens=0,
        completion_tokens=0,
    )
    config = _config({"moonshot/kimi-k3": {"inputPerMtok": 3.00, "cacheReadPerMtok": 0.30}})

    priced = priced_usage_payload(payload, config)

    # 100k fresh at $3.00/M plus 900k cached at $0.30/M.
    assert priced["providers_30d"][0]["cost_usd"] == 0.57
    # What the double-count produced: 1M at $3.00 plus 900k at $0.30.
    assert priced["providers_30d"][0]["cost_usd"] != 3.27


def test_a_cache_write_is_not_billed_as_fresh_input_as_well() -> None:
    """Anthropic reports `cache_creation_input_tokens` and the backend folds it into the input."""
    payload = _payload(
        prompt_tokens=1_000_000,
        cached_tokens=0,
        cache_write_tokens=400_000,
        completion_tokens=0,
    )
    config = _config({"moonshot/kimi-k3": {"inputPerMtok": 3.00, "cacheWritePerMtok": 3.75}})

    priced = priced_usage_payload(payload, config)

    # 600k fresh at $3.00/M plus 400k written at $3.75/M.
    assert priced["providers_30d"][0]["cost_usd"] == 3.3


def test_a_fully_cached_prompt_costs_only_the_cache_rate() -> None:
    payload = _payload(
        prompt_tokens=1_000_000,
        cached_tokens=1_000_000,
        cache_write_tokens=0,
        completion_tokens=0,
    )
    config = _config({"moonshot/kimi-k3": {"inputPerMtok": 3.00, "cacheReadPerMtok": 0.30}})

    assert priced_usage_payload(payload, config)["providers_30d"][0]["cost_usd"] == 0.3


def test_an_impossible_cache_count_does_not_credit_the_bill() -> None:
    """A provider reporting more cached tokens than its own prompt total is stating nonsense.

    A negative input bucket would subtract money from the invoice, which is the one direction a
    cost figure must never move on bad data.
    """
    payload = _payload(
        prompt_tokens=1_000,
        cached_tokens=9_000_000,
        cache_write_tokens=0,
        completion_tokens=0,
    )
    config = _config({"moonshot/kimi-k3": {"inputPerMtok": 3.00, "cacheReadPerMtok": 0.30}})

    cost = priced_usage_payload(payload, config)["providers_30d"][0]["cost_usd"]
    assert cost is not None
    assert cost > 0
