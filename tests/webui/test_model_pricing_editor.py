"""Rates go where the model is added (#235).

The cost card told the operator to hand-edit `config.json` and type a `"<provider>/<model>"` key
the UI already knows. A rate that only exists in a file nobody opens is a rate nobody sets, and an
unset rate makes the whole Cost column a column of dashes.

Two levels, and the tests are organised around the edits that are easy to get wrong rather than
around the happy path:

* the rates live on the **model**, not on the configuration, so a configuration deleted or renamed
  does not un-price four hundred days of history
* an explicit `0` is a price and means free, so clearing needs its own parameter
* a provider default covers a local fleet in one edit, and a model's own rates always win
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.config.loader import load_config
from nanoinfra.llm_usage.pricing import resolve_pricing
from nanoinfra.webui.settings_api import (
    WebUISettingsError,
    create_model_configuration,
    delete_model_configuration,
    settings_payload,
    update_model_configuration,
    update_provider_settings,
)


def _use_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: object) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", path)
    return path


def _base(**over: Any) -> dict[str, Any]:
    """A deployment with one provider and one configuration naming it."""
    raw: dict[str, Any] = {
        "providers": {"moonshot": {"apiKey": "sk-test"}},
        "modelPresets": {
            "coding": {"label": "Coding", "provider": "moonshot", "model": "kimi-k3"}
        },
        "agents": {"defaults": {"modelPreset": "coding"}},
    }
    raw.update(over)
    return raw


def _query(**params: Any) -> dict[str, list[str]]:
    return {key: [str(value)] for key, value in params.items()}


def _row(name: str) -> dict[str, Any]:
    row = next(
        entry for entry in settings_payload()["model_presets"] if entry["name"] == name
    )
    return row


# --- the four rates, through the routes -------------------------------------------------


def test_a_configuration_can_be_created_with_its_rates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(
        tmp_path,
        monkeypatch,
        {"providers": {"moonshot": {"apiKey": "sk-test"}}, "modelPresets": {}},
    )

    create_model_configuration(
        _query(
            label="Coding",
            provider="moonshot",
            model="kimi-k3",
            inputPerMtok=0.60,
            outputPerMtok=2.50,
            cacheReadPerMtok=0.15,
            cacheWritePerMtok=0.75,
        )
    )

    price, source = resolve_pricing(load_config(), "moonshot", "kimi-k3")
    assert source == "model"
    assert price is not None
    assert (price.input_per_mtok, price.output_per_mtok) == (0.60, 2.50)
    assert (price.cache_read_per_mtok, price.cache_write_per_mtok) == (0.15, 0.75)


def test_rates_are_keyed_by_the_model_and_not_by_the_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reason the map is at the root. `llm_calls` records provider and model, not a preset."""
    _use_config(tmp_path, monkeypatch, _base())

    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    assert "moonshot/kimi-k3" in load_config().pricing


def test_an_update_leaves_the_rates_it_was_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent means unchanged, which is this route's convention for every other field too."""
    _use_config(tmp_path, monkeypatch, _base())
    update_model_configuration(_query(name="coding", inputPerMtok=0.60, outputPerMtok=2.50))

    update_model_configuration(_query(name="coding", outputPerMtok=3.00))

    price = load_config().pricing["moonshot/kimi-k3"]
    assert price.input_per_mtok == 0.60
    assert price.output_per_mtok == 3.00


