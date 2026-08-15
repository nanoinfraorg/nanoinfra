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
3. The approval path differs from the origin path of the request. ``gates.identityIndependence``
   adds a second way to satisfy this condition, and it is off by default. See below.

**Identity independence, and what a deployment gives up (#47, item 11).** Condition 3 compares
path strings, so two people behind one proxy are one path and neither can answer the other's
request. With real identities that is the wrong answer. ``gates.identityIndependence`` therefore
lets a different person on the origin path answer, and the flag defaults to ``false``.

The reason for the flag is not caution. The origin identity is an assertion of the agent.
``nanoinfra/gates/executor/protocol.py`` says so about the origin path already, and
``origin_actor`` inherits that exactly. So a compromised agent can claim that the request came
from another person, and one genuine human approval then substitutes for two authenticated paths.
The human still reads the command and the host list that the executor rendered, which stays the
real protection. But the deployment gives up the property that one compromised account cannot
hold both halves.

When the flag is ``true``:

- An answer counts when the answering identity differs from the origin identity, **or** when the
  answering path differs from the origin path. Either one is enough.
- An answer never counts when both are the same. Self-approval stays impossible in every mode.
- An unknown origin identity falls back to the path rule alone. A missing value is never a
  wildcard that matches every person.

The identity comparison decides an outcome in one case only: the answer arrived on the origin
path. On a second path the path half already permits the answer. So both identities always come
from one path when the comparison matters, which means they share one vocabulary and the compare
is meaningful.

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
    # The same person answered on the origin path, with identity independence on. It is a name of
    # its own and not a reuse of SAME_PATH, because #27 and #43 map a refusal name to an
    # instruction an operator can act on. "Add a second path" answers SAME_PATH. It answers
    # nothing here, where the fix is another person.
    SAME_ACTOR_AND_PATH = "same_actor_and_path"


@dataclass(frozen=True, slots=True)
class ApprovalCheck:
    """The typed outcome of one check. ``ok`` is never the only thing a caller learns.

    The type carries no ``__bool__`` on purpose. ``if check_approval(...):`` would read as a
    gate and would throw away the reason and the record fields.

    ``same_path`` reports the same-path case even when the policy permits it. A future policy
    may relax condition 3, and two configured paths may share one credential. A reviewer needs
    to see both paths in the record, so both travel on the result.

    ``origin_actor`` and ``same_actor`` carry the identity half for the same reason.
    ``gates.identityIndependence`` permits an answer on the origin path, so a reviewer must be
    able to see which two people held the two halves. ``same_actor`` stays false for a request
    that named nobody, because it reports a fact and an absent value matches nothing.
    """

    ok: bool
    origin_path: str
    approval_path: str
    sender: str
    same_path: bool
    origin_actor: str = ""
    same_actor: bool = False
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
            "origin_actor": self.origin_actor,
            "same_actor": self.same_actor,
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


def identity_may_substitute(gates: GatesConfig, *, origin_actor: str) -> bool:
    """Whether a second person on the origin path can replace the second path.

    Two facts must hold together. The operator turned ``gates.identityIndependence`` on, and the
    request names the person the origin path authenticated. A request that names nobody keeps
    the path rule alone, because an absent value must never behave as a wildcard that matches
    every person. Without the second fact the rule would relax itself for free: every request
    from a channel that authenticates nobody would gain an answering path it did not earn.
    """
    return gates.identity_independence and bool(origin_actor.strip())


def answer_is_independent(
    gates: GatesConfig,
    *,
    origin_path: str,
    origin_actor: str,
    approval_path: str,
    sender: str,
) -> bool:
    """Whether one answer is independent of the request it answers (condition 3).

    The path half comes first and it decides most cases. The identity half decides one case
    only: the answer arrived on the origin path. So both identities come from one path whenever
    the compare matters, and they therefore share one vocabulary.

    The identity compare is exact, for the reason ``_is_approver`` gives. A case-folded or
    truncated compare would read one person as two, and self-approval would then pass.

    Condition 1 runs before this, so a blank sender never reaches here from ``check_approval``.
    The two callers that ask this question directly skip a blank sender themselves.
    """
    origin = origin_path.strip()
    path = approval_path.strip()
    if not origin:
        # No origin path means no proof of anything. The caller refuses on its own.
        return False
    if path != origin:
        return True
    if not identity_may_substitute(gates, origin_actor=origin_actor):
        return False
    return sender.strip() != origin_actor.strip()


def approval_feasible(
    *, gates: GatesConfig, origin_path: str, origin_actor: str = ""
) -> ApprovalFeasibility:
    """Say whether one approver could answer a request that arrived on *origin_path*.

    The test is the three conditions of #13, minus the identity that has not answered yet. One
    approver must sit on one authenticated path, and that approver must be able to satisfy
    condition 3. The order of the checks matches ``check_approval``, so the two functions never
    disagree about which sentence an operator reads.

    ``origin_actor`` is the person the origin path authenticated, and it is blank when the
    channel authenticated nobody. It matters here as well as in the check: with identity
    independence on, a single-path deployment with two people can answer, and a feasibility test
    that ignored the flag would refuse the action before anybody saw it.
    """
    origin = origin_path.strip()
    who_origin = origin_actor.strip()
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
    answering = _answering_paths(gates, origin=origin, origin_actor=who_origin)
    if not answering:
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
            if approver.channel.strip() in answering
            and approver.sender.strip()
            and answer_is_independent(
                gates,
                origin_path=origin,
                origin_actor=who_origin,
                approval_path=approver.channel.strip(),
                sender=approver.sender.strip(),
            )
        }
    )
    if not reachable:
        return ApprovalFeasibility(
            ok=False,
            refusal=ApprovalRefusal.NOT_AN_APPROVER,
            reason=(
                "gates.approvers lists nobody who may answer this request, so it would wait for "
                f"nothing. gates.approvalPaths lists {authenticated!r}, and the request arrived "
                f"on {origin!r}. Add an approver on another path, or declare a standing grant."
            ),
        )

    return ApprovalFeasibility(ok=True)


