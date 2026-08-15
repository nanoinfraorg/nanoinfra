# tests/gates/test_pending_approvals.py
"""Item 36 (#38): the store that holds one suspended action.

An ``approve`` outcome suspends the action. The record of that suspension lives in the
executor, and it lives in memory. Four properties carry the design.

- A blocked waiter wakes as soon as an operator answers.
- A deadline ends the wait, so no caller polls.
- An answer arrives once. A second answer gets a refusal.
- The store opens no file, so a restart drops every unanswered action.

The last property is the fail-closed one. A restart that resurrects an approvable action
gives the agent a retry that no human sees.
"""

from __future__ import annotations

import ast
import threading
import time
from pathlib import Path

import pytest

from nanoinfra.gates.pending import (
    AnswerRefusal,
    ApprovalState,
    PendingApprovalStore,
)

_COMMAND = "systemctl reload nginx"
_HOSTS = ("web-01", "web-02")
_DIGEST = "sha256:" + "a" * 64
_PAYLOAD = "nanoinfra approval request v1\n"


def _store() -> PendingApprovalStore:
    return PendingApprovalStore()


def _create(store: PendingApprovalStore, *, timeout_s: float = 30.0, session_id: str = "s1"):
    return store.create(
        session_id=session_id,
        origin_path="telegram",
        execution_context="interactive",
        capability_class="mutate.remote",
        scope="group",
        hosts=_HOSTS,
        command=_COMMAND,
        payload=_PAYLOAD,
        target_digest=_DIGEST,
        timeout_s=timeout_s,
    )


def test_a_new_record_is_pending_and_carries_the_rendered_payload() -> None:
    """The payload is the bytes #14 rendered. The store keeps them and changes nothing."""
    store = _store()

    record = _create(store)

    assert record.session_id == "s1"
    assert record.origin_path == "telegram"
    assert record.hosts == _HOSTS
    assert record.payload == _PAYLOAD
    assert record.target_digest == _DIGEST
    assert [item.request_id for item in store.pending()] == [record.request_id]


def test_an_approval_wakes_a_blocked_waiter() -> None:
    """The wait ends on the answer, and not on the deadline.

    A deadline that ended every wait would make an approval useless.
    """
    store = _store()
    record = _create(store)
    outcomes: list[object] = []

    def waiter() -> None:
        outcomes.append(store.wait(record.request_id))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _wait_until(lambda: store.waiter_count() == 1)

    refusal = store.approve(
        request_id=record.request_id,
        actor="operator-1",
        approval_path="webui",
        token_nonce="nonce-1",
        target_digest=_DIGEST,
    )
    thread.join(timeout=5)

    assert refusal is None
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert getattr(outcome, "state") is ApprovalState.APPROVED
    assert getattr(outcome, "token_nonce") == "nonce-1"
    assert getattr(outcome, "actor") == "operator-1"
    assert getattr(outcome, "approval_path") == "webui"


def test_a_denial_wakes_the_waiter_and_carries_the_operator_reason() -> None:
    store = _store()
    record = _create(store)
    outcomes: list[object] = []

    def waiter() -> None:
        outcomes.append(store.wait(record.request_id))

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    _wait_until(lambda: store.waiter_count() == 1)

    store.deny(
        request_id=record.request_id,
        actor="operator-1",
        approval_path="webui",
        reason="the change window is closed",
    )
    thread.join(timeout=5)

    outcome = outcomes[0]
    assert getattr(outcome, "state") is ApprovalState.DENIED
    assert "change window" in getattr(outcome, "reason")
    assert getattr(outcome, "token_nonce") is None


def test_an_unanswered_record_expires_at_the_deadline() -> None:
    """The wait is bounded. An operator who reads nothing costs the action, not the process."""
    store = _store()
    record = _create(store, timeout_s=0.2)

    outcome = store.wait(record.request_id)

    assert outcome.state is ApprovalState.EXPIRED
    assert outcome.token_nonce is None
    assert store.pending() == ()


