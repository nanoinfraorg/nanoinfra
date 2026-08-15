# tests/gates/test_identity_independence.py
"""Item 11 (#47): a second person can stand in for a second path, and the flag is off.

#13 compares path strings, so two people behind one proxy are one path and neither can answer
the other's request. With real identities that is the wrong answer, and ``gates`` gains a flag
that changes it.

**The flag defaults to false, and the reason is not caution.** The origin identity is an
assertion of the agent. ``nanoinfra/gates/executor/protocol.py`` already says that about the
origin path, and ``origin_actor`` inherits it exactly. So a compromised agent can claim that the
request came from another person, and one genuine human approval then substitutes for two
authenticated paths. The human still reads the command and the host list the executor rendered,
which stays the real protection. The deployment gives up the property that one compromised
account cannot hold both halves.

The rule, when the flag is true:

- An answer counts when the answering identity differs from the origin identity, **or** when the
  answering path differs from the origin path. Either one is enough.
- An answer never counts when both are the same. Self-approval stays impossible in every mode,
  and this file tests that case in both modes.
- An unknown origin identity falls back to the path rule alone. A missing value is never a
  wildcard that matches every person.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.approval_delivery import delivery_targets
from nanoinfra.gates.approvals import (
    ApprovalRefusal,
    approval_feasible,
    check_approval,
)
from nanoinfra.gates.executor.operator_socket import (
    OPERATOR_PROTOCOL_VERSION,
    ApprovalService,
    pending_view,
)
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.startup import identity_posture_line, policy_summary
from nanoinfra.gates.tokens import ApprovalTokenStore

# Two people behind one proxy, in the prefixed form #47 uses for an asserted identity.
_ALICE = "webui:alice@example.com"
_BOB = "webui:bob@example.com"

_PAYLOAD = "nanoinfra approval request v1\n"
_DIGEST = "sha256:" + "d" * 64


def _one_path_gates(*, independence: bool, approvers: list[dict[str, str]] | None = None):
    """A WebUI-only deployment, which is the common single-operator install.

    It has one path, so the path rule alone leaves it with no runtime approval path. That is the
    deployment the flag exists for.
    """
    return GatesConfig.model_validate(
        {
            "approvers": approvers
            if approvers is not None
            else [
                {"channel": "webui", "sender": _ALICE},
                {"channel": "webui", "sender": _BOB},
            ],
            "approvalPaths": ["webui"],
            "identityIndependence": independence,
        }
    )


def _two_path_gates(*, independence: bool) -> GatesConfig:
    """The same two people, plus a hardened Telegram beside the WebUI."""
    return GatesConfig.model_validate(
        {
            "approvers": [
                {"channel": "webui", "sender": _ALICE},
                {"channel": "webui", "sender": _BOB},
            ],
            "approvalPaths": ["webui", "telegram"],
            "identityIndependence": independence,
        }
    )


# -- the config ---------------------------------------------------------------------------


def test_the_flag_is_off_by_default() -> None:
    """A deployment that writes no policy keeps the two-path property."""
    assert GatesConfig().identity_independence is False


def test_the_flag_reads_the_camel_case_key() -> None:
    """JSON config carries ``identityIndependence``, like every other key in this block."""
    gates = GatesConfig.model_validate({"identityIndependence": True})

    assert gates.identity_independence is True


def test_a_mistyped_flag_refuses_rather_than_reads_as_off() -> None:
    """``extra="forbid"`` covers this block, so a typo cannot turn the flag into a default.

    A silently ignored key would leave an operator believing that two people can answer.
    """
    with pytest.raises(ValidationError):
        GatesConfig.model_validate({"identityIndepedence": True})


# -- the flag off, which is every deployment today ----------------------------------------


def test_with_the_flag_off_a_second_person_on_the_origin_path_cannot_answer() -> None:
    """Today's behaviour, unchanged. Two people on one path are one path."""
    check = check_approval(
        gates=_two_path_gates(independence=False),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_BOB,
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.SAME_PATH


def test_with_the_flag_off_a_person_cannot_approve_their_own_request() -> None:
    """Self-approval is impossible in this mode, and the path rule is what refuses it."""
    check = check_approval(
        gates=_two_path_gates(independence=False),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_ALICE,
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.SAME_PATH


# -- the flag on --------------------------------------------------------------------------


def test_with_the_flag_on_a_second_person_on_the_origin_path_answers() -> None:
    """The case the flag exists for. Alice asked, and Bob answered on the same path."""
    check = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_BOB,
    )

    assert check.ok
    assert check.refusal is None
    assert check.same_path is True  # the record still reports the fact


def test_with_the_flag_on_a_person_still_cannot_approve_their_own_request() -> None:
    """Self-approval stays impossible in every mode.

    Both halves of the rule failed here, so the refusal carries its own name. An operator who
    read ``same_path`` would add a second path, which does not fix this answer.
    """
    check = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_ALICE,
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.SAME_ACTOR_AND_PATH
    assert check.refusal != ApprovalRefusal.SAME_PATH


def test_the_refusal_names_the_person_rule_and_the_path_rule() -> None:
    """An operator reads which rule failed, and never a bare denial (#27 and #43)."""
    same_person = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_ALICE,
    )
    same_path = check_approval(
        gates=_two_path_gates(independence=False),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_BOB,
    )

    assert "same person" in same_person.reason
    assert "webui" in same_path.reason
    assert "different authenticated path" in same_path.reason


