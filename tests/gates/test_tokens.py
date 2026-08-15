# tests/gates/test_tokens.py
"""Item 9 (#12): an approval token binds one approval to one resolved action.

Five rules, and each rule closes one hole, so each rule gets its own test. The holes are
independent: a store that closes replay can still leave ambient authority, and a store with
a short TTL can still accept a token that a second session proposes.

Nothing here reads a wall clock or sleeps. The store takes a ``now`` callable, so an expiry
test moves the clock instead of waiting for it.
"""

from __future__ import annotations

import pytest

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.gates.tokens import (
    DEFAULT_TTL_S,
    MAX_TTL_S,
    ApprovalToken,
    ApprovalTokenStore,
    TokenRefusal,
    compute_target_digest,
)


class FakeClock:
    """A monotonic clock a test drives by hand. Real time never enters these tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _issue(
    store: ApprovalTokenStore,
    *,
    session_id: str = "session-1",
    digest: str | None = None,
) -> ApprovalToken:
    """Issue a token with the fields #13 would fill from a real approval."""
    default_digest = compute_target_digest(command="systemctl restart nginx", hosts=["web-1"])
    return store.issue(
        session_id=session_id,
        actor="telegram:12345",
        origin_path="telegram:chat-9",
        approval_path="telegram:chat-9",
        target_digest=digest or default_digest,
        capability_class=MUTATE_REMOTE,
        scope="host",
    )


def test_execution_consumes_the_token_and_issuance_does_not() -> None:
    """Rule 1. One use, and the use is the execution.

    A store that marks the token used at issuance leaves the window between the approval and
    the transport with no authorization in force, so a replay in that window meets no state
    at all.
    """
    clock = FakeClock()
    store = ApprovalTokenStore(now=clock)
    token = _issue(store)

    # Issuance left the token spendable, so the gate can still verify before it dials.
    assert store.verify(
        nonce=token.nonce, session_id=token.session_id, target_digest=token.target_digest
    ).ok
    assert store.pending_count() == 1

    first = store.consume(
        nonce=token.nonce, session_id=token.session_id, target_digest=token.target_digest
    )
    assert first.ok
    assert first.token == token

    second = store.consume(
        nonce=token.nonce, session_id=token.session_id, target_digest=token.target_digest
    )
    assert not second.ok
    assert second.refusal == TokenRefusal.ALREADY_USED
    assert second.token is None


def test_ttl_is_short_and_an_expired_token_is_refused() -> None:
    """Rule 2. The TTL runs in tens of seconds, so ambient authority cannot develop."""
    # A human needs time to read a rendered host list, and no longer than a few minutes.
    assert 30.0 <= DEFAULT_TTL_S <= MAX_TTL_S
    assert MAX_TTL_S <= 300.0

    clock = FakeClock()
    store = ApprovalTokenStore(now=clock, ttl_s=60.0)
    token = _issue(store)
    assert token.expires_at == token.issued_at + 60.0

    clock.advance(59.0)
    assert store.verify(
        nonce=token.nonce, session_id=token.session_id, target_digest=token.target_digest
    ).ok

    clock.advance(2.0)
    late = store.consume(
        nonce=token.nonce, session_id=token.session_id, target_digest=token.target_digest
    )
    assert not late.ok
    assert late.refusal == TokenRefusal.EXPIRED

    # A caller cannot buy ambient authority with a long per-issue TTL.
    with pytest.raises(ValueError):
        ApprovalTokenStore(now=clock, ttl_s=MAX_TTL_S + 1.0)
    with pytest.raises(ValueError):
        store.issue(
            session_id="session-1",
            actor="telegram:12345",
            origin_path="telegram:chat-9",
            approval_path="telegram:chat-9",
            target_digest=compute_target_digest(command="uptime", hosts=["web-1"]),
            capability_class=MUTATE_REMOTE,
            scope="host",
            ttl_s=MAX_TTL_S + 1.0,
        )


