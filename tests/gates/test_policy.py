# tests/gates/test_policy.py
"""Item 6 (#8): evaluate gate policy, deny by default for unattended contexts.

The only allow path for an unattended context is a standing grant that covers the resolved
host set and holds the resolved command exactly. Everything else refuses.
"""

from __future__ import annotations

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_INVENTORY,
    MUTATE_REMOTE,
)
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.policy import Outcome, evaluate

INTERACTIVE = "interactive"
AUTOMATION = "automation"
SUBAGENT = "subagent"


def _grant(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "reload-nginx",
        "contexts": ["unattended"],
        "hosts": ["staging-web-01"],
        "commands": ["systemctl reload nginx"],
    }
    base.update(over)
    return base


def test_an_unattended_remote_action_without_a_grant_is_refused() -> None:
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="systemctl reload nginx",
    )

    assert decision.outcome is Outcome.DENY


def _grant_policy(**grant_over: object) -> GatesConfig:
    """Policy an operator writes to enable grants: the scope decision says `grant`."""
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant", "group": "grant"}},
            "standingGrants": [_grant(**grant_over)],
        }
    )


def test_a_matching_grant_allows_an_unattended_remote_action() -> None:
    decision = evaluate(
        _grant_policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="systemctl reload nginx",
    )

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "reload-nginx"


def test_a_grant_is_ignored_while_the_scope_decision_stays_deny() -> None:
    """`deny` means deny. A grant does not override the matrix, so both must agree."""
    gates = GatesConfig.model_validate({"standingGrants": [_grant()]})

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="systemctl reload nginx",
    )

    assert decision.outcome is Outcome.DENY


def test_a_refusal_says_when_a_grant_would_have_matched_but_policy_denies() -> None:
    """Without this, an operator writes a grant, sees nothing happen, and cannot tell why.

    A grant that matches everything except the matrix is the most confusing possible state,
    so the refusal names the exact key to change.
    """
    gates = GatesConfig.model_validate({"standingGrants": [_grant()]})

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="systemctl reload nginx",
    )

    assert "reload-nginx" in decision.reason
    assert "grant" in decision.reason
    assert "gates.unattended" in decision.reason


def test_a_command_off_by_one_character_is_refused() -> None:
    """`commands` is an exact allowlist. A near miss is a miss."""
    gates = GatesConfig.model_validate({"standingGrants": [_grant()]})

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="systemctl reload nginx ",
    )

    assert decision.outcome is Outcome.DENY


def test_a_grant_must_cover_every_resolved_host() -> None:
    """Partial coverage is no coverage. A group must not execute on a host nobody granted."""
    gates = GatesConfig.model_validate({"standingGrants": [_grant()]})

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context=AUTOMATION,
        hosts=("staging-web-01", "prod-web-09"),
        command="systemctl reload nginx",
    )

    assert decision.outcome is Outcome.DENY


def test_a_subagent_is_refused_even_though_its_parent_is_interactive() -> None:
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=SUBAGENT,
        hosts=("staging-web-01",),
        command="uptime",
    )

    assert decision.outcome is Outcome.DENY


def test_a_grant_scoped_to_unattended_does_not_apply_interactively() -> None:
    gates = GatesConfig.model_validate({"standingGrants": [_grant(contexts=["unattended"])]})

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=INTERACTIVE,
        hosts=("staging-web-01",),
        command="systemctl reload nginx",
    )

    assert decision.outcome is Outcome.APPROVE


def test_all_scope_is_refused_in_every_context() -> None:
    """No runtime path to unbounded scope exists, and no grant can create one."""
    gates = GatesConfig.model_validate(
        {"standingGrants": [_grant(contexts=["unattended", "interactive"], hosts=["a", "b"])]}
    )

    for context in (INTERACTIVE, AUTOMATION, SUBAGENT):
        decision = evaluate(
            gates,
            capability_class=MUTATE_REMOTE,
            scope="all",
            execution_context=context,
            hosts=("a", "b"),
            command="systemctl reload nginx",
        )

        assert decision.outcome is Outcome.DENY


def test_an_unattended_inventory_write_is_refused() -> None:
    """#23: an inventory write changes what a later remote action reaches."""
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_INVENTORY,
        scope="host",
        execution_context=AUTOMATION,
        hosts=(),
        command="update_server",
    )

    assert decision.outcome is Outcome.DENY


def test_an_interactive_inventory_write_is_allowed() -> None:
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_INVENTORY,
        scope="host",
        execution_context=INTERACTIVE,
        hosts=(),
        command="update_server",
    )

    assert decision.outcome is Outcome.ALLOW


def test_a_grant_cannot_permit_an_inventory_write() -> None:
    """A grant carries no class, so it must never satisfy mutate.inventory."""
    gates = GatesConfig.model_validate(
        {"standingGrants": [_grant(contexts=["unattended"], commands=["update_server"], hosts=[])]}
    )

    decision = evaluate(
        gates,
        capability_class=MUTATE_INVENTORY,
        scope="host",
        execution_context=AUTOMATION,
        hosts=(),
        command="update_server",
    )

    assert decision.outcome is Outcome.DENY


def test_an_unattended_credential_access_is_refused() -> None:
    decision = evaluate(
        GatesConfig(),
        capability_class=CREDENTIAL_ACCESS,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="uptime",
    )

    assert decision.outcome is Outcome.DENY


def test_a_refusal_names_the_class_the_scope_and_the_missing_grant() -> None:
    """An operator debugging a broken automation at 03:00 must learn which grant to write."""
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context=AUTOMATION,
        hosts=("web-01", "web-02"),
        command="systemctl reload nginx",
    )

    assert MUTATE_REMOTE in decision.reason
    assert "group" in decision.reason
    assert "grant" in decision.reason


def test_an_unknown_capability_class_is_refused() -> None:
    """Fail closed. A class the policy does not model must not fall through to allow."""
    decision = evaluate(
        GatesConfig(),
        capability_class="mutate.something_new",
        scope="host",
        execution_context=INTERACTIVE,
        hosts=("web-01",),
        command="uptime",
    )

    assert decision.outcome is Outcome.DENY


def test_an_unknown_execution_context_is_treated_as_unattended() -> None:
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context="something_new",
        hosts=("web-01",),
        command="uptime",
    )

    assert decision.outcome is Outcome.DENY


def test_an_unresolved_scope_is_refused() -> None:
    """#4 returns `unresolved` when it cannot expand a pattern. That is not a scope."""
    decision = evaluate(
        GatesConfig(),
        capability_class=MUTATE_REMOTE,
        scope="unresolved",
        execution_context=INTERACTIVE,
        hosts=(),
        command="uptime",
    )

    assert decision.outcome is Outcome.DENY