def test_an_unknown_origin_identity_is_not_a_wildcard() -> None:
    """A missing value must not match every person, which would relax the rule for free.

    The naive test ``origin_actor != sender`` reads an absent identity as "a different person",
    and the same-path rule would then never refuse. So an unknown origin identity falls back to
    the path rule alone, in both modes.
    """
    for origin_actor in ("", "   "):
        check = check_approval(
            gates=_two_path_gates(independence=True),
            origin_path="webui",
            origin_actor=origin_actor,
            approval_path="webui",
            sender=_BOB,
        )

        assert not check.ok, origin_actor
        assert check.refusal == ApprovalRefusal.SAME_PATH, origin_actor
        assert "identityIndependence" in check.reason, origin_actor


def test_the_origin_identity_is_matched_by_the_whole_string() -> None:
    """The same exactness as the approver match. A near miss is a different person.

    A case-folded or truncated compare would read one person as two, and self-approval would
    then pass through the identity half of the rule.
    """
    for lookalike in ("WEBUI:alice@example.com", "webui:alice@example.com.attacker.test"):
        check = check_approval(
            gates=GatesConfig.model_validate(
                {
                    "approvers": [{"channel": "webui", "sender": lookalike}],
                    "approvalPaths": ["webui"],
                    "identityIndependence": True,
                }
            ),
            origin_path="webui",
            origin_actor=_ALICE,
            approval_path="webui",
            sender=lookalike,
        )

        assert check.ok, lookalike


def test_the_same_person_on_a_second_path_still_answers() -> None:
    """#13 says the same operator is acceptable. The flag adds a rule and removes none."""
    check = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="telegram",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_ALICE,
    )

    assert check.ok


def test_a_single_path_deployment_gains_a_runtime_approval_path() -> None:
    """With the flag off this deployment has none, and the refusal says so.

    With the flag on, the second person is the second factor, so the same answer counts.
    """
    off = check_approval(
        gates=_one_path_gates(independence=False),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_BOB,
    )
    on = check_approval(
        gates=_one_path_gates(independence=True),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_BOB,
    )

    assert off.refusal == ApprovalRefusal.NO_SECOND_PATH
    assert on.ok


def test_the_record_carries_both_identities_and_the_same_person_fact() -> None:
    """#16 records the decision, and a reviewer needs to see who asked and who answered."""
    check = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_ALICE,
    )
    fields = check.audit_fields()

    assert fields["origin_actor"] == _ALICE
    assert fields["sender"] == _ALICE
    assert fields["same_actor"] is True
    assert fields["same_path"] is True
    assert fields["refusal"] == "same_actor_and_path"