def check_approval(
    *,
    gates: GatesConfig,
    origin_path: str,
    approval_path: str,
    sender: str,
    origin_actor: str = "",
) -> ApprovalCheck:
    """Decide whether one approval counts for a request that arrived on *origin_path*.

    ``sender`` is the identity the approval path authenticated. The channel half of the
    approver match comes from ``approval_path`` and never from a separate argument. A caller
    that could name any channel beside any path would break the binding between the identity
    and the transport that carried the approval.

    ``origin_actor`` is the person the origin path authenticated, and it is blank when the
    channel authenticated nobody. It is the agent's assertion about itself, so it can only ever
    relax condition 3 behind ``gates.identityIndependence``. The module docstring states what a
    deployment gives up when it turns that flag on.

    The checks run in a fixed order, because the order decides which sentence the operator
    reads. The deployment-level facts come first. A missing origin path and a deployment with
    one path both mean that no correct approval exists, so those two answers must not hide
    behind an identity mismatch. The remaining three follow the order of the conditions in
    #13.
    """
    origin = origin_path.strip()
    path = approval_path.strip()
    who = sender.strip()
    who_origin = origin_actor.strip()
    # Two blank values are not one path. A blank origin fails closed below on its own.
    same_path = bool(origin) and origin == path
    # The same rule for the identity half. An absent value names nobody. So two absent values are
    # not one person, and the record must not report them as one.
    same_actor = bool(who_origin) and who_origin == who

    def refuse(refusal: ApprovalRefusal, reason: str) -> ApprovalCheck:
        return ApprovalCheck(
            ok=False,
            origin_path=origin,
            approval_path=path,
            sender=who,
            same_path=same_path,
            origin_actor=who_origin,
            same_actor=same_actor,
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
    if not _answering_paths(gates, origin=origin, origin_actor=who_origin):
        return refuse(
            ApprovalRefusal.NO_SECOND_PATH,
            f"{NO_SECOND_PATH_REASON}. gates.approvalPaths lists {authenticated!r}, and the "
            f"request arrived on {origin!r}.",
        )

    if not _is_approver(gates, path=path, sender=who):
        # The refusal names the sender that failed and never an approver. #43 set that shape.
        # The failed sender is already in the hands of whoever sent it, so naming it costs
        # nothing and it tells an operator with a misconfigured proxy which identity arrived. A
        # refusal that listed the approver set would tell an attacker who to impersonate.
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

    if not answer_is_independent(
        gates,
        origin_path=origin,
        origin_actor=who_origin,
        approval_path=path,
        sender=who,
    ):
        if same_actor and identity_may_substitute(gates, origin_actor=who_origin):
            # The identity rule was live, and the person is what failed. So the refusal carries
            # its own name. An operator who read "same path" here would add a second path, and
            # that fixes nothing: the same person would still hold both halves.
            #
            # With the flag off the answer fails on the path alone, and a different person on
            # that path fails the same way. The instruction is then "add a second path", which is
            # what SAME_PATH already says, so this branch must not take that case.
            return refuse(
                ApprovalRefusal.SAME_ACTOR_AND_PATH,
                f"the same person {who!r} raised this request and answered it, on the one path "
                f"{origin!r}. gates.identityIndependence permits a different person on that "
                "path. It permits self-approval in no mode.",
            )
        reason = (
            f"the approval arrived on the request origin path {origin!r}. A different "
            "authenticated path must deliver it."
        )
        if gates.identity_independence and not who_origin:
            # The flag is on and the request named nobody, so the identity half could not
            # decide. An operator who turned the flag on must read why it did nothing here.
            reason += (
                " gates.identityIndependence is on, and the request names no origin identity, "
                "so the path rule alone decides."
            )
        return refuse(ApprovalRefusal.SAME_PATH, reason)

    return ApprovalCheck(
        ok=True,
        origin_path=origin,
        approval_path=path,
        sender=who,
        same_path=same_path,
        origin_actor=who_origin,
        same_actor=same_actor,
    )


def _authenticated_paths(gates: GatesConfig) -> list[str]:
    """The configured paths, without blanks. A blank entry authenticates nobody.

    Config order survives, because the order tells an operator which path the deployment
    prefers when the refusal names the list.
    """
    return [entry.strip() for entry in gates.approval_paths if entry.strip()]


def _answering_paths(gates: GatesConfig, *, origin: str, origin_actor: str) -> list[str]:
    """The authenticated paths an approval could arrive on for a request from *origin*.

    The origin path joins the list when identity independence is live, because a second person
    on that one path is then enough. A single-path deployment therefore keeps its
    ``NO_SECOND_PATH`` refusal until an operator turns the flag on and the request names a
    person.

    Config order survives, for the reason ``_authenticated_paths`` gives.
    """
    authenticated = _authenticated_paths(gates)
    if identity_may_substitute(gates, origin_actor=origin_actor):
        return authenticated
    return [candidate for candidate in authenticated if candidate != origin]


def _is_approver(gates: GatesConfig, *, path: str, sender: str) -> bool:
    """Match ``gates.approvers`` on the path that carried the approval (condition 1).

    The comparison is exact on the whole string: no prefix stripping, no case folding, and no
    substring test. An authority list that matched loosely would be a different list from the
    one an operator read in the git review, and the review is the only control over this list.
    The value is not a secret, so a plain compare is enough and a constant-time compare would
    mislead a reader.

    A sender is one opaque token, so ``webui:alberto@example.com`` needs no new syntax and no
    parsing here (#47, item 9). The prefix keeps the path inside the identity, which is what
    stops a chat sender of the same text from reading as the same person. The gate never splits
    it: ``webui`` is the actor of a deployment with no proxy, and a prefix match would let that
    shared token approve for a named person.

    A blank entry names nobody, so it matches nobody. Both halves must carry text. Without this
    an approver entry with a blank sender would match a blank ``actor`` from the operator wire,
    which carries the field as a plain string. ``approval_feasible`` already skips a blank
    sender, so a match here would also let an answer count on a deployment whose feasibility
    test said that nobody could answer.
    """
    if not path or not sender:
        return False
    return any(
        approver.channel.strip() == path and approver.sender.strip() == sender
        for approver in gates.approvers
    )


__all__ = [
    "NO_SECOND_PATH_REASON",
    "ApprovalCheck",
    "ApprovalFeasibility",
    "ApprovalRefusal",
    "answer_is_independent",
    "approval_feasible",
    "check_approval",
    "identity_may_substitute",
]