def test_an_answer_after_the_expiry_gets_a_refusal() -> None:
    """A late approval must not revive an action that already refused."""
    store = _store()
    record = _create(store, timeout_s=0.05)
    store.wait(record.request_id)

    refusal = store.approve(
        request_id=record.request_id,
        actor="operator-1",
        approval_path="webui",
        token_nonce="nonce-1",
        target_digest=_DIGEST,
    )

    assert refusal is AnswerRefusal.EXPIRED


def test_a_second_answer_gets_a_refusal() -> None:
    """One action, one answer. A second answer would change a decision already taken."""
    store = _store()
    record = _create(store)

    first = store.approve(
        request_id=record.request_id,
        actor="operator-1",
        approval_path="webui",
        token_nonce="nonce-1",
        target_digest=_DIGEST,
    )
    second = store.deny(
        request_id=record.request_id,
        actor="operator-1",
        approval_path="webui",
        reason="changed my mind",
    )

    assert first is None
    assert second is AnswerRefusal.ALREADY_ANSWERED


def test_an_unknown_request_id_gets_a_refusal() -> None:
    store = _store()

    refusal = store.deny(
        request_id="no-such-request",
        actor="operator-1",
        approval_path="webui",
        reason="",
    )

    assert refusal is AnswerRefusal.UNKNOWN_REQUEST


def test_an_approval_of_other_bytes_gets_a_refusal() -> None:
    """The operator echoes the digest of the payload they read.

    A mismatch means the answer describes other bytes, so it authorizes nothing.
    """
    store = _store()
    record = _create(store)

    refusal = store.approve(
        request_id=record.request_id,
        actor="operator-1",
        approval_path="webui",
        token_nonce="nonce-1",
        target_digest="sha256:" + "b" * 64,
    )

    assert refusal is AnswerRefusal.DIGEST_MISMATCH
    assert [item.request_id for item in store.pending()] == [record.request_id]


def test_the_wait_of_an_unknown_request_raises() -> None:
    """The executor waits on a record it just created, so an unknown id is a defect."""
    store = _store()

    with pytest.raises(KeyError):
        store.wait("no-such-request")


def test_two_sessions_wait_independently() -> None:
    """A pending approval in one session must not answer for another session."""
    store = _store()
    first = _create(store, session_id="s1")
    second = _create(store, session_id="s2")

    store.approve(
        request_id=first.request_id,
        actor="operator-1",
        approval_path="webui",
        token_nonce="nonce-1",
        target_digest=_DIGEST,
    )

    assert [item.request_id for item in store.pending()] == [second.request_id]


def test_an_old_record_goes_away_and_a_recent_one_stays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-lived executor must not hold one record per action forever.

    Removal waits until well after the deadline, so a late answer still reads "expired" rather
    than "no such request". The test shortens that window instead of waiting for it.
    """
    from nanoinfra.gates import pending as pending_module

    store = _store()
    old = _create(store, timeout_s=0.01)
    store.wait(old.request_id)
    fresh = _create(store)

    assert store.get(old.request_id) is not None  # still inside the retention window

    monkeypatch.setattr(pending_module, "_RETENTION_S", 0.0)
    _create(store)

    assert store.get(old.request_id) is None
    assert store.get(fresh.request_id) is not None


def test_the_store_opens_no_file() -> None:
    """Structural guard for the in-memory rule.

    An unanswered action must die with the process. A store that wrote a file could hand a
    restart an action that a human never answered.
    """
    source = (Path(__file__).parents[2] / "nanoinfra/gates/pending.py").read_text()
    tree = ast.parse(source)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    persistence = [
        name
        for name in imported
        if name.split(".")[0] in {"pathlib", "json", "os", "shelve", "sqlite3", "pickle"}
    ]
    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert persistence == []
    assert "open" not in calls


def _wait_until(predicate, timeout_s: float = 5.0) -> None:
    """Wait for a condition another thread produces, or fail the test."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("the condition never became true")