def test_the_record_reports_two_people_as_two_people() -> None:
    """The same fact on the other side. ``same_actor`` is false for a real second person."""
    check = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="webui",
        origin_actor=_ALICE,
        approval_path="webui",
        sender=_BOB,
    )

    assert check.audit_fields()["same_actor"] is False
    assert check.audit_fields()["origin_actor"] == _ALICE


def test_an_unknown_origin_identity_is_not_the_same_person_either() -> None:
    """``same_actor`` reports a fact, so an absent value is not a match.

    A record that read "the same person" for a request that named nobody would send a reviewer
    after an operator who did nothing.
    """
    check = check_approval(
        gates=_two_path_gates(independence=True),
        origin_path="webui",
        origin_actor="",
        approval_path="webui",
        sender="",
    )

    assert check.audit_fields()["same_actor"] is False


# -- can any approval exist at all (#38) --------------------------------------------------


def test_a_single_path_deployment_with_two_people_can_be_asked() -> None:
    """The feasibility test must agree with the check, or the executor refuses before it asks.

    A disagreement here would suspend nothing and the flag would change nothing in production.
    """
    answer = approval_feasible(
        gates=_one_path_gates(independence=True), origin_path="webui", origin_actor=_ALICE
    )

    assert answer.ok
    assert answer.refusal is None


def test_a_single_path_deployment_with_one_person_cannot_be_asked() -> None:
    """Alice is the only approver, and she raised the request. Nobody else may answer.

    The refusal names ``gates.approvers`` rather than the missing path, because adding a path
    is not the fix for a deployment with one person.
    """
    answer = approval_feasible(
        gates=_one_path_gates(independence=True, approvers=[{"channel": "webui", "sender": _ALICE}]),
        origin_path="webui",
        origin_actor=_ALICE,
    )

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NOT_AN_APPROVER
    assert "gates.approvers" in answer.reason


def test_the_flag_changes_no_feasibility_when_the_request_names_no_person() -> None:
    """An unknown origin identity leaves the path rule in charge here too."""
    answer = approval_feasible(
        gates=_one_path_gates(independence=True), origin_path="webui", origin_actor=""
    )

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NO_SECOND_PATH


def test_a_single_path_deployment_with_the_flag_off_cannot_be_asked() -> None:
    """Today's answer, unchanged, for the deployment that wrote no flag."""
    answer = approval_feasible(
        gates=_one_path_gates(independence=False), origin_path="webui", origin_actor=_ALICE
    )

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NO_SECOND_PATH


# -- who receives one request (#43) -------------------------------------------------------


def test_with_the_flag_on_a_second_person_on_the_origin_path_is_a_target() -> None:
    """The watcher must reach the only person who can answer.

    A watcher that kept the origin path out would deliver to nobody on a single-path
    deployment, and the action would wait for the timeout. That hang is what #38 exists to
    stop.
    """
    targets = delivery_targets(
        gates=_one_path_gates(independence=True), origin_path="webui", origin_actor=_ALICE
    )

    assert [target.chat_id for target in targets] == [_BOB]


def test_the_person_who_raised_the_request_is_never_a_target() -> None:
    """A delivery to Alice would invite an answer the gate refuses.

    An operator who answers and reads a refusal stops reading the message.
    """
    targets = delivery_targets(
        gates=_one_path_gates(independence=True), origin_path="webui", origin_actor=_ALICE
    )

    assert _ALICE not in [target.chat_id for target in targets]


def test_with_the_flag_off_the_origin_path_reaches_nobody() -> None:
    """Today's behaviour. An answer from that path cannot count, so nothing goes there."""
    targets = delivery_targets(
        gates=_one_path_gates(independence=False), origin_path="webui", origin_actor=_ALICE
    )

    assert targets == ()


def test_an_unknown_origin_identity_keeps_the_origin_path_out() -> None:
    """The fallback holds for delivery too. No identity means the path rule alone."""
    targets = delivery_targets(
        gates=_one_path_gates(independence=True), origin_path="webui", origin_actor=""
    )

    assert targets == ()


