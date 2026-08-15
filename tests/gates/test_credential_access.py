# tests/gates/test_credential_access.py
"""Item 37 (#39): the credential.access class decides before the decryption.

``gates.<context>.credential.access`` enforced nothing before this item. The executor wrote a
log-only observation after ``resolve_plaintext`` returned, so an operator who set ``deny``
changed nothing at all. A control that enforces nothing is worse than no control.

The class covers the decryption, and ``mutate.remote`` covers the action. That separation is
useful. One deployment holds both kinds of server: one needs a stored credential, and one
reaches its hosts through an agent the operator already trusts.

**Nobody answers two prompts for one action.** A human approved the action, or a standing grant
covered it. Either one carries the authorization for the credential the action needs, so
``approve`` and ``grant`` read the action's own decision. An action that reached ``allow`` with
neither still needs a decision here, so it refuses.

Two orders carry the security properties. The decision lands before ``resolve_plaintext``, and
it lands before any transport opens. No record holds the plaintext.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.agent.tools.capabilities import CREDENTIAL_ACCESS, MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.executor.operator_socket import ApprovalService
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.policy import (
    ActionAuthorization,
    Outcome,
    evaluate_credential_access,
)
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_SSH_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_RESOLVE_PLAINTEXT = "nanoinfra.secrets.store.SecretStore.resolve_plaintext"

_SECRET_VALUE = "s3cr3t-key-material"
_COMMAND = "systemctl reload nginx"
_HOST = "10.0.1.5"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _gates(**over: Any) -> GatesConfig:
    """A deployment with two paths and one approver on the second one."""
    raw: dict[str, Any] = {
        "approvers": [{"channel": "webui", "sender": "operator-1"}],
        "approvalPaths": ["webui", "telegram"],
        "approvalTimeoutS": 30,
    }
    raw.update(over)
    return GatesConfig.model_validate(raw)


def _credential(gates: GatesConfig, *, context: str, value: str) -> GatesConfig:
    """Set one credential.access value, whatever the config schema spells today.

    #7 names four decision values, and nanoinfra/config/gates.py spells two of them for this
    key. Another change owns that file. Pydantic validates no assignment here, so these tests
    state every case now.
    """
    policy = gates.interactive if context == "interactive" else gates.unattended
    policy.credential_access = value  # type: ignore[assignment]
    return gates


def _unattended_grant(value: str) -> GatesConfig:
    """Unattended policy with one standing grant, plus one credential.access value.

    The grant is the only unattended allow path (#8), so this config is the shape an operator
    writes for a nightly automation.
    """
    gates = _gates(
        unattended={"mutate.remote": {"host": "grant", "group": "grant"}},
        standingGrants=[
            {
                "id": "reload",
                "contexts": ["unattended"],
                "hosts": [_HOST],
                "commands": [_COMMAND],
            }
        ],
    )
    return _credential(gates, context="unattended", value=value)


def _interactive_allow(value: str) -> GatesConfig:
    """Interactive policy that allows the action outright, plus one credential.access value.

    Nobody approved this action, and no grant covered it. So the credential decision has only
    the matrix to read.
    """
    gates = _gates(interactive={"mutate.remote": {"host": "allow", "group": "allow"}})
    return _credential(gates, context="interactive", value=value)


def _secret(tmp_path: Path) -> str:
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "password", "providerId": "local", "value": _SECRET_VALUE}
    )
    return secret.id


def _ssh_server(tmp_path: Path, *, secret_ref: str | None = None) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": _HOST},
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "telegram",
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _ok() -> ExecutionResult:
    return ExecutionResult(exit_code=0, output="reloaded", error=None)


class _Harness:
    """One executor, one audit store, and the operator service behind the second socket."""

    def __init__(self, tmp_path: Path, gates: GatesConfig) -> None:
        self.gates = gates
        self.audit = AuditStore(tmp_path / "gates")
        self.pending = PendingApprovalStore()
        self.tokens = ApprovalTokenStore()
        self.executor = Executor(
            workspace=tmp_path,
            gates_loader=lambda: self.gates,
            audit=self.audit,
            pending=self.pending,
            tokens=self.tokens,
        )
        self.service = ApprovalService(
            pending=self.pending,
            tokens=self.tokens,
            gates_loader=lambda: self.gates,
            audit=self.audit,
        )

    def decisions(self) -> list[tuple[str, str]]:
        """Every record as a decision and a class, in order."""
        return [
            (str(record["decision"]), str(record["capability_class"]))
            for record in self.audit.read_all()
        ]

    def credential_records(self) -> list[dict[str, Any]]:
        return [
            record
            for record in self.audit.read_all()
            if record["capability_class"] == CREDENTIAL_ACCESS
        ]

    async def wait_for_one_pending(
        self, timeout_s: float | None = None, task: "asyncio.Task[Any] | None" = None
    ):
        """Wait until the executor suspends one action, then return that record.

        *task* is the ``handle`` call this wait belongs to, and passing it changes what a
        failure teaches. An action that **refused** instead of suspending finishes that task,
        and this wait then reports the refusal at once. Without it the same run waited out the
        whole budget and reported "never suspended", which names the symptom and hides the
        cause.

        That is also why the budget is generous rather than tight. A real refusal now fails
        immediately, so a large budget delays no real failure. It only stops a slow machine
        from reading as a broken gate. The old budget was 5 seconds, and one group action
        measured 3.4 of them under coverage on a machine faster than the CI runner, which made
        that pass a coin flip: the test failed on the 3.14 job and passed on 3.11 in one run.
        """
        # The budget lives at the end of this file, so it cannot be a default argument:
        # Python evaluates a default when it defines the function.
        budget = _SUSPEND_BUDGET_S if timeout_s is None else timeout_s
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            items = self.pending.pending()
            if items:
                return items[0]
            if task is not None and task.done():
                raise AssertionError(
                    "the executor answered instead of suspending the action: "
                    + _finished_task_answer(task)
                )
            await asyncio.sleep(0.01)
        raise AssertionError(
            f"the executor never suspended an action within {budget}s. A group action "
            "resolves its host set through a real ansible-inventory subprocess, so a slow "
            "machine needs the budget, and a refusal reports itself at once when the caller "
            "passes its task."
        )


# --------------------------------------------------------------- deny refuses the action


@pytest.mark.asyncio
async def test_an_unattended_grant_refuses_when_the_credential_class_denies(
    tmp_path: Path,
) -> None:
    """The acceptance case. A grant permits the action, and the class refuses the decryption.

    The refusal arrives before ``resolve_plaintext``, and before the backend opens a transport.
    A job record for an action nothing ran would misreport the history, so no job lands either.
    """
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _unattended_grant("deny"))

    with (
        patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve,
    ):
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert not response.ok
    assert CREDENTIAL_ACCESS in response.reason
    resolve.assert_not_called()
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []
    assert harness.decisions() == [("allow", MUTATE_REMOTE), ("denied", CREDENTIAL_ACCESS)]


@pytest.mark.asyncio
async def test_the_refusal_names_the_secret_ref_of_the_server(tmp_path: Path) -> None:
    """An operator reads which credential stayed encrypted, and not only which class refused."""
    secret_id = _secret(tmp_path)
    _ssh_server(tmp_path, secret_ref=secret_id)
    harness = _Harness(tmp_path, _unattended_grant("deny"))

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert secret_id in response.reason
    assert harness.credential_records()[0]["secret_ref"] == secret_id


@pytest.mark.asyncio
async def test_the_same_action_runs_when_the_credential_class_allows(tmp_path: Path) -> None:
    """The other half of the acceptance case. ``allow`` decrypts, and the action runs."""
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _unattended_grant("allow"))

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert response.ok
    assert run.call_args.args[2] == _SECRET_VALUE
    # The completion record #46 appends when the action ends, with the action's own class.
    assert harness.decisions() == [
        ("allow", MUTATE_REMOTE),
        ("allow", CREDENTIAL_ACCESS),
        ("completion", MUTATE_REMOTE),
    ]


@pytest.mark.asyncio
async def test_a_value_no_policy_models_refuses(tmp_path: Path) -> None:
    """A hand-edited value must cost the decryption rather than buy it."""
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _unattended_grant("sometimes"))

    with (
        patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve,
    ):
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert not response.ok
    resolve.assert_not_called()
    run.assert_not_called()


# ------------------------------------------- the action's own decision satisfies the class


@pytest.mark.asyncio
async def test_a_standing_grant_satisfies_a_class_that_asks_for_a_grant(
    tmp_path: Path,
) -> None:
    """A grant is a permission the operator declared in advance, so it authorizes the read.

    The record names the grant, because a reviewer must see what satisfied the decision.
    """
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _unattended_grant("grant"))

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert response.ok
    run.assert_called_once()
    record = harness.credential_records()[0]
    assert record["decision"] == "allow"
    assert record["grant_id"] == "reload"
    assert record["approval_id"] is None


@pytest.mark.asyncio
async def test_the_action_record_also_names_the_grant_that_allowed_it(tmp_path: Path) -> None:
    """A finding beside #39: no executor record carried a grant id, and #16 defines the field.

    A reviewer who reads the ``mutate.remote`` record must see which grant permitted the action.
    The credential record names its own authorization, so the two records must agree.
    """
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _unattended_grant("grant"))

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert response.ok
    # The completion record names no grant: the decision record it follows holds that answer.
    assert [record["grant_id"] for record in harness.audit.read_all()] == [
        "reload",
        "reload",
        None,
    ]


@pytest.mark.asyncio
async def test_an_action_a_human_approved_needs_no_second_approval(tmp_path: Path) -> None:
    """The shipped interactive defaults: ``mutate.remote`` asks, and the class reads the answer.

    One action suspends once. The approval that released it also authorizes the credential the
    action needs, so nobody reads a second prompt for one decision.
    """
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        task = asyncio.create_task(harness.executor.handle(_request()))
        suspended = await harness.wait_for_one_pending(task=task)
        harness.service.approve(
            request_id=suspended.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=suspended.target_digest,
        )
        response = await task

    assert response.ok
    run.assert_called_once()
    assert harness.pending.pending() == ()
    # The completion record #46 appends when the action ends, with the action's own class.
    assert harness.decisions() == [
        ("approve", MUTATE_REMOTE),
        ("allow", MUTATE_REMOTE),
        ("allow", CREDENTIAL_ACCESS),
        ("completion", MUTATE_REMOTE),
    ]


@pytest.mark.asyncio
async def test_the_record_names_the_approval_that_authorized_the_decryption(
    tmp_path: Path,
) -> None:
    """A reviewer asks which approval authorized this decryption, and the record answers."""
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        task = asyncio.create_task(harness.executor.handle(_request()))
        suspended = await harness.wait_for_one_pending(task=task)
        harness.service.approve(
            request_id=suspended.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=suspended.target_digest,
        )
        await task

    record = harness.credential_records()[0]
    assert record["approval_id"] == suspended.request_id
    assert record["actor"] == "operator-1"
    assert record["approval_path"] == "webui"
    assert record["origin_path"] == "telegram"
    assert record["grant_id"] is None


@pytest.mark.asyncio
async def test_an_allowed_action_runs_with_the_shipped_credential_default(
    tmp_path: Path,
) -> None:
    """The combination a real operator met, end to end through the executor.

    An operator widened `interactive.mutate.remote.host` to `allow` and kept the shipped `approve`
    on this class. Every remote action against a server with a secretRef then refused, and it
    latched the session twice. `allow` meant nothing.

    The matrix decision is the authorization here. A second prompt would ask the same operator
    again for the same action, which #13 spends attention on the unusual case instead.
    """
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _interactive_allow("approve"))

    with (
        patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run,
        patch(_RESOLVE_PLAINTEXT, new=Mock(return_value="key-material")) as resolve,
    ):
        response = await harness.executor.handle(_request())

    assert response.ok, response.reason
    resolve.assert_called_once()
    run.assert_called_once()
    assert harness.pending.pending() == ()


@pytest.mark.asyncio
async def test_a_denied_credential_class_still_refuses_an_allowed_action(
    tmp_path: Path,
) -> None:
    """Where this class keeps its teeth. `deny` refuses the decryption an `allow` would need."""
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _interactive_allow("deny"))

    with (
        patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve,
    ):
        response = await harness.executor.handle(_request())

    assert not response.ok
    assert CREDENTIAL_ACCESS in response.reason
    resolve.assert_not_called()
    run.assert_not_called()


# ------------------------------------------------------- a server that needs no credential


@pytest.mark.asyncio
async def test_a_server_with_no_secret_ref_reaches_no_credential_decision(
    tmp_path: Path,
) -> None:
    """The class stays distinct from ``mutate.remote``.

    An agent-reached host needs no stored credential, so a ``deny`` for this class must not stop
    it. No decision happens, so no record claims one.
    """
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _interactive_allow("deny"))

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await harness.executor.handle(_request())

    assert response.ok
    run.assert_called_once()
    assert harness.credential_records() == []
    # No credential decision, because the server names no secret. The completion still lands (#46).
    assert harness.decisions() == [("allow", MUTATE_REMOTE), ("completion", MUTATE_REMOTE)]


# ------------------------------------------------------------------- the plaintext stays out


@pytest.mark.asyncio
async def test_no_audit_record_holds_the_plaintext(tmp_path: Path) -> None:
    """The plaintext exists in one process (#18), and it reaches no record.

    The test reads the segment bytes rather than the parsed records. A parser that dropped a
    field would hide a leak that the file still holds.
    """
    _ssh_server(tmp_path, secret_ref=_secret(tmp_path))
    harness = _Harness(tmp_path, _unattended_grant("allow"))

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await harness.executor.handle(_request(execution_context="automation"))

    assert response.ok
    # The backend received the value, so a plaintext really existed during this action.
    assert run.call_args.args[2] == _SECRET_VALUE
    segments = harness.audit.segments()
    assert segments
    for segment in segments:
        assert _SECRET_VALUE not in segment.read_text(encoding="utf-8")


def test_a_policy_allow_authorizes_the_credential_the_action_needs() -> None:
    """The case an operator meets first, and #39 refused it.

    An operator who sets `mutate.remote` to `allow` at one scope has said the agent may run that
    action. The action needs the stored credential of the server it names, so the same decision
    authorizes that read. Without this rule, `allow` plus the shipped `approve` on this class
    refuses every remote action against a server that holds a secretRef, and `allow` then means
    nothing. That combination reached a real operator.

    A second prompt is not the answer either. #13 spends a human's attention on the unusual case.
    """
    gates = GatesConfig.model_validate(
        {
            "interactive": {"mutate.remote": {"host": "allow"}, "credential.access": "approve"},
        }
    )

    decision = evaluate_credential_access(
        gates,
        execution_context="interactive",
        authorization=ActionAuthorization(policy_decision="allow", scope="host"),
    )

    assert decision.outcome is Outcome.ALLOW
    assert "allow" in decision.reason


def test_an_unattended_deny_still_refuses_a_policy_allow() -> None:
    """The class keeps its teeth where they matter: no human is present there."""
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}, "credential.access": "deny"},
        }
    )

    decision = evaluate_credential_access(
        gates,
        execution_context="automation",
        authorization=ActionAuthorization(grant_id="reload-web", policy_decision="grant"),
    )

    assert decision.outcome is Outcome.DENY


def test_the_class_accepts_allow_in_config() -> None:
    """The refusal advises 'allow', and the schema refused that value, so the advice was a dead end."""
    gates = GatesConfig.model_validate({"interactive": {"credential.access": "allow"}})

    decision = evaluate_credential_access(
        gates, execution_context="interactive", authorization=ActionAuthorization()
    )

    assert decision.outcome is Outcome.ALLOW


def test_the_class_accepts_grant_in_config() -> None:
    """A deployment may permit a decryption for granted work alone, and refuse it elsewhere."""
    gates = GatesConfig.model_validate({"unattended": {"credential.access": "grant"}})

    granted = evaluate_credential_access(
        gates,
        execution_context="automation",
        authorization=ActionAuthorization(grant_id="reload-web"),
    )
    bare = evaluate_credential_access(
        gates, execution_context="automation", authorization=ActionAuthorization()
    )

    assert granted.outcome is Outcome.ALLOW
    assert bare.outcome is Outcome.DENY


# ---------------------------------------------------------------- the suspension wait (#82)

#: How long one suspension may take. The work includes a real ansible-inventory subprocess for
#: a group scope, and this number only bounds a machine that is slow. A refusal reports itself
#: at once, so the budget never delays a real failure.
_SUSPEND_BUDGET_S = 30.0


def _finished_task_answer(task: "asyncio.Task[Any]") -> str:
    """What a finished handle call answered, for a wait that expected a suspension."""
    if task.cancelled():
        return "the call was cancelled"
    error = task.exception()
    if error is not None:
        return f"{type(error).__name__}: {error}"
    return repr(task.result())
