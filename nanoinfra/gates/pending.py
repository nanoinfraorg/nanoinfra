"""One suspended action, and the wait for a human answer -- nanoinfraorg/nanoinfra#38.

#12 binds a token to one action. #13 decides whether an approval counts. #14 renders the
payload a human reads. All three shipped as pure functions, and nothing created a pending
approval, so an ``approve`` outcome executed. This module holds the state that closes that gap.

**The store lives in the executor, and it lives in memory.** The executor is the authority
(#18), so the record of a suspended action belongs beside the decision. Memory is not a
shortcut. An unanswered action must die with the process, because a restart that resurrects an
approvable action is the opposite of fail closed. The agent can cause a restart, so a durable
pending record would hand the agent a retry that no human sees.

**The wait blocks, and the caller does not poll.** Four reasons, and each one stands alone.

1. The wire carries one request kind. A poll needs a new kind plus retry state in the tool.
2. A tool call is already a long await. A remote command runs for minutes under an idle timeout.
3. A blocked call keeps the digest bound to the action, with no extra state. The connection that
   submitted the action also executes it.
4. A poll would re-submit and re-resolve the action. That is the redirect #23 and #24 close.

The cost is one held connection per pending approval. ``serve_forever`` therefore serves each
connection in its own thread. A blocked approval that stopped every other action would be a
denial of service on the whole agent.

**The clock is monotonic, and it is not injectable.** ``threading.Condition.wait`` takes a real
timeout, so a fake clock would produce a real sleep of a fake length. A wall clock would let an
NTP step extend or void a live deadline, which is the rule #12 already applies to a token TTL.

The module decides nothing. It reads no policy, it verifies no identity, and it writes no audit
record. ``nanoinfra/gates/approvals.py`` decides whether an answer counts, and the executor
records every state change.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

# How long an answered or expired record stays after its deadline. The record survives so a late
# answer reads "already answered" or "expired" instead of "no such request". Those words describe
# different events to an operator, and #12 keeps a spent token for the same reason.
_RETENTION_S = 900.0

# The wording an expired action carries. It is a constant because an operator reads it, and
# "expired" alone does not say what to do next.
EXPIRY_REASON = (
    "no operator answered before the deadline, so the action expired. "
    "Ask again when an approver is present, or declare a standing grant"
)


class ApprovalState(StrEnum):
    """What happened to one suspended action. #16 records the value, so it is a value."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"


class AnswerRefusal(StrEnum):
    """Why an answer did not land. The operator surface renders this, so it is a value.

    A bare ``False`` would force the caller to guess between a stale request, a second
    answer, and an answer that describes other bytes. Those are three operator messages.
    """

    UNKNOWN_REQUEST = "unknown_request"
    ALREADY_ANSWERED = "already_answered"
    EXPIRED = "expired"
    DIGEST_MISMATCH = "digest_mismatch"


@dataclass(frozen=True, slots=True)
class PendingApproval:
    """One action that waits for a human.

    ``payload`` holds the exact bytes #14 rendered, and ``target_digest`` binds them. The two
    fields travel together, so an operator surface never renders a summary of its own.

    ``command`` and ``hosts`` are the resolver's output. They stay here because the executor
    runs the command after the answer, and because #12 binds the digest to both.

    ``origin_actor`` names the person the origin path authenticated, and it is blank when the
    channel authenticated nobody. ``gates.identityIndependence`` reads it when an answer arrives
    (#47, item 11). The field sits last because a dataclass takes its defaults last, and every
    caller passes it by keyword.
    """

    request_id: str
    session_id: str
    origin_path: str
    execution_context: str
    capability_class: str
    scope: str
    hosts: tuple[str, ...]
    command: str
    payload: str
    target_digest: str
    created_at: float
    expires_at: float
    origin_actor: str = ""

    @property
    def host_count(self) -> int:
        """How many hosts this action reaches. An operator reads the count and the names."""
        return len(self.hosts)


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """How one wait ended.

    ``token_nonce`` is present for an approval and absent for every other state. The nonce
    never crosses the wire to the agent, because a token the agent can read is a token the
    model can propose (#12).
    """

    state: ApprovalState
    actor: str | None = None
    approval_path: str | None = None
    token_nonce: str | None = None
    reason: str = ""


@dataclass(slots=True)
class _Record:
    """The mutable half. The caller holds the frozen ``PendingApproval`` instead."""

    approval: PendingApproval
    outcome: ApprovalOutcome = field(
        default_factory=lambda: ApprovalOutcome(state=ApprovalState.PENDING)
    )


