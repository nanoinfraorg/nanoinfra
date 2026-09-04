"""Named agents and the knowledge block, as config (#247, #237).

Both are additive: a deployment that names no agent and enables no knowledge base reads exactly as
it did before, which is the property these tests exist to keep. The rest is about what config
refuses, because a roster is authority and authority that loads a typo is worse than one that
refuses to load.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanoinfra.config.schema import AgentsConfig, Config, KnowledgeConfig, ToolsConfig
from nanoinfra.webui.named_agents_api import webui_named_agents_payload

# --- named agents ---------------------------------------------------------------------------


def test_no_named_agent_is_the_shape_every_deployment_has_today() -> None:
    assert AgentsConfig().named == {}


def test_a_named_agent_carries_its_own_model_and_tools() -> None:
    config = AgentsConfig.model_validate({
        "named": {
            "sre": {
                "description": "hands-on",
                "modelPreset": "kimi-general",
                "toolGroups": ["servers"],
                "skills": ["servers"],
            }
        }
    })

    agent = config.named["sre"]
    assert agent.model_preset == "kimi-general"
    assert agent.tool_groups == ["servers"]
    assert agent.skills == ["servers"]


def test_an_unset_field_inherits_rather_than_meaning_nothing() -> None:
    """A two-line agent has to be meaningful, so an omitted preset means *the default*, and an
    empty tool-group list means every group — the reading a single-agent deployment already has."""
    agent = AgentsConfig.model_validate({"named": {"minimal": {}}}).named["minimal"]

    assert agent.model_preset is None
    # `None`, not `[]`: nothing declared. An empty list is a *declared* narrowing to no groups at
    # all, which is a different agent -- one that must ask a peer for anything grouped.
    assert agent.tool_groups is None
    assert agent.skills is None
    assert agent.delegates == []


def test_a_roster_naming_an_agent_that_does_not_exist_is_refused_at_load() -> None:
    """The roster is the authorization. An operator who mistypes a peer should hear it from the
    config that refuses to load, not from an agent that cannot find who it was told to ask."""
    with pytest.raises(ValidationError, match="not a configured agent"):
        AgentsConfig.model_validate({"named": {"manager": {"delegates": ["typo"]}}})


def test_an_agent_cannot_delegate_to_itself() -> None:
    with pytest.raises(ValidationError, match="itself as a delegate"):
        AgentsConfig.model_validate({"named": {"loop": {"delegates": ["loop"]}}})


def test_a_valid_roster_loads() -> None:
    config = AgentsConfig.model_validate({
        "named": {"sre": {}, "db": {}, "manager": {"delegates": ["sre", "db"]}}
    })

    assert config.named["manager"].delegates == ["sre", "db"]


def test_the_addendum_is_a_field_rather_than_a_prompt_replacement() -> None:
    """It specialises an agent and cannot replace the tool contract or the safety notes. The
    permission model for the other sections is #256; this only pins that the addendum exists as its
    own field rather than as an override of the prompt."""
    agent = AgentsConfig.model_validate(
        {"named": {"x": {"addendum": "prefer read-only checks"}}}
    ).named["x"]

    assert agent.addendum == "prefer read-only checks"


def test_named_agents_survive_a_full_config_round_trip() -> None:
    config = Config.model_validate({"agents": {"named": {"sre": {"toolGroups": ["servers"]}}}})

    assert config.agents.named["sre"].tool_groups == ["servers"]


def test_an_agent_name_that_cannot_be_a_mention_token_is_refused() -> None:
    """`@agent:<name>` is how an operator asks for one, and the composer splits that token on the
    colon and ends it on whitespace. A name config accepts but nobody can type is a dead agent."""
    for bad in ("db team", "sre:prod", ""):
        with pytest.raises(ValidationError, match="usable agent name"):
            AgentsConfig.model_validate({"named": {bad: {}}})


def test_the_names_an_operator_actually_writes_are_accepted() -> None:
    config = AgentsConfig.model_validate(
        {"named": {"sre-prod": {}, "db_2": {}, "net.core": {}, "báses": {}}}
    )

    assert set(config.named) == {"sre-prod", "db_2", "net.core", "báses"}


def test_the_roster_endpoint_answers_names_and_descriptions_and_nothing_else() -> None:
    """The picker needs a name to offer and a line to explain it. What an agent may *reach* is the
    authorization model, and a browser should not be able to enumerate it from a mention menu."""
    config = Config.model_validate({
        "agents": {
            "named": {
                "sre": {
                    "description": "hands-on checks",
                    "modelPreset": "kimi-general",
                    "toolGroups": ["servers"],
                    "skills": ["servers"],
                    "addendum": "prefer read-only",
                }
            }
        }
    })

    payload = webui_named_agents_payload(config)

    assert payload == {"agents": [{"name": "sre", "description": "hands-on checks"}]}


def test_the_roster_endpoint_is_empty_when_no_agent_is_named() -> None:
    """Which is what makes the `agent:` prefix disappear rather than open an empty menu."""
    assert webui_named_agents_payload(Config()) == {"agents": []}


def test_the_roster_endpoint_keeps_config_order() -> None:
    """Config order is the operator's order; re-sorting it alphabetically would put the manager
    wherever the alphabet puts it rather than where it was written."""
    config = Config.model_validate(
        {"agents": {"named": {"manager": {}, "sre": {}, "db": {}}}}
    )

    payload = webui_named_agents_payload(config)

    assert [agent["name"] for agent in payload["agents"]] == ["manager", "sre", "db"]


# --- knowledge ------------------------------------------------------------------------------


def test_knowledge_is_off_by_default() -> None:
    """An empty index answers nothing, and a deployment that never drops a document should not pay
    for a walk of its workspace."""
    assert KnowledgeConfig().enabled is False
    assert ToolsConfig().knowledge.enabled is False


def test_lexical_is_the_default_mode() -> None:
    """The mode that carries no dependencies. Hybrid is opt-in because for runbooks the words in
    the question are usually the words in the document."""
    assert KnowledgeConfig().mode == "lexical"


def test_an_unknown_mode_is_refused_rather_than_read_as_lexical() -> None:
    with pytest.raises(ValidationError):
        KnowledgeConfig(mode="semantic")


def test_the_secret_excludes_are_present_by_default() -> None:
    """Written out rather than implied, so removing one is deliberate and visible in a diff."""
    excludes = KnowledgeConfig().exclude

    for pattern in (".env", "*.pem", "*.key", "secrets/**"):
        assert pattern in excludes, pattern


def test_the_reindex_interval_has_a_floor() -> None:
    """A shorter window churns a directory walk for no gain; the search tool covers freshness."""
    with pytest.raises(ValidationError):
        KnowledgeConfig(reindexIntervalS=5)


def test_the_size_caps_exist_so_one_log_file_cannot_become_the_knowledge_base() -> None:
    config = KnowledgeConfig()

    assert config.max_file_bytes > 0
    assert config.max_total_bytes > config.max_file_bytes


def test_results_are_bounded() -> None:
    """A citation the model reads is worth more than ten it skims."""
    assert 1 <= KnowledgeConfig().max_results <= 25
    with pytest.raises(ValidationError):
        KnowledgeConfig(maxResults=500)


def test_knowledge_survives_a_full_config_round_trip() -> None:
    config = Config.model_validate(
        {"tools": {"knowledge": {"enabled": True, "reindexIntervalS": 600}}}
    )

    assert config.tools.knowledge.enabled is True
    assert config.tools.knowledge.reindex_interval_s == 600
