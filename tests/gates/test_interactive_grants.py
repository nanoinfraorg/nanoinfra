# tests/gates/test_interactive_grants.py
"""Item 8 (#11): standing grants at interactive group scope.

The common case repeats and the operator knows it in advance: reload nginx across the
webservers group. A human who reads forty prompts a week stops reading them, so a matching
grant proceeds with no prompt. Runtime approval then stays the exception, and its rarity is
what keeps it meaningful.

This item is separate from #8. The unattended path has no prompt to skip. Here the grant
exists precisely to prevent one.
"""

from __future__ import annotations

from pathlib import Path

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.policy import Outcome, evaluate
from nanoinfra.servers.store import ServerStore

INTERACTIVE = "interactive"
AUTOMATION = "automation"
_COMMAND = "systemctl reload nginx"


def _gates(*, contexts: list[str], scope_decision: str = "approve") -> GatesConfig:
    return GatesConfig.model_validate(
        {
            "interactive": {"mutate.remote": {"host": scope_decision, "group": scope_decision}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": contexts,
                    "hosts": ["web-01", "web-02"],
                    "commands": [_COMMAND],
                }
            ],
        }
    )


def _decide(gates: GatesConfig, *, scope: str = "group", hosts=("web-01", "web-02")):
    return evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope=scope,
        execution_context=INTERACTIVE,
        hosts=hosts,
        command=_COMMAND,
    )


def test_a_matching_grant_skips_the_prompt_at_interactive_group_scope() -> None:
    decision = _decide(_gates(contexts=["interactive"]))

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "reload"


def test_the_decision_names_the_grant_for_the_audit_record() -> None:
    """#16 records which grant matched, so a reviewer can see why no human was asked."""
    decision = _decide(_gates(contexts=["interactive"]))

    assert "reload" in decision.reason


def test_an_unmatched_group_action_still_asks_for_approval() -> None:
    gates = _gates(contexts=["interactive"])

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context=INTERACTIVE,
        hosts=("web-01", "web-99"),
        command=_COMMAND,
    )

    assert decision.outcome is Outcome.APPROVE


def test_a_different_command_still_asks_for_approval() -> None:
    gates = _gates(contexts=["interactive"])

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context=INTERACTIVE,
        hosts=("web-01", "web-02"),
        command="systemctl restart nginx",
    )

    assert decision.outcome is Outcome.APPROVE


def test_an_unattended_only_grant_does_not_skip_an_interactive_prompt() -> None:
    """`contexts` is the operator's choice about where a grant applies."""
    decision = _decide(_gates(contexts=["unattended"]))

    assert decision.outcome is Outcome.APPROVE


def test_an_interactive_only_grant_does_not_permit_an_unattended_action() -> None:
    """The mirror case. A grant scoped to a present operator grants an automation nothing."""
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"group": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["interactive"],
                    "hosts": ["web-01", "web-02"],
                    "commands": [_COMMAND],
                }
            ],
        }
    )

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="group",
        execution_context=AUTOMATION,
        hosts=("web-01", "web-02"),
        command=_COMMAND,
    )

    assert decision.outcome is Outcome.DENY


def test_a_grant_never_overrides_a_deny() -> None:
    """A grant skips a prompt. It does not overrule a refusal.

    `approve` means "a human may permit this", so a pre-declared permission answers it.
    `deny` means the action is not permitted at all, and a grant that could overrule that
    would make the matrix advisory.
    """
    decision = _decide(_gates(contexts=["interactive"], scope_decision="deny"))

    assert decision.outcome is Outcome.DENY


def test_the_refusal_names_the_shadowed_grant() -> None:
    """An operator who wrote a grant and sees nothing happen must learn which key to change."""
    decision = _decide(_gates(contexts=["interactive"], scope_decision="deny"))

    assert "reload" in decision.reason
    assert "gates.interactive" in decision.reason


def test_a_matching_grant_also_skips_the_prompt_at_host_scope() -> None:
    """Nothing about the rule is group-only. A recurring single-host action repeats too."""
    decision = _decide(_gates(contexts=["interactive"]), scope="host", hosts=("web-01",))

    assert decision.outcome is Outcome.ALLOW


def test_all_scope_is_still_refused_with_a_matching_grant() -> None:
    """No grant reaches unbounded scope, in any context."""
    gates = GatesConfig.model_validate(
        {
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["interactive"],
                    "hosts": ["web-01", "web-02"],
                    "commands": [_COMMAND],
                }
            ]
        }
    )

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="all",
        execution_context=INTERACTIVE,
        hosts=("web-01", "web-02"),
        command=_COMMAND,
    )

    assert decision.outcome is Outcome.DENY


def test_an_interactive_grant_matches_on_the_resolved_target(tmp_path: Path) -> None:
    """#24 applies here too: a grant lists names, and the gate compares resolved addresses."""
    store = ServerStore(tmp_path)
    store.create({"name": "web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}})
    gates = GatesConfig.model_validate(
        {
            "interactive": {"mutate.remote": {"host": "approve"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["interactive"],
                    "hosts": ["web-01"],
                    "commands": [_COMMAND],
                }
            ],
        }
    )

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=INTERACTIVE,
        hosts=("10.0.1.5",),
        command=_COMMAND,
        servers=store,
    )

    assert decision.outcome is Outcome.ALLOW