class PendingApprovalStore:
    """Every action that waits for an operator, keyed by request id.

    One lock guards the whole store, and one condition wakes the waiters. The read of a state
    and the write of that state happen together, so one answer means one answer.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._records: dict[str, _Record] = {}
        self._waiters = 0

    def create(
        self,
        *,
        session_id: str,
        origin_path: str,
        execution_context: str,
        capability_class: str,
        scope: str,
        hosts: tuple[str, ...],
        command: str,
        payload: str,
        target_digest: str,
        timeout_s: float,
        origin_actor: str = "",
    ) -> PendingApproval:
        """Register one suspended action and return its record.

        ``timeout_s`` is the whole window a human gets. The executor reads it from
        ``gates.approvalTimeoutS``, so an operator owns the length of the wait.

        ``origin_actor`` defaults to blank, which reads as "the channel authenticated nobody".
        That is the fail-closed value: #13 then judges the answer by the path rule alone.
        """
        if timeout_s <= 0.0:
            raise ValueError("a pending approval needs a positive timeout")
        now = time.monotonic()
        approval = PendingApproval(
            request_id=uuid.uuid4().hex,
            session_id=session_id,
            origin_path=origin_path,
            origin_actor=origin_actor,
            execution_context=execution_context,
            capability_class=capability_class,
            scope=scope,
            hosts=tuple(hosts),
            command=command,
            payload=payload,
            target_digest=target_digest,
            created_at=now,
            expires_at=now + timeout_s,
        )
        with self._condition:
            self._records[approval.request_id] = _Record(approval=approval)
            self._prune(keep=approval.request_id)
        return approval

    def get(self, request_id: str) -> PendingApproval | None:
        """Return one record, whatever its state. The operator surface reads the payload here."""
        with self._condition:
            record = self._records.get(request_id)
            return None if record is None else record.approval

    def pending(self) -> tuple[PendingApproval, ...]:
        """Return the actions an operator can still answer, oldest first.

        An action past its deadline is absent. Its waiter marks it expired, and a list that
        still showed it would invite an answer that cannot land.
        """
        now = time.monotonic()
        with self._condition:
            live = [
                record.approval
                for record in self._records.values()
                if record.outcome.state is ApprovalState.PENDING and now < record.approval.expires_at
            ]
        return tuple(sorted(live, key=lambda approval: approval.created_at))

    def waiter_count(self) -> int:
        """How many threads wait right now. A test synchronises on this."""
        with self._condition:
            return self._waiters

    def approve(
        self,
        *,
        request_id: str,
        actor: str,
        approval_path: str,
        token_nonce: str,
        target_digest: str,
    ) -> AnswerRefusal | None:
        """Accept an approval, or name the reason it did not land.

        ``target_digest`` is the digest of the payload the operator read. A mismatch refuses and
        leaves the action pending, so an answer about other bytes authorizes nothing and costs
        nothing.
        """
        with self._condition:
            record = self._records.get(request_id)
            if record is None:
                return AnswerRefusal.UNKNOWN_REQUEST
            refusal = self._answerable(record)
            if refusal is not None:
                return refusal
            if record.approval.target_digest != target_digest:
                return AnswerRefusal.DIGEST_MISMATCH
            record.outcome = ApprovalOutcome(
                state=ApprovalState.APPROVED,
                actor=actor,
                approval_path=approval_path,
                token_nonce=token_nonce,
            )
            self._condition.notify_all()
            return None

    def deny(
        self, *, request_id: str, actor: str, approval_path: str, reason: str
    ) -> AnswerRefusal | None:
        """Accept a denial, or name the reason it did not land.

        A denial carries no digest. An approval authorizes bytes, and a denial stops an action,
        so the request id names enough for the second case.
        """
        with self._condition:
            record = self._records.get(request_id)
            if record is None:
                return AnswerRefusal.UNKNOWN_REQUEST
            refusal = self._answerable(record)
            if refusal is not None:
                return refusal
            record.outcome = ApprovalOutcome(
                state=ApprovalState.DENIED,
                actor=actor,
                approval_path=approval_path,
                reason=reason,
            )
            self._condition.notify_all()
            return None

    def wait(self, request_id: str) -> ApprovalOutcome:
        """Block until an operator answers, or until the deadline passes.

        Raises ``KeyError`` for an unknown request id. The executor waits on a record it just
        created, so an unknown id there is a defect rather than a stale answer.

        The deadline transition happens here, in the thread that owns the action. No reaper
        thread exists, because a waiter is always present for a live record.
        """
        with self._condition:
            record = self._records.get(request_id)
            if record is None:
                raise KeyError(request_id)
            self._waiters += 1
            try:
                while record.outcome.state is ApprovalState.PENDING:
                    remaining = record.approval.expires_at - time.monotonic()
                    if remaining <= 0.0:
                        record.outcome = ApprovalOutcome(
                            state=ApprovalState.EXPIRED, reason=EXPIRY_REASON
                        )
                        break
                    self._condition.wait(remaining)
            finally:
                self._waiters -= 1
            return record.outcome

    def _prune(self, *, keep: str) -> None:
        """Drop records that explain nothing any more. The caller holds the lock.

        A long-lived executor would otherwise hold one record per suspended action forever.
        Removal waits until well after the deadline, so a late answer still reads the right word.
        """
        cutoff = time.monotonic() - _RETENTION_S
        stale = [
            request_id
            for request_id, record in self._records.items()
            if record.approval.expires_at < cutoff and request_id != keep
        ]
        for request_id in stale:
            del self._records[request_id]

    @staticmethod
    def _answerable(record: _Record) -> AnswerRefusal | None:
        """Say whether one record can still take an answer. The caller holds the lock.

        The order decides the message an operator reads. "Already answered" describes a
        decision somebody took. "Expired" describes a decision nobody took in time.
        """
        if record.outcome.state is not ApprovalState.PENDING:
            if record.outcome.state is ApprovalState.EXPIRED:
                return AnswerRefusal.EXPIRED
            return AnswerRefusal.ALREADY_ANSWERED
        if time.monotonic() >= record.approval.expires_at:
            # The waiter has not run yet, and the deadline already passed. The answer must not
            # land, because the action is on its way to a refusal.
            return AnswerRefusal.EXPIRED
        return None


__all__ = [
    "EXPIRY_REASON",
    "AnswerRefusal",
    "ApprovalOutcome",
    "ApprovalState",
    "PendingApproval",
    "PendingApprovalStore",
]