# -- the identity reaches the executor ----------------------------------------------------


def test_the_pending_view_carries_the_origin_identity() -> None:
    """The operator socket answers with this view, and the service judges from the store."""
    store = PendingApprovalStore()
    approval = store.create(
        session_id="s1",
        origin_path="webui",
        origin_actor=_ALICE,
        execution_context="interactive",
        capability_class="mutate.remote",
        scope="host",
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        payload=_PAYLOAD,
        target_digest=_DIGEST,
        timeout_s=30.0,
    )

    assert approval.origin_actor == _ALICE
    assert pending_view(approval)["origin_actor"] == _ALICE


def test_the_operator_wire_version_rose_with_the_pending_view() -> None:
    """The view gained a field, so the version rose.

    The rule is the one the execute wire sets: a peer that shares the version shares the field
    set. A tolerated difference between two peers is an ambiguity, and an ambiguity here is a
    hole.
    """
    assert OPERATOR_PROTOCOL_VERSION == 2


def _service(gates: GatesConfig) -> tuple[ApprovalService, PendingApprovalStore]:
    store = PendingApprovalStore()
    return (
        ApprovalService(pending=store, tokens=ApprovalTokenStore(), gates_loader=lambda: gates),
        store,
    )


def _suspend(store: PendingApprovalStore, *, origin_actor: str):
    return store.create(
        session_id="s1",
        origin_path="webui",
        origin_actor=origin_actor,
        execution_context="interactive",
        capability_class="mutate.remote",
        scope="host",
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        payload=_PAYLOAD,
        target_digest=_DIGEST,
        timeout_s=30.0,
    )


def test_the_service_lets_a_second_person_approve_on_the_origin_path() -> None:
    """End to end through the authority behind the operator socket."""
    service, store = _service(_one_path_gates(independence=True))
    approval = _suspend(store, origin_actor=_ALICE)

    result = service.approve(
        request_id=approval.request_id,
        actor=_BOB,
        approval_path="webui",
        target_digest=_DIGEST,
    )

    assert result.ok


def test_the_service_refuses_the_person_who_raised_the_request() -> None:
    """The same store, the same path, and the person who asked. This never counts."""
    service, store = _service(_one_path_gates(independence=True))
    approval = _suspend(store, origin_actor=_ALICE)

    result = service.approve(
        request_id=approval.request_id,
        actor=_ALICE,
        approval_path="webui",
        target_digest=_DIGEST,
    )

    assert not result.ok
    assert result.refusal == ApprovalRefusal.SAME_ACTOR_AND_PATH.value


def test_the_service_refuses_a_second_person_when_the_flag_is_off() -> None:
    """The default deployment. One path is one path, whoever answers."""
    service, store = _service(_one_path_gates(independence=False))
    approval = _suspend(store, origin_actor=_ALICE)

    result = service.approve(
        request_id=approval.request_id,
        actor=_BOB,
        approval_path="webui",
        target_digest=_DIGEST,
    )

    assert not result.ok
    assert result.refusal == ApprovalRefusal.NO_SECOND_PATH.value


# -- the operator reads the posture at start ----------------------------------------------


def test_the_startup_line_names_the_posture_when_the_flag_is_on() -> None:
    """A deployment that gives up a property must read that fact at every start.

    #8 exists because a mistyped block loads as silent defaults. A flag that widens who may
    approve deserves the same treatment. #72 gives the posture a line of its own, so the fact
    reaches the operator beside the trusted-proxy posture instead of inside the policy line.
    """
    line = identity_posture_line(_one_path_gates(independence=True))

    assert line is not None
    assert "gates.identityIndependence" in line
    assert "assertion of the agent" in line
    assert "identityIndependence" not in policy_summary(_one_path_gates(independence=True))


def test_the_startup_line_says_nothing_when_the_flag_is_off() -> None:
    """Silence is correct for the default. A line about every flag teaches nobody to read one."""
    assert identity_posture_line(GatesConfig()) is None
    assert "identityIndependence" not in policy_summary(GatesConfig())
