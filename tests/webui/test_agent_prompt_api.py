"""The read behind the Prompt tab (#256).

Two rules are load-bearing here and both are about the line between two kinds of read. The Prompt
tab answers *what is this agent told, and what of that is mine to change* -- so it carries section
names, permissions and the addendum's text. It does not answer *what may this agent reach*, which
is the authorization model and stays in the config file a human reviews.
"""

from __future__ import annotations

from nanoinfra.agent.prompt_sections import ADDENDUM_SECTION
from nanoinfra.config.schema import Config
from nanoinfra.webui.agent_prompt_api import webui_agent_prompt_payload


def _config() -> Config:
    return Config.model_validate(
        {
            "agents": {
                "named": {
                    "sre": {
                        "description": "hands-on checks",
                        # Deliberately unmistakable strings: the assertion below is that none of
                        # them reach the payload, and a value like "servers" would also match the
                        # name of a section.
                        "toolGroups": ["group-omega"],
                        "skills": ["skill-omega"],
                        "connectors": ["connector-omega"],
                        "delegates": ["db"],
                        "addendum": "Prefer read-only checks, and say what you did not check.",
                    },
                    "db": {"description": "slow queries"},
                }
            }
        }
    )


def test_an_unknown_agent_has_no_prompt_to_describe() -> None:
    """404, not an empty section list: a panel that rendered "no sections" for a typo would be
    describing a prompt that does not exist."""
    assert webui_agent_prompt_payload(_config(), "nobody") is None


def test_every_section_arrives_with_a_permission() -> None:
    payload = webui_agent_prompt_payload(_config(), "sre")

    assert payload is not None
    permissions = {str(row["name"]): row["permission"] for row in payload["sections"]}
    assert permissions["Tool usage notes"] == "fixed"
    assert permissions["Safety notes"] == "fixed"
    assert permissions["Memory"] == "replaceable"
    assert permissions[ADDENDUM_SECTION] == "append_only"


def test_the_payload_carries_the_addendum_and_none_of_the_bindings() -> None:
    """Instructions, yes; permissions, no.

    The addendum is prompt content -- it is appended after the platform's sections and cannot
    widen what the agent may reach. Tool groups, skills, connectors and delegates are the
    authorization model, and a browser that could enumerate them here would be reading it out of a
    settings panel.
    """
    payload = webui_agent_prompt_payload(_config(), "sre")

    assert payload is not None
    assert payload["addendum"].startswith("Prefer read-only checks")
    flat = repr(payload)
    assert "omega" not in flat
    assert "db" not in flat
    for key in ("tool_groups", "toolGroups", "skills", "connectors", "delegates"):
        assert key not in payload


def test_the_fixed_sections_state_their_cost_and_the_per_turn_ones_do_not() -> None:
    """A Memory figure taken from one turn would read as a property of the agent.

    The tool contract and the safety notes cost the same on every turn of this deployment, so the
    number is honest. The per-turn numbers already exist where they belong: on the turn.
    """
    payload = webui_agent_prompt_payload(_config(), "sre")

    assert payload is not None
    rows = {str(row["name"]): row for row in payload["sections"]}
    assert isinstance(rows["Tool usage notes"]["tokens"], int)
    assert rows["Tool usage notes"]["tokens"] > 0
    assert rows["Memory"]["tokens"] is None
    assert rows["Memory"]["static"] is False


def test_the_addendum_section_is_absent_for_an_agent_that_has_none() -> None:
    """Present is a fact about this agent; the permission is a fact about the platform."""
    payload = webui_agent_prompt_payload(_config(), "db")

    assert payload is not None
    addendum_row = next(
        row for row in payload["sections"] if str(row["name"]) == ADDENDUM_SECTION
    )
    assert addendum_row["present"] is False
    assert addendum_row["permission"] == "append_only"
    assert payload["addendum"] == ""


def test_nothing_is_claimed_to_be_measured() -> None:
    """The same caveat the prompt manifest carries: these are our tokenizer's estimates."""
    payload = webui_agent_prompt_payload(_config(), "sre")

    assert payload is not None
    assert payload["measured"] is False
