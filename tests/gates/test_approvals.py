# tests/gates/test_approvals.py
"""Item 10 (#13): an approval must arrive on a different authenticated path.

Three conditions hold together, and each one gets its own tests. The conditions are
independent: a listed approver can still answer on the origin channel, and a second path
can still fail to authenticate anybody.

The single-path deployment matters most. A WebUI-only install has one path, so it has no
runtime approval path for an unusual group action. That install must read a specific error
that names the missing second path. A generic "denied" reads as a bug.

Nothing here touches a channel allowlist or the pairing store as an authority. One test
constructs a real ``allowFrom`` entry to prove the check ignores it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from nanoinfra.config.gates import GatesConfig
from nanoinfra.config.schema import Config
from nanoinfra.gates.approvals import (
    NO_SECOND_PATH_REASON,
    ApprovalRefusal,
    approval_feasible,
    check_approval,
)


def _two_path_gates() -> GatesConfig:
    """A deployment with a hardened Telegram beside the WebUI, and one listed approver."""
    return GatesConfig.model_validate(
        {
            "approvers": [{"channel": "webui", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
        }
    )


def test_an_approval_on_a_second_authenticated_path_counts() -> None:
    """The request arrives on Telegram, and the operator answers in the WebUI session.

    The same operator is acceptable. The second path is what the rule requires.
    """
    check = check_approval(
        gates=_two_path_gates(),
        origin_path="telegram",
        approval_path="webui",
        sender="operator-1",
    )

    assert check.ok
    assert check.refusal is None
    assert check.same_path is False


def test_an_approval_on_the_origin_path_gets_a_refusal_at_group_scope() -> None:
    """A requester who approves on the origin channel is single-factor by construction.

    One compromised Telegram account would otherwise yield the request and the approval.
    """
    gates = GatesConfig.model_validate(
        {
            "approvers": [{"channel": "telegram", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
        }
    )

    check = check_approval(
        gates=gates,
        origin_path="telegram",
        approval_path="telegram",
        sender="operator-1",
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.SAME_PATH


def test_the_record_shows_the_same_path_case_and_both_paths() -> None:
    """A future policy may relax this rule, and two paths may share one credential.

    The reviewer needs the fact in the record, so ``same_path`` is a field and not a log line.
    """
    gates = GatesConfig.model_validate(
        {
            "approvers": [{"channel": "telegram", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
        }
    )

    check = check_approval(
        gates=gates,
        origin_path="telegram",
        approval_path="telegram",
        sender="operator-1",
    )

    assert check.same_path is True
    assert check.audit_fields()["same_path"] is True
    assert check.audit_fields()["origin_path"] == "telegram"
    assert check.audit_fields()["approval_path"] == "telegram"
    assert check.audit_fields()["refusal"] == "same_path"


def test_a_sender_inside_allow_from_but_outside_approvers_gets_a_refusal() -> None:
    """``allowFrom`` carries reachability, so it grants no approval authority.

    The pairing store grants nothing either. Chat writes that store at runtime, and an
    injected instruction attacks a runtime-mutable list first.
    """
    config = Config.model_validate(
        {
            "channels": {"telegram": {"allowFrom": ["12345"]}},
            "gates": {"approvers": [], "approvalPaths": ["webui", "telegram"]},
        }
    )
    channels = config.channels.model_extra or {}

    check = check_approval(
        gates=config.gates,
        origin_path="webui",
        approval_path="telegram",
        sender="12345",
    )

    assert channels["telegram"]["allowFrom"] == ["12345"]  # the sender can reach the bot
    assert not check.ok  # and reachability decides nothing
    assert check.refusal == ApprovalRefusal.NOT_AN_APPROVER
    assert "gates.approvers" in check.reason


def test_an_approver_listed_for_another_path_does_not_approve_on_this_one() -> None:
    """An approver entry names one channel. The path that carried the approval must match.

    A caller that could pass any channel name beside any path would break the binding.
    """
    check = check_approval(
        gates=_two_path_gates(),  # operator-1 is listed on webui only
        origin_path="webui",
        approval_path="telegram",
        sender="operator-1",
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.NOT_AN_APPROVER


def test_an_approval_path_outside_gates_approval_paths_does_not_authenticate() -> None:
    """Condition 2. A path that authenticates nobody cannot deliver the second factor."""
    gates = GatesConfig.model_validate(
        {
            "approvers": [{"channel": "telegram", "sender": "operator-1"}],
            "approvalPaths": ["webui", "discord"],
        }
    )

    check = check_approval(
        gates=gates,
        origin_path="webui",
        approval_path="telegram",
        sender="operator-1",
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.UNAUTHENTICATED_PATH
    assert "gates.approvalPaths" in check.reason


def test_a_webui_only_deployment_returns_the_missing_second_path_error() -> None:
    """The default config lists one path, which is the common single-operator install.

    That install has no runtime approval path. The error names the missing thing, so the
    operator can add a path or declare a standing grant instead of a bug report.
    """
    gates = GatesConfig.model_validate({"approvers": [{"channel": "webui", "sender": "op"}]})

    check = check_approval(
        gates=gates,
        origin_path="webui",
        approval_path="webui",
        sender="op",
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.NO_SECOND_PATH
    assert NO_SECOND_PATH_REASON in check.reason
    assert (
        "no second authenticated path is configured. Add one, or declare a standing grant"
        in check.reason
    )


def test_the_missing_second_path_error_names_the_configured_paths_and_the_origin() -> None:
    """A named path and a named origin turn the refusal into an action."""
    gates = GatesConfig.model_validate({"approvalPaths": ["webui"]})

    check = check_approval(
        gates=gates,
        origin_path="webui",
        approval_path="webui",
        sender="op",
    )

    assert "webui" in check.reason
    assert "gates.approvalPaths" in check.reason
    assert check.same_path is True  # recorded, even though the deployment fails earlier


def test_an_empty_approval_path_list_leaves_no_runtime_approval_path() -> None:
    """An operator who empties the list removes every authenticated path.

    A blank entry counts for nothing, because a blank string authenticates nobody.
    """
    gates = GatesConfig.model_validate(
        {"approvers": [{"channel": "webui", "sender": "op"}], "approvalPaths": ["  "]}
    )

    check = check_approval(
        gates=gates,
        origin_path="telegram",
        approval_path="webui",
        sender="op",
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.NO_SECOND_PATH


def test_a_request_without_an_origin_path_fails_closed() -> None:
    """No origin path means no proof that the approval arrived somewhere else."""
    check = check_approval(
        gates=_two_path_gates(),
        origin_path="   ",
        approval_path="webui",
        sender="operator-1",
    )

    assert not check.ok
    assert check.refusal == ApprovalRefusal.UNKNOWN_ORIGIN_PATH


def test_sender_matching_is_exact() -> None:
    """Sender ids are opaque tokens, so a near match is a mismatch.

    Case folding or a prefix test would let a lookalike identity approve.
    """
    for lookalike in ("Operator-1", "operator-10", "perator-1", "operator_1"):
        check = check_approval(
            gates=_two_path_gates(),
            origin_path="telegram",
            approval_path="webui",
            sender=lookalike,
        )

        assert not check.ok, lookalike

    trimmed = check_approval(
        gates=_two_path_gates(),
        origin_path="telegram",
        approval_path="webui",
        sender=" operator-1 ",
    )

    assert trimmed.ok  # surrounding whitespace is transport noise, not identity


def test_every_refusal_carries_a_reason_and_a_named_refusal() -> None:
    """#13 renders the reason and #16 records the refusal, so neither may be empty."""
    scenarios = [
        ("   ", "webui", "operator-1", GatesConfig()),
        ("webui", "webui", "operator-1", GatesConfig()),
        ("webui", "telegram", "nobody", _two_path_gates()),
        ("webui", "discord", "operator-1", _two_path_gates()),
        ("telegram", "telegram", "operator-1", _two_path_gates()),
    ]

    for origin, approval, sender, gates in scenarios:
        check = check_approval(
            gates=gates,
            origin_path=origin,
            approval_path=approval,
            sender=sender,
        )

        assert not check.ok, (origin, approval, sender)
        assert check.refusal is not None, (origin, approval, sender)
        assert check.reason.strip(), (origin, approval, sender)