def test_an_empty_field_is_not_a_rate_of_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blank input must not silently reprice a model to free."""
    _use_config(tmp_path, monkeypatch, _base())
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    update_model_configuration({"name": ["coding"], "inputPerMtok": [""]})

    assert load_config().pricing["moonshot/kimi-k3"].input_per_mtok == 0.60


def test_zero_is_a_price_and_means_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())

    update_model_configuration(_query(name="coding", inputPerMtok=0))

    price, source = resolve_pricing(load_config(), "moonshot", "kimi-k3")
    assert source == "model"
    assert price is not None
    assert price.input_per_mtok == 0.0


def test_clearing_takes_its_own_parameter_because_zero_is_a_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    update_model_configuration(_query(name="coding", clearPricing=1))

    assert "moonshot/kimi-k3" not in load_config().pricing
    assert resolve_pricing(load_config(), "moonshot", "kimi-k3") == (None, None)


# --- validation --------------------------------------------------------------------------


def test_a_negative_rate_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_config(tmp_path, monkeypatch, _base())

    with pytest.raises(WebUISettingsError):
        update_model_configuration(_query(name="coding", inputPerMtok=-1))

    assert "moonshot/kimi-k3" not in load_config().pricing


def test_a_rate_that_could_only_be_a_mistake_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unit is per **million** tokens, so `25000` is somebody pricing per token.

    The bound catches one direction only, which the message says: a per-token or per-1K price is
    just a small number and cannot be told from a real cheap rate. This direction can, so it is
    refused rather than rendered as a $40,000 month.
    """
    _use_config(tmp_path, monkeypatch, _base())

    with pytest.raises(WebUISettingsError, match="per million tokens"):
        update_model_configuration(_query(name="coding", inputPerMtok=25_000))

    assert "moonshot/kimi-k3" not in load_config().pricing


def test_a_rate_that_is_not_a_number_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())

    with pytest.raises(WebUISettingsError, match="must be a number"):
        update_model_configuration(_query(name="coding", inputPerMtok="free"))


# --- the edits that are easy to get wrong -----------------------------------------------


def test_changing_the_model_carries_the_rates_to_the_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retyping them is the annoyance this whole editor exists to remove."""
    _use_config(tmp_path, monkeypatch, _base())
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    update_model_configuration(_query(name="coding", model="kimi-k4"))

    config = load_config()
    assert config.pricing["moonshot/kimi-k4"].input_per_mtok == 0.60


def test_changing_the_model_leaves_the_old_entry_where_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`llm_calls` keeps 400 days keyed by provider and model.

    Deleting the old entry would silently un-price rows that are still on the Usage page, which is
    a regression in a figure somebody already read.
    """
    _use_config(tmp_path, monkeypatch, _base())
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    update_model_configuration(_query(name="coding", model="kimi-k4"))

    assert load_config().pricing["moonshot/kimi-k3"].input_per_mtok == 0.60


def test_the_rates_do_not_move_when_another_configuration_still_names_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moving them would claim a price that belongs to a configuration that did not change."""
    _use_config(
        tmp_path,
        monkeypatch,
        _base(
            modelPresets={
                "coding": {"label": "Coding", "provider": "moonshot", "model": "kimi-k3"},
                "creative": {"label": "Creative", "provider": "moonshot", "model": "kimi-k3"},
            }
        ),
    )
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    update_model_configuration(_query(name="coding", model="kimi-k4"))

    config = load_config()
    assert "moonshot/kimi-k4" not in config.pricing
    assert config.pricing["moonshot/kimi-k3"].input_per_mtok == 0.60


def test_the_rates_do_not_overwrite_a_price_the_new_model_already_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(
        tmp_path,
        monkeypatch,
        _base(pricing={"moonshot/kimi-k4": {"inputPerMtok": 1.20}}),
    )
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    update_model_configuration(_query(name="coding", model="kimi-k4"))

    assert load_config().pricing["moonshot/kimi-k4"].input_per_mtok == 1.20


def test_deleting_a_configuration_leaves_the_price_that_covers_its_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(
        tmp_path,
        monkeypatch,
        {
            "providers": {"moonshot": {"apiKey": "sk-test"}},
            "modelPresets": {
                "coding": {"label": "Coding", "provider": "moonshot", "model": "kimi-k3"},
                "keeper": {"label": "Keeper", "provider": "moonshot", "model": "kimi-k9"},
            },
            "agents": {"defaults": {"modelPreset": "keeper"}},
        },
    )
    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    delete_model_configuration(_query(name="coding"))

    assert load_config().pricing["moonshot/kimi-k3"].input_per_mtok == 0.60


# --- the provider default ---------------------------------------------------------------


def test_a_provider_default_prices_a_model_with_no_entry_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())

    update_provider_settings(_query(provider="moonshot", inputPerMtok=0.50))

    price, source = resolve_pricing(load_config(), "moonshot", "kimi-k3")
    assert source == "provider"
    assert price is not None
    assert price.input_per_mtok == 0.50


def test_a_models_own_rates_beat_the_provider_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())
    update_provider_settings(_query(provider="moonshot", inputPerMtok=0.50))

    update_model_configuration(_query(name="coding", inputPerMtok=0.60))

    price, source = resolve_pricing(load_config(), "moonshot", "kimi-k3")
    assert source == "model"
    assert price is not None
    assert price.input_per_mtok == 0.60


def test_free_writes_four_explicit_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the provider level.

    A local fleet is a dozen models that all cost nothing, and pricing them one at a time is
    twelve edits to state one fact. The zeros have to be explicit, because an entry that states
    nothing reads as unpriced.
    """
    _use_config(tmp_path, monkeypatch, _base(providers={"ollama": {}}))

    update_provider_settings(_query(provider="ollama", pricingFree=1))

    price, source = resolve_pricing(load_config(), "ollama", "qwen3")
    assert source == "provider"
    assert price is not None
    assert price.input_per_mtok == 0.0
    assert price.is_stated()


