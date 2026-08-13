from nanoinfra.config.schema import Config


def test_skills_marketplace_defaults_to_the_hosted_catalog() -> None:
    assert Config().skills_marketplace.nanoinfra_base_url == "https://skills.nanoinfra.org"


def test_skills_marketplace_accepts_both_spellings_of_the_top_level_key() -> None:
    """`skillsMarketplace` used to be rejected outright.

    Every other top-level key accepts a camelCase alias, so a config written in
    the documented camelCase style failed validation on this one key alone.
    """
    for key in ("skillsMarketplace", "skills_marketplace"):
        config = Config.model_validate({key: {"nanoinfraBaseUrl": "https://skills.example.test"}})
        assert config.skills_marketplace.nanoinfra_base_url == "https://skills.example.test", key


def test_skills_marketplace_accepts_both_spellings_of_the_inner_key() -> None:
    for key in ("nanoinfraBaseUrl", "nanoinfra_base_url"):
        config = Config.model_validate({"skills_marketplace": {key: "https://skills.example.test"}})
        assert config.skills_marketplace.nanoinfra_base_url == "https://skills.example.test", key


def test_no_top_level_key_serialises_in_snake_case() -> None:
    """Serialising by alias must produce the camelCase form the docs describe."""
    dumped = Config().model_dump(by_alias=True)
    assert [key for key in dumped if "_" in key] == []
    assert "skillsMarketplace" in dumped