# ------------------------------------------------------- can any approval exist at all (#38)


def test_a_deployment_with_an_approver_on_a_second_path_can_be_asked() -> None:
    """#38 asks this before it suspends an action.

    A suspended action that nobody can answer is a hang. The executor must refuse first.
    """
    answer = approval_feasible(gates=_two_path_gates(), origin_path="telegram")

    assert answer.ok
    assert answer.refusal is None


def test_a_single_path_deployment_cannot_be_asked() -> None:
    """The WebUI-only install. The refusal names the missing second path, as #13 requires."""
    gates = GatesConfig.model_validate(
        {"approvers": [{"channel": "webui", "sender": "operator-1"}]}
    )

    answer = approval_feasible(gates=gates, origin_path="webui")

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NO_SECOND_PATH
    assert NO_SECOND_PATH_REASON in answer.reason


def test_a_deployment_with_no_approver_cannot_be_asked() -> None:
    """Two paths and nobody to ask is still a hang. The reason names gates.approvers."""
    gates = GatesConfig.model_validate({"approvalPaths": ["webui", "telegram"]})

    answer = approval_feasible(gates=gates, origin_path="telegram")

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NOT_AN_APPROVER
    assert "gates.approvers" in answer.reason


def test_an_approver_on_the_origin_path_alone_cannot_be_asked() -> None:
    """Condition 3 already refuses that answer, so the request must not wait for it."""
    gates = GatesConfig.model_validate(
        {
            "approvers": [{"channel": "telegram", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
        }
    )

    answer = approval_feasible(gates=gates, origin_path="telegram")

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NOT_AN_APPROVER


def test_an_approver_on_an_unauthenticated_path_cannot_be_asked() -> None:
    """A path outside gates.approvalPaths authenticates nobody, so it answers nothing."""
    gates = GatesConfig.model_validate(
        {
            "approvers": [{"channel": "discord", "sender": "operator-1"}],
            "approvalPaths": ["webui", "telegram"],
        }
    )

    answer = approval_feasible(gates=gates, origin_path="telegram")

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.NOT_AN_APPROVER


def test_a_request_with_no_origin_path_cannot_be_asked() -> None:
    """Path independence needs an origin. An agent that states none must not execute."""
    answer = approval_feasible(gates=_two_path_gates(), origin_path="  ")

    assert not answer.ok
    assert answer.refusal == ApprovalRefusal.UNKNOWN_ORIGIN_PATH


def test_the_module_reads_no_reachability_list() -> None:
    """Structural guard for condition 1. Config is the only source of approval authority.

    The check inspects imports and attribute names rather than prose. The module docstring
    names both lists to explain why it must not read them, and a text grep would punish that.
    """
    source = (Path(__file__).parents[2] / "nanoinfra/gates/approvals.py").read_text()
    tree = ast.parse(source)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = [name for name in imported if "channels" in name or "pairing" in name]

    accessed = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)} | {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    reachability = {"allow_from", "allowFrom", "is_allowed", "is_approved", "generate_code"}

    assert forbidden == []
    assert accessed & reachability == set()