def test_clearing_a_provider_default_returns_its_models_to_unpriced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base(providers={"ollama": {}}))
    update_provider_settings(_query(provider="ollama", pricingFree=1))

    update_provider_settings(_query(provider="ollama", clearPricing=1))

    assert load_config().providers.ollama.pricing is None
    assert resolve_pricing(load_config(), "ollama", "qwen3") == (None, None)


def test_a_provider_update_that_says_nothing_about_pricing_leaves_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())
    update_provider_settings(_query(provider="moonshot", inputPerMtok=0.50))

    update_provider_settings(_query(provider="moonshot", apiBase="https://example.invalid"))

    assert load_config().providers.moonshot.pricing is not None


# --- what the panel reads ---------------------------------------------------------------


def test_the_row_carries_the_effective_rates_and_where_they_came_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())
    update_provider_settings(_query(provider="moonshot", inputPerMtok=0.50))

    row = _row("coding")

    assert row["pricing"] == {
        "inputPerMtok": 0.50,
        "outputPerMtok": 0.0,
        "cacheReadPerMtok": 0.0,
        "cacheWritePerMtok": 0.0,
    }
    assert row["pricing_source"] == "provider"


def test_an_unpriced_row_says_so_rather_than_carrying_zeros(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base())

    row = _row("coding")

    assert row["pricing"] is None
    assert row["pricing_source"] is None


def test_the_row_names_the_other_configurations_it_shares_a_bill_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Computed server-side, because only the server has every configuration."""
    _use_config(
        tmp_path,
        monkeypatch,
        _base(
            modelPresets={
                "coding": {"label": "Coding", "provider": "moonshot", "model": "kimi-k3"},
                "creative": {"label": "Creative", "provider": "moonshot", "model": "kimi-k3"},
                "other": {"label": "Other", "provider": "moonshot", "model": "kimi-k9"},
            }
        ),
    )

    assert _row("coding")["shares_pricing_with"] == ["Creative"]
    assert _row("other")["shares_pricing_with"] == []


def test_a_provider_row_carries_its_default_and_whether_it_is_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _use_config(tmp_path, monkeypatch, _base(providers={"ollama": {}}))
    update_provider_settings(_query(provider="ollama", pricingFree=1))

    row = next(
        entry for entry in settings_payload()["providers"] if entry["name"] == "ollama"
    )

    assert row["pricing_free"] is True
    assert row["is_local"] is True
    assert row["pricing"]["inputPerMtok"] == 0.0
