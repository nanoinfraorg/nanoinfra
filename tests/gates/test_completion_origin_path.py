# tests/gates/test_completion_origin_path.py
"""A completion record must answer the same filters as its decision -- #83.

`record_completion` copies a defined subset of the decision it follows, and #46 chose that subset
on purpose. #79 added `origin_actor` to it. `origin_path` stayed out, so the viewer's filters
disagreed with each other: a filter on the **person** who raised an action returned both rows, and a
filter on the **path** that raised it returned the decision alone and hid the row that says what
happened.

The rule that decides which side a field falls on: **a fact about the action is copied, and a step
of the authorization is not.** So `origin_path` travels and `same_path` does not, because the path is
a fact about the request and the comparison of two paths is a step of the decision.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from nanoinfra.gates.audit import AuditStore

_ORIGIN = "telegram"


def _store(tmp_path: Path) -> AuditStore:
    return AuditStore(tmp_path / "gates")


def _decision(store: AuditStore) -> dict[str, Any]:
    return store.record(
        decision="allow",
        capability_class="mutate.remote",
        execution_context="interactive",
        session_id="s1",
        origin_path=_ORIGIN,
        origin_actor="webui:asked@example.com",
        approval_path="webui",
        actor="webui:answered@example.com",
        scope="host",
        hosts=["10.0.1.5"],
        command_digest="sha256:abc",
        reason="allowed",
    )


def test_a_completion_carries_the_origin_path_of_its_decision(tmp_path: Path) -> None:
    store = _store(tmp_path)
    decision = _decision(store)

    store.record_completion(follows=decision, exit_code=0, duration_ms=12, reason="ended")

    records = store.read_all()
    assert [record["origin_path"] for record in records] == [_ORIGIN, _ORIGIN]


def test_one_filter_on_the_origin_path_returns_both_rows_of_an_action(tmp_path: Path) -> None:
    """The property the asymmetry broke. A reviewer asks for a channel and reads the outcome."""
    store = _store(tmp_path)
    decision = _decision(store)
    store.record_completion(follows=decision, exit_code=0, duration_ms=12, reason="ended")

    matched = [r for r in store.read_all() if r["origin_path"] == _ORIGIN]

    assert [r["decision"] for r in matched] == ["allow", "completion"]


def test_a_completion_still_holds_no_step_of_the_authorization(tmp_path: Path) -> None:
    """`same_path` is a comparison the gate made, so it stays with the decision.

    #46 keeps the authorization on one record so one authorization cannot read two ways, and this
    change must not widen that. `actor` names who answered and belongs to the same side.
    """
    store = _store(tmp_path)
    decision = _decision(store)
    store.record_completion(follows=decision, exit_code=0, duration_ms=12, reason="ended")

    completion = store.read_all()[1]

    # same_path is derived from the two paths and never passed, so a completion that copies
    # the origin path and not the answering path reports None by construction. That is the
    # derivation doing the right thing rather than a value this change had to choose.
    assert completion["same_path"] is None
    assert completion["actor"] is None
    assert completion["approval_path"] is None
    assert completion["grant_id"] is None
    assert completion["approval_id"] is None
    assert completion["token_nonce"] is None


def test_a_decision_with_no_origin_path_leaves_the_completion_null(tmp_path: Path) -> None:
    """An absent fact stays absent. A blank must not read as a path name."""
    store = _store(tmp_path)
    decision = store.record(
        decision="allow",
        capability_class="mutate.local",
        execution_context="interactive",
        session_id="s1",
        reason="allowed",
    )

    store.record_completion(follows=decision, exit_code=0, duration_ms=3, reason="ended")

    assert store.read_all()[1]["origin_path"] is None
