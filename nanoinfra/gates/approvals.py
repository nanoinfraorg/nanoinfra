"""Path independence for an approval -- nanoinfraorg/nanoinfra#13.

One path raises the request. A different authenticated path delivers the approval. The same
operator is acceptable. The same credential and the same transport are not acceptable. A
requester who approves on the origin channel is single-factor by construction, because one
compromised account then yields both halves.

Three conditions hold together:

1. The approver matches ``gates.approvers`` (#7). Membership in a channel ``allowFrom`` list
   grants nothing, and membership in the pairing store grants nothing. Those lists carry
   reachability. The pairing store is also mutable at runtime from chat, and an injected
   instruction attacks a runtime-mutable list first. This module therefore imports nothing
   from nanoinfra/channels or nanoinfra/pairing, and a test asserts that fact.
2. The approval path authenticates the approver, which means the path is in
   ``gates.approvalPaths``.
3. The approval path differs from the origin path of the request.

The single-path deployment is the important case. Path independence needs a second path to
exist. A WebUI-only install is a common single-operator deployment, and it has one path. Such
a deployment has no runtime approval path for an unusual group action. That outcome is a
design consequence and not an accident, so the refusal names the missing second path. An
operator who reads "no second authenticated path is configured. Add one, or declare a
standing grant" can act. An operator who reads "denied" files a bug.

A path here is one transport, and the name matches ``Approver.channel``. Two chats on one
channel are one path, because they share the credential and the transport.

The module decides and records nothing. It opens no transport, it reads no clock, and it
imports no audit code. #16 owns the record, so the same-path fact travels back as a field on
the result. #8 and #27 own the wiring at the point that dials.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from nanoinfra.config.gates import GatesConfig

# The sentence a single-path deployment must read. It is a constant because the wording is the
# feature. A generic denial sends the operator to the issue tracker instead of the config.
NO_SECOND_PATH_REASON = (
    "no second authenticated path is configured. Add one, or declare a standing grant"
)


class ApprovalRefusal(StrEnum):
    """Why the approval did not count. #13 renders this and #16 records it, so it is a value.

    A bare ``False`` would force every caller to guess between a wrong identity, a path that
    authenticates nobody, and a deployment with one path. Those are three operator actions.
    """

    UNKNOWN_ORIGIN_PATH = "unknown_origin_path"
    NO_SECOND_PATH = "no_second_path"
    NOT_AN_APPROVER = "not_an_approver"
    UNAUTHENTICATED_PATH = "unauthenticated_path"
    SAME_PATH = "same_path"


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """The typed outcome of one check. ``ok`` is never the only thing a caller learns.

    The type carries no ``__bool__`` on purpose. ``if check_approval(...):`` would read as a
    gate and would throw away the reason and the record fields.

    ``same_path`` reports the same-path case even when the policy permits it. A future policy
    may relax condition 3, and two configured paths may share one credential. A reviewer needs
    to see both paths in the record, so both travel on the result.
    """

    ok: bool
    origin_path: str
    approval_path: str
    sender: str
    same_path: bool
    refusal: ApprovalRefusal | None = None
    reason: str = ""

    def audit_fields(self) -> dict[str, str | bool | None]:
        """The fields #16 records. The module writes no record of its own.

        The prose reason stays out. An operator reads it once, and a record that keeps two
        wordings of one decision drifts as the wording changes.
        """
        return {
            "ok": self.ok,
            "origin_path": self.origin_path,
            "approval_path": self.approval_path,
            "sender": self.sender,
            "same_path": self.same_path,
            "refusal": self.refusal.value if self.refusal is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ApprovalFeasibility:
    """Whether any correct approval can exist for one request -- #38.

    #38 asks this before it suspends an action. A suspended action that nobody may answer is a
    hang, and a hang teaches an operator to widen policy for the wrong reason. So the executor
    refuses at once and names the missing piece.

    The type carries no identity fields. No answer has arrived yet, so no actor and no approval
    path exist to report.
    """

    ok: bool
    refusal: ApprovalRefusal | None = None
    reason: str = ""


def approval_feasible(*, gates: GatesConfig, origin_path: str) -> ApprovalFeasibility:
    """Say whether one approver could answer a request that arrived on *origin_path*.

    The test is the three conditions of #13, minus the identity that has not answered yet. One
    approver must sit on one authenticated path that is not the origin path. The order of the
    checks matches ``check_approval``, so the two functions never disagree about which sentence
    an operator reads.
    """
    origin = origin_path.strip()
    if not origin:
        return ApprovalFeasibility(
            ok=False,
            refusal=ApprovalRefusal.UNKNOWN_ORIGIN_PATH,
            reason=(
                "the request names no origin path. Path independence is not provable, so no "
                "approval can count."
            ),
        )

    authenticated = _authenticated_paths(gates)
    second_paths = [candidate for candidate in authenticated if candidate != origin]
    if not second_paths:
        return ApprovalFeasibility(
            ok=False,
            refusal=ApprovalRefusal.NO_SECOND_PATH,
            reason=(
                f"{NO_SECOND_PATH_REASON}. gates.approvalPaths lists {authenticated!r}, and the "
                f"request arrived on {origin!r}."
            ),
        )

    reachable = sorted(
        {
            approver.channel.strip()
            for approver in gates.approvers
            if approver.channel.strip() in second_paths and approver.sender.strip()
        }
    )
    if not reachable:
        return ApprovalFeasibility(
            ok=False,
            refusal=ApprovalRefusal.NOT_AN_APPROVER,
            reason=(
                "gates.approvers lists nobody on a second authenticated path, so nobody may "
                f"answer this request. gates.approvalPaths lists {authenticated!r}, and the "
                f"request arrived on {origin!r}. Add an approver on another path, or declare a "
                "standing grant."
            ),
        )

    return ApprovalFeasibility(ok=True)


def check_approval(
    *,
    gates: GatesConfig,
    origin_path: str,
    approval_path: str,
    sender: str,
) -> ApprovalCheck:
    """Decide whether one approval counts for a request that arrived on *origin_path*.

    ``sender`` is the identity the approval path authenticated. The channel half of the
    approver match comes from ``approval_path`` and never from a separate argument. A caller
    that could name any channel beside any path would break the binding between the identity
    and the transport that carried the approval.

    The checks run in a fixed order, because the order decides which sentence the operator
    reads. The deployment-level facts come first. A missing origin path and a deployment with
    one path both mean that no correct approval exists, so those two answers must not hide
    behind an identity mismatch. The remaining three follow the order of the conditions in
    #13.
    """
    origin = origin_path.strip()
    path = approval_path.strip()
    who = sender.strip()
    # Two blank values are not one path. A blank origin fails closed below on its own.
    same_path = bool(origin) and origin == path

    def refuse(refusal: ApprovalRefusal, reason: str) -> ApprovalCheck:
        return ApprovalCheck(
            ok=False,
            origin_path=origin,
            approval_path=path,
            sender=who,
            same_path=same_path,
            refusal=refusal,
            reason=reason,
        )

    if not origin:
        return refuse(
            ApprovalRefusal.UNKNOWN_ORIGIN_PATH,
            "the request names no origin path. Path independence is not provable, so the "
            "approval does not count.",
        )

    authenticated = _authenticated_paths(gates)
    if not [candidate for candidate in authenticated if candidate != origin]:
        return refuse(
            ApprovalRefusal.NO_SECOND_PATH,
            f"{NO_SECOND_PATH_REASON}. gates.approvalPaths lists {authenticated!r}, and the "
            f"request arrived on {origin!r}.",
        )

    if not _is_approver(gates, path=path, sender=who):
        return refuse(
            ApprovalRefusal.NOT_AN_APPROVER,
            f"{who!r} on path {path!r} is not in gates.approvers. A channel allowFrom list "
            "and the pairing store grant no approval authority.",
        )

    if path not in authenticated:
        return refuse(
            ApprovalRefusal.UNAUTHENTICATED_PATH,
            f"path {path!r} is not in gates.approvalPaths, so it authenticates no approver.",
        )

    if same_path:
        return refuse(
            ApprovalRefusal.SAME_PATH,
            f"the approval arrived on the request origin path {origin!r}. A different "
            "authenticated path must deliver it.",
        )

    return ApprovalCheck(
        ok=True,
        origin_path=origin,
        approval_path=path,
        sender=who,
        same_path=same_path,
    )


def _authenticated_paths(gates: GatesConfig) -> list[str]:
    """The configured paths, without blanks. A blank entry authenticates nobody.

    Config order survives, because the order tells an operator which path the deployment
    prefers when the refusal names the list.
    """
    return [entry.strip() for entry in gates.approval_paths if entry.strip()]


def _is_approver(gates: GatesConfig, *, path: str, sender: str) -> bool:
    """Match ``gates.approvers`` on the path that carried the approval (condition 1).

    The comparison is exact. A sender id is an opaque token, so a case-folded match or a
    prefix match would let a lookalike identity approve. The value is not a secret, so a
    plain compare is enough and a constant-time compare would mislead a reader.
    """
    return any(
        approver.channel.strip() == path and approver.sender.strip() == sender
        for approver in gates.approvers
    )


__all__ = [
    "NO_SECOND_PATH_REASON",
    "ApprovalCheck",
    "ApprovalFeasibility",
    "ApprovalRefusal",
    "approval_feasible",
    "check_approval",
]