def test_digest_covers_the_expanded_command_and_the_resolved_host_set() -> None:
    """Rule 3. The digest binds what runs, not what the plan said.

    An approval of ``systemctl restart $SVC`` plus an execution of the expansion is the
    replay hole, so the template and the expansion must not share a digest.
    """
    template = compute_target_digest(command="systemctl restart $SVC", hosts=["web-1"])
    expanded = compute_target_digest(command="systemctl restart nginx", hosts=["web-1"])
    assert template != expanded

    one_host = compute_target_digest(command="uptime", hosts=["web-1"])
    two_hosts = compute_target_digest(command="uptime", hosts=["web-1", "web-2"])

    # One host set has one digest, so a reordered render cannot force a second approval.
    assert compute_target_digest(command="uptime", hosts=["web-2", "web-1"]) == two_hosts
    # A repeated host adds no host, so it changes no digest.
    assert compute_target_digest(command="uptime", hosts=["web-1", "web-1"]) == one_host
    # A wider host set is another action, even under the same command.
    assert one_host != two_hosts
    # The digest names its algorithm, and it repeats, so the audit record stays comparable.
    assert one_host.startswith("sha256:")
    assert compute_target_digest(command="uptime", hosts=["web-1"]) == one_host

    # A separator inside a host name must not forge another host set.
    assert compute_target_digest(command="uptime", hosts=["web-1\nweb-2"]) != two_hosts

    # No resolved host means no target to bind, so the digest refuses to exist.
    with pytest.raises(ValueError):
        compute_target_digest(command="uptime", hosts=[])


def test_a_different_host_set_command_or_session_needs_a_new_approval() -> None:
    """Rule 4. "Approve once and reuse across the plan" does not exist."""
    clock = FakeClock()
    store = ApprovalTokenStore(now=clock)
    token = _issue(
        store,
        digest=compute_target_digest(command="systemctl restart nginx", hosts=["web-1"]),
    )

    wider_hosts = store.verify(
        nonce=token.nonce,
        session_id=token.session_id,
        target_digest=compute_target_digest(
            command="systemctl restart nginx", hosts=["web-1", "web-2"]
        ),
    )
    assert wider_hosts.refusal == TokenRefusal.DIGEST_MISMATCH

    other_command = store.verify(
        nonce=token.nonce,
        session_id=token.session_id,
        target_digest=compute_target_digest(command="systemctl stop nginx", hosts=["web-1"]),
    )
    assert other_command.refusal == TokenRefusal.DIGEST_MISMATCH

    other_session = store.verify(
        nonce=token.nonce, session_id="session-2", target_digest=token.target_digest
    )
    assert other_session.refusal == TokenRefusal.WRONG_SESSION

    # Every refusal left the token unspent, so the approved action still runs once.
    assert store.consume(
        nonce=token.nonce, session_id=token.session_id, target_digest=token.target_digest
    ).ok


def test_approval_state_lives_in_the_store_and_not_in_proposed_fields() -> None:
    """Rule 5. The model can propose any token the model can read.

    Authority therefore comes from the store's own record. A token that the store never
    issued has no record, so it verifies as unknown however well formed it looks.
    """
    clock = FakeClock()
    store = ApprovalTokenStore(now=clock)
    issued = _issue(store)

    forged = ApprovalToken(
        session_id=issued.session_id,
        actor=issued.actor,
        origin_path=issued.origin_path,
        approval_path=issued.approval_path,
        target_digest=issued.target_digest,
        capability_class=issued.capability_class,
        scope=issued.scope,
        issued_at=clock(),
        expires_at=clock() + DEFAULT_TTL_S,
        nonce="nonce-the-model-wrote",
    )
    refused = store.consume(
        nonce=forged.nonce,
        session_id=forged.session_id,
        target_digest=forged.target_digest,
    )
    assert not refused.ok
    assert refused.refusal == TokenRefusal.UNKNOWN_TOKEN

    # #16 records the approval, and the record must not carry the bearer value.
    fields = issued.audit_fields()
    assert "nonce" not in fields
    assert fields["target_digest"] == issued.target_digest
    assert fields["actor"] == issued.actor
    assert issued.nonce not in repr(issued.audit_fields())


def test_nonces_come_from_a_csprng_and_never_repeat() -> None:
    """The nonce is the bearer value, so a guessable nonce is a bypass of every other rule."""
    store = ApprovalTokenStore(now=FakeClock())
    nonces = {_issue(store).nonce for _ in range(50)}

    assert len(nonces) == 50
    for nonce in nonces:
        assert len(nonce) >= 32
