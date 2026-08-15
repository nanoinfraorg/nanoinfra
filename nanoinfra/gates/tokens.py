"""Approval tokens -- nanoinfraorg/nanoinfra#12.

One approval binds to one resolved action. An authenticated blob that names no session, no
actor, and no target is replayable, and replayability is the vulnerability. So a token here
carries the binding, and the store keeps the authority.

Five rules, and each rule closes one hole:

1. The token has one use, and the *execution* spends it. Issuance must not spend it, because
   a token that issuance spends leaves the window between the approval and the transport
   with no authorization in force.
2. The TTL is short. A human needs time to read a rendered host list, and no longer. Ambient
   authority must not develop out of an approval nobody spent.
3. ``target_digest`` covers the resolved command and the resolved host set, after variable
   expansion. An approval of ``systemctl restart $SVC`` plus an execution of the expansion is
   the replay hole.
4. Any change needs a new approval. A wider host set, another command, or another session all
   fail verification. "Approve once and reuse across the plan" does not exist.
5. Approval state lives in this store, outside model-visible context. The model can propose
   any token the model can read, so a proposed field is an input and never the authority.

The module is a library on purpose. It opens no transport and reads no policy. #8 and #13 own
enforcement at the point that dials, because ``AgentHook.before_execute_tool`` returns
``None`` and cannot deny a call. The store also does not match ``actor`` against
``gates.approvers``: the token records who approved, and the gate decides whether that
identity may. One module that both records and decides would hide the decision.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from nanoinfra.agent.tools.capabilities import command_digest

# Tens of seconds to low minutes. The ceiling is a hard refusal and not advice, because a
# caller that may pass a long TTL can buy ambient authority one issue at a time.
DEFAULT_TTL_S = 120.0
MAX_TTL_S = 300.0

# 32 bytes of CSPRNG output. ``secrets`` and never ``random``: the nonce is the bearer value,
# so a predictable nonce bypasses every other rule in this file.
_NONCE_BYTES = 32

# How long a spent or expired record stays after its expiry. The record survives so a late
# execution learns "expired" or "already used" instead of "unknown". #13 renders the reason
# and #16 records it, and "unknown" would describe a forgery rather than a stale approval.
_REASON_RETENTION_S = 900.0


class TokenRefusal(StrEnum):
    """Why verification failed. #13 renders this and #16 records it, so it is a value.

    A bare ``False`` would force both callers to guess between a forgery, a stale approval,
    and a changed target, which are three different operator messages.
    """

    UNKNOWN_TOKEN = "unknown_token"
    ALREADY_USED = "already_used"
    EXPIRED = "expired"
    WRONG_SESSION = "wrong_session"
    DIGEST_MISMATCH = "digest_mismatch"


@dataclass(frozen=True, slots=True)
class ApprovalToken:
    """One approval, bound to one resolved action.

    ``capability_class`` spells out what ``class`` would name, because ``class`` is a Python
    keyword. The vocabulary lives in nanoinfra/agent/tools/capabilities.py (#3) and the scope
    tiers come from #4, so this module validates neither and stores what the gate resolved.

    ``issued_at`` and ``expires_at`` read from the store's clock, which is monotonic by
    default. A wall clock would let an NTP step extend or void a live approval.
    """

    session_id: str
    actor: str
    origin_path: str
    approval_path: str
    target_digest: str
    capability_class: str
    scope: str
    issued_at: float
    expires_at: float
    nonce: str

    def audit_fields(self) -> dict[str, str | float]:
        """Every field except the nonce, for the record in #16.

        The audit log is readable by more people than the approval path is. A nonce in that
        log is a bearer value in a second place, so the record names the approval and omits
        the means to spend it.
        """
        return {
            "session_id": self.session_id,
            "actor": self.actor,
            "origin_path": self.origin_path,
            "approval_path": self.approval_path,
            "target_digest": self.target_digest,
            "capability_class": self.capability_class,
            "scope": self.scope,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class TokenVerification:
    """The typed outcome of a check. ``ok`` is never the only thing a caller learns.

    The type carries no ``__bool__`` on purpose. ``if store.verify(...):`` would read as a
    gate and would throw away the reason that #13 and #16 both need.
    """

    ok: bool
    refusal: TokenRefusal | None = None
    token: ApprovalToken | None = None


@dataclass(slots=True)
class _Record:
    """The server-side half. ``used`` lives here and never on the token the caller holds."""

    token: ApprovalToken
    used: bool = False


def compute_target_digest(*, command: str, hosts: Iterable[str]) -> str:
    """Digest the resolved command together with the resolved host set (rule 3).

    Callers pass the expansion, never the template, and pass resolved targets rather than
    inventory labels. #24 compares resolved targets because an inventory write can repoint a
    label at another address.

    The command part reuses ``command_digest`` from capabilities.py. A second hasher here
    would drift from the one the audit record uses, and two digests of one command is a way
    to make an approval and a record disagree.

    Hosts sort and deduplicate, so two renders of one host set produce one digest and a
    reordered list does not force a second approval. The payload is JSON, so a newline or a
    separator inside a host name cannot forge another host set.

    An empty host set raises. No resolved host means no target to bind to, and a token bound
    to nothing would verify against anything.
    """
    resolved = sorted(set(hosts))
    if not resolved:
        raise ValueError("target_digest needs at least one resolved host")
    payload = json.dumps([command_digest(command), resolved], separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


class ApprovalTokenStore:
    """Server-side approval state, keyed by nonce (rule 5).

    Nothing reconstructs a token from model-visible context. A caller proposes a nonce, and
    the answer comes from the record this store already holds.

    The store is in-process and holds no secret at rest, so it needs no persistence. A
    restart drops pending approvals, which fails closed: the operator approves again.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        ttl_s: float = DEFAULT_TTL_S,
    ) -> None:
        """``now`` is injectable so an expiry test moves the clock instead of sleeping.

        The default is ``time.monotonic`` and not ``time.time``. A TTL that a wall clock
        drives moves when the clock moves, and a backward step would revive an expired
        approval.
        """
        _check_ttl(ttl_s)
        self._now = now
        self._ttl_s = ttl_s
        self._records: dict[str, _Record] = {}
        # The gateway runs turns concurrently. One use has to mean one use, so the read of
        # ``used`` and the write of ``used`` happen under one lock.
        self._lock = threading.Lock()

    def issue(
        self,
        *,
        session_id: str,
        actor: str,
        origin_path: str,
        approval_path: str,
        target_digest: str,
        capability_class: str,
        scope: str,
        ttl_s: float | None = None,
    ) -> ApprovalToken:
        """Mint a token for one resolved action. Issuance does not spend it (rule 1).

        ``target_digest`` comes from ``compute_target_digest`` on the *resolved* command and
        host set. This method cannot check that, which is why #8 computes the digest at the
        same point it opens the transport.
        """
        effective_ttl = self._ttl_s if ttl_s is None else ttl_s
        _check_ttl(effective_ttl)
        issued_at = self._now()
        token = ApprovalToken(
            session_id=session_id,
            actor=actor,
            origin_path=origin_path,
            approval_path=approval_path,
            target_digest=target_digest,
            capability_class=capability_class,
            scope=scope,
            issued_at=issued_at,
            expires_at=issued_at + effective_ttl,
            nonce=secrets.token_urlsafe(_NONCE_BYTES),
        )
        with self._lock:
            self._records[token.nonce] = _Record(token=token)
            self._prune(keep=token.nonce)
        return token

    def verify(
        self, *, nonce: str, session_id: str, target_digest: str
    ) -> TokenVerification:
        """Check a proposed token and spend nothing.

        #13 calls this to render a refusal before it asks anybody. Execution calls
        ``consume`` instead, because a check that spends the token and an execution that
        follows it are two moments, and the gap is the hole rule 1 closes.
        """
        with self._lock:
            result, _ = self._evaluate(
                nonce=nonce, session_id=session_id, target_digest=target_digest
            )
            return result

    def consume(
        self, *, nonce: str, session_id: str, target_digest: str
    ) -> TokenVerification:
        """Verify and spend, as one step, at the point of execution (rule 1).

        A refusal spends nothing. A caller whose host set changed can still run the action it
        got an approval for, once, without a second prompt.
        """
        with self._lock:
            result, record = self._evaluate(
                nonce=nonce, session_id=session_id, target_digest=target_digest
            )
            if result.ok and record is not None:
                record.used = True
            return result

    def pending_count(self) -> int:
        """How many tokens a caller can still spend. #13 shows this, and tests assert on it."""
        with self._lock:
            self._prune()
            now = self._now()
            return sum(1 for r in self._records.values() if not r.used and now < r.token.expires_at)

    def _evaluate(
        self, *, nonce: str, session_id: str, target_digest: str
    ) -> tuple[TokenVerification, _Record | None]:
        """Apply the rules in order, and name the first one that failed.

        Order matters for the message an operator reads. "Already used" and "expired" describe
        an approval that existed, so they come before the target checks, which describe an
        approval that never covered this action.
        """
        record = self._find(nonce)
        if record is None:
            return TokenVerification(ok=False, refusal=TokenRefusal.UNKNOWN_TOKEN), None
        if record.used:
            return TokenVerification(ok=False, refusal=TokenRefusal.ALREADY_USED), record
        if self._now() >= record.token.expires_at:
            return TokenVerification(ok=False, refusal=TokenRefusal.EXPIRED), record
        if record.token.session_id != session_id:
            return TokenVerification(ok=False, refusal=TokenRefusal.WRONG_SESSION), record
        if not _equal(record.token.target_digest, target_digest):
            return TokenVerification(ok=False, refusal=TokenRefusal.DIGEST_MISMATCH), record
        return TokenVerification(ok=True, token=record.token), record

    def _find(self, nonce: str) -> _Record | None:
        """Look a nonce up with a constant-time compare, and never break early.

        A plain ``dict[nonce]`` would answer from a hash, and the loop below keeps the
        comparison of the bearer value in ``hmac.compare_digest``. The scan visits every
        record, so the time it takes tells an attacker the number of pending approvals and
        nothing about the value. The store holds pending approvals only, so it stays small.
        """
        match: _Record | None = None
        for stored, record in self._records.items():
            if _equal(stored, nonce):
                match = record
        return match

    def _prune(self, *, keep: str | None = None) -> None:
        """Drop records that no longer explain anything. Callers hold the lock.

        A long-lived gateway would otherwise grow one record per approval forever. Removal
        waits until well after expiry so a late execution still gets "expired" rather than
        "unknown", because those two words describe different events to an operator.
        """
        cutoff = self._now() - _REASON_RETENTION_S
        stale = [
            nonce
            for nonce, record in self._records.items()
            if record.token.expires_at < cutoff and nonce != keep
        ]
        for nonce in stale:
            del self._records[nonce]


def _check_ttl(ttl_s: float) -> None:
    """Refuse a TTL that is not short (rule 2). The ceiling is not negotiable at runtime."""
    if ttl_s <= 0.0:
        raise ValueError("approval TTL must be positive")
    if ttl_s > MAX_TTL_S:
        raise ValueError(f"approval TTL must not exceed {MAX_TTL_S} seconds")


def _equal(stored: str, proposed: str) -> bool:
    """Compare a bearer-shaped value in constant time, and survive junk.

    ``hmac.compare_digest`` raises on a non-ASCII ``str``, and a proposed nonce arrives from
    model-visible text. A crash there would turn a forgery into an exception instead of a
    refusal, so a non-ASCII value simply does not match.
    """
    if not proposed.isascii() or not stored.isascii():
        return False
    return hmac.compare_digest(stored, proposed)


__all__ = [
    "DEFAULT_TTL_S",
    "MAX_TTL_S",
    "ApprovalToken",
    "ApprovalTokenStore",
    "TokenRefusal",
    "TokenVerification",
    "compute_target_digest",
]
