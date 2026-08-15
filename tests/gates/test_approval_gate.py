# tests/gates/test_approval_gate.py
"""Item 36 (#38): an ``approve`` outcome suspends the action and waits for a human.

Before this item the executor allowed every interactive turn. #8 and #10 both named that
short-circuit, and it meant an `approve` decision executed. So the three shipped pieces never
ran: #12 issued no token, #13 judged no path, and #14 rendered no payload.

The sequence under test is the acceptance criterion of the item.

- An unusual interactive action suspends.
- An operator answers on a second authenticated path.
- The action runs after the approval, and it expires without one.
- A denial refuses, and the refusal carries the operator's words.

Two orders carry the security properties. The audit record for the suspension lands before the
wait. The token is issued on the answer, and it is consumed at execution.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.executor.operator_socket import ApprovalService
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.prompt import digest_rendered_prompt
from nanoinfra.gates.tokens import ApprovalTokenStore, TokenRefusal, compute_target_digest
from nanoinfra.secrets import crypto
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_SSH_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_ANSIBLE_BACKEND = "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run"
_RESOLVE_PLAINTEXT = "nanoinfra.secrets.store.SecretStore.resolve_plaintext"

_COMMAND = "systemctl reload nginx"
_GROUP_HOSTS = ("10.0.2.11", "10.0.2.12", "10.0.2.13")
_INVENTORY = "[web]\n" + "\n".join(_GROUP_HOSTS) + "\n"


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


def _ssh_server(tmp_path: Path) -> None:
    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": "10.0.1.5"}}
    )


def _group_server(tmp_path: Path) -> None:
    project = tmp_path / "ansible-project"
    project.mkdir(exist_ok=True)
    (project / "inventory").write_text(_INVENTORY, encoding="utf-8")
    ServerStore(tmp_path).create(
        {
            "name": "webservers",
            "providerId": "ansible-runner",
            "config": {"group": "web", "projectPath": str(project)},
        }
    )


class _Harness:
    """One executor, one pending store, one token store, and the operator service."""

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

    def decisions(self) -> list[str]:
        return [str(record["decision"]) for record in self.audit.read_all()]

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


# ------------------------------------------------------------------------ the config knob


def test_the_default_wait_is_short() -> None:
    """#12 rule 2. A human needs time to read a rendered host list, and no longer.

    The default must also stay at or below the token ceiling. A wait longer than the ceiling
    would produce an approval whose token cannot cover the action it approved.
    """
    from nanoinfra.gates.tokens import MAX_TTL_S

    assert GatesConfig().approval_timeout_s == 120
    assert GatesConfig().approval_timeout_s <= MAX_TTL_S


def test_a_wait_above_the_token_ceiling_is_refused() -> None:
    """Ambient authority must not develop out of one config value."""
    import pydantic

    from nanoinfra.gates.tokens import MAX_TTL_S

    with pytest.raises(pydantic.ValidationError):
        GatesConfig.model_validate({"approvalTimeoutS": int(MAX_TTL_S) + 1})
    with pytest.raises(pydantic.ValidationError):
        GatesConfig.model_validate({"approvalTimeoutS": 0})


# ----------------------------------------------------------- the short-circuit is gone


@pytest.mark.asyncio
async def test_an_interactive_action_no_longer_executes_without_an_approval(
    tmp_path: Path,
) -> None:
    """The two-line short-circuit in ``_gate`` allowed every interactive turn.

    An executor with no approval store wired must refuse rather than run.
    """
    _ssh_server(tmp_path)
    executor = Executor(workspace=tmp_path, gates_loader=_gates)

    with (
        patch(_SSH_BACKEND, new=AsyncMock()) as run,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve,
    ):
        response = await executor.handle(_request())

    assert not response.ok
    run.assert_not_called()
    resolve.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_an_interactive_action_that_policy_denies_is_refused(tmp_path: Path) -> None:
    """Policy now answers for an interactive turn, so a deny reaches the caller."""
    _ssh_server(tmp_path)
    gates = _gates(interactive={"mutate.remote": {"host": "deny", "group": "deny"}})
    harness = _Harness(tmp_path, gates)

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(_request())

    assert not response.ok
    assert "deny" in response.reason
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_interactive_action_a_standing_grant_covers_still_runs(tmp_path: Path) -> None:
    """#11: a grant skips the prompt, so a recurring action asks nobody."""
    _ssh_server(tmp_path)
    gates = _gates(
        standingGrants=[
            {
                "id": "reload",
                "contexts": ["interactive"],
                "hosts": ["10.0.1.5"],
                "commands": [_COMMAND],
            }
        ]
    )
    harness = _Harness(tmp_path, gates)

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await harness.executor.handle(_request())

    assert response.ok
    run.assert_called_once()
    assert harness.pending.pending() == ()


# --------------------------------------------------------------- suspend, then answer


@pytest.mark.asyncio
async def test_an_unusual_interactive_group_action_suspends_and_then_runs(
    tmp_path: Path,
) -> None:
    """The whole sequence: suspend, answer on a second path, run.

    The approval arrives in the WebUI while the request came from Telegram. #13 needs that
    difference, because a requester who approves on the origin channel is single-factor.
    """
    _group_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_ANSIBLE_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        task = asyncio.create_task(harness.executor.handle(_request(server_id_or_name="webservers")))
        record = await harness.wait_for_one_pending(task=task)
        run.assert_not_called()

        answer = harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        response = await task

    assert answer.ok
    assert response.ok
    assert "reloaded" in response.output
    run.assert_called_once()
    # The completion record #46 appends when the action ends.
    assert harness.decisions() == ["approve", "allow", "completion"]


@pytest.mark.asyncio
async def test_the_payload_the_operator_reads_names_every_host_and_the_count(
    tmp_path: Path,
) -> None:
    """#14: a group renders as every resolved name plus a count, and never as a label.

    The digest covers exactly those bytes, so a re-derivation from the text must agree.
    """
    _group_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_ANSIBLE_BACKEND, new=AsyncMock(return_value=_ok())):
        task = asyncio.create_task(harness.executor.handle(_request(server_id_or_name="webservers")))
        record = await harness.wait_for_one_pending(task=task)
        harness.service.deny(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            reason="not now",
        )
        await task

    for host in _GROUP_HOSTS:
        assert host in record.payload
    assert f"Hosts: {len(_GROUP_HOSTS)}" in record.payload
    assert "\n  web\n" not in record.payload  # the group label never stands in for the hosts
    assert record.target_digest == digest_rendered_prompt(record.payload)
    assert record.target_digest == compute_target_digest(
        command=_COMMAND, hosts=_GROUP_HOSTS
    )
    assert record.hosts == _GROUP_HOSTS
    assert record.scope == "group"


@pytest.mark.asyncio
async def test_an_unanswered_action_expires_and_reaches_no_host(tmp_path: Path) -> None:
    """The deadline ends the wait. An operator who reads nothing costs the action."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates(approvalTimeoutS=1))

    with (
        patch(_SSH_BACKEND, new=AsyncMock()) as run,
        patch(_RESOLVE_PLAINTEXT, new=Mock()) as resolve,
    ):
        response = await harness.executor.handle(_request())

    assert not response.ok
    assert "expired" in response.reason
    run.assert_not_called()
    resolve.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []
    assert harness.decisions() == ["approve", "expired"]


@pytest.mark.asyncio
async def test_a_denial_refuses_the_action_and_carries_the_operator_words(
    tmp_path: Path,
) -> None:
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        harness.service.deny(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            reason="the change window is closed",
        )
        response = await task

    assert not response.ok
    assert "change window" in response.reason
    run.assert_not_called()
    assert harness.decisions() == ["approve", "denied"]


# ------------------------------------------------------------- who may answer, and where


@pytest.mark.asyncio
async def test_an_approval_on_the_origin_path_gets_a_refusal(tmp_path: Path) -> None:
    """#13 condition 3. One compromised Telegram account must not yield both halves."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates(approvalTimeoutS=1))
    approvers = [{"channel": "telegram", "sender": "operator-1"}, *[]]
    harness.gates = _gates(
        approvers=[*approvers, {"channel": "webui", "sender": "operator-1"}],
        approvalTimeoutS=1,
    )

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        answer = harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="telegram",
            target_digest=record.target_digest,
        )
        response = await task

    assert not answer.ok
    assert "path" in str(answer.error)
    assert not response.ok
    run.assert_not_called()
    assert harness.decisions() == ["approve", "approval_refused", "expired"]


@pytest.mark.asyncio
async def test_a_sender_outside_the_approver_set_cannot_answer(tmp_path: Path) -> None:
    """#13 condition 1. A channel allowlist and the pairing store grant nothing."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates(approvalTimeoutS=1))

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        answer = harness.service.approve(
            request_id=record.request_id,
            actor="intruder",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        response = await task

    assert not answer.ok
    assert "gates.approvers" in str(answer.error)
    assert not response.ok
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_answer_about_other_bytes_cannot_approve(tmp_path: Path) -> None:
    """The human authorizes bytes. A digest that names other bytes authorizes nothing."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates(approvalTimeoutS=1))

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        answer = harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=compute_target_digest(command="rm -rf /", hosts=("10.0.2.11",)),
        )
        response = await task

    assert not answer.ok
    assert not response.ok
    run.assert_not_called()


# --------------------------------------------------------- no correct answer can exist


@pytest.mark.asyncio
async def test_a_single_path_deployment_refuses_at_once_and_names_the_missing_path(
    tmp_path: Path,
) -> None:
    """#13's design consequence. A WebUI-only install has no runtime approval path.

    The refusal must arrive at once. A suspension nobody may answer is a hang, and an
    operator who waits two minutes for "denied" learns nothing about the fix.
    """
    _ssh_server(tmp_path)
    harness = _Harness(
        tmp_path,
        _gates(approvalPaths=["webui"], approvers=[{"channel": "webui", "sender": "operator-1"}]),
    )

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(_request(origin_path="webui"))

    assert not response.ok
    assert "second authenticated path" in response.reason
    assert "standing grant" in response.reason
    run.assert_not_called()
    assert harness.pending.pending() == ()
    assert harness.decisions() == ["denied"]


@pytest.mark.asyncio
async def test_a_single_path_deployment_with_two_people_suspends_and_then_runs(
    tmp_path: Path,
) -> None:
    """#47 item 11, end to end. The identity travels from the frame to the answer.

    The frame names the person who raised the turn, the executor puts that name on the
    suspended action, and the answer of a second person on the one path counts. The test proves
    the wiring as well as the rule: a rule nothing supplies would change nothing in production.
    """
    _ssh_server(tmp_path)
    harness = _Harness(
        tmp_path,
        _gates(
            approvalPaths=["webui"],
            approvers=[
                {"channel": "webui", "sender": "webui:alice@example.com"},
                {"channel": "webui", "sender": "webui:bob@example.com"},
            ],
            identityIndependence=True,
        ),
    )

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        task = asyncio.create_task(
            harness.executor.handle(
                _request(origin_path="webui", origin_actor="webui:alice@example.com")
            )
        )
        record = await harness.wait_for_one_pending(task=task)
        run.assert_not_called()

        refused = harness.service.approve(
            request_id=record.request_id,
            actor="webui:alice@example.com",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        answer = harness.service.approve(
            request_id=record.request_id,
            actor="webui:bob@example.com",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        response = await task

    assert record.origin_actor == "webui:alice@example.com"
    assert not refused.ok  # the person who asked cannot answer, in any mode
    assert refused.refusal == "same_actor_and_path"
    assert answer.ok
    assert response.ok
    run.assert_called_once()


@pytest.mark.asyncio
async def test_a_single_path_deployment_with_the_flag_off_still_refuses_at_once(
    tmp_path: Path,
) -> None:
    """The same deployment without the flag. Two people on one path stay one path.

    The refusal arrives before the suspension, so the flag decides whether the action waits.
    """
    _ssh_server(tmp_path)
    harness = _Harness(
        tmp_path,
        _gates(
            approvalPaths=["webui"],
            approvers=[
                {"channel": "webui", "sender": "webui:alice@example.com"},
                {"channel": "webui", "sender": "webui:bob@example.com"},
            ],
        ),
    )

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(
            _request(origin_path="webui", origin_actor="webui:alice@example.com")
        )

    assert not response.ok
    assert "second authenticated path" in response.reason
    run.assert_not_called()
    assert harness.pending.pending() == ()


@pytest.mark.asyncio
async def test_a_deployment_with_no_approver_refuses_at_once(tmp_path: Path) -> None:
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates(approvers=[]))

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(_request())

    assert not response.ok
    assert "gates.approvers" in response.reason
    run.assert_not_called()
    assert harness.pending.pending() == ()


@pytest.mark.asyncio
async def test_a_request_that_names_no_origin_path_refuses_at_once(tmp_path: Path) -> None:
    """An agent that does not state its path must not execute."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(_request(origin_path=None))

    assert not response.ok
    assert "origin path" in response.reason
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_request_that_names_no_session_refuses_at_once(tmp_path: Path) -> None:
    """#12 binds a token to one session. No session means no binding, so no approval."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(_request(session_id=None))

    assert not response.ok
    assert "session" in response.reason
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_nonce_from_the_agent_is_refused(tmp_path: Path) -> None:
    """The executor issues every nonce and hands none to the agent.

    So a request that carries one is a proposal from model-visible text, and it gets a refusal
    rather than a verification attempt.
    """
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock()) as run:
        response = await harness.executor.handle(_request(token_nonce="proposed-by-the-model"))

    assert not response.ok
    assert "nonce" in str(response.error) + response.reason
    run.assert_not_called()
    assert harness.pending.pending() == ()


# ----------------------------------------------------------------- the token, once only


@pytest.mark.asyncio
async def test_the_token_is_issued_on_the_answer_and_spent_by_the_execution(
    tmp_path: Path,
) -> None:
    """#12 rule 1. Issuance does not spend the token, and the execution does."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        assert harness.tokens.pending_count() == 0
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        assert harness.tokens.pending_count() == 0  # nothing issued before the answer
        harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        response = await task

    assert response.ok
    assert harness.tokens.pending_count() == 0  # the execution spent it


@pytest.mark.asyncio
async def test_the_token_cannot_be_replayed_for_another_command_in_the_same_session(
    tmp_path: Path,
) -> None:
    """#12 rule 4. A different command needs a different approval.

    The nonce never leaves the executor, so this test reads it from the store the operator's
    answer wrote. It then replays that nonce against another command in the same session.
    """
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates(approvalTimeoutS=1))
    nonces: list[str] = []

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        first = await task
        nonces.append(_answered_nonce(harness, record.request_id))

        # The same session asks for another command. The approval above covers the first
        # command only, so the second action suspends again and expires.
        second = await harness.executor.handle(_request(command="rm -rf /var/log"))

    assert first.ok
    assert run.call_count == 1
    assert not second.ok

    replay = harness.tokens.consume(
        nonce=nonces[0],
        session_id="s1",
        target_digest=compute_target_digest(command="rm -rf /var/log", hosts=("10.0.1.5",)),
    )

    assert not replay.ok
    assert replay.refusal in (TokenRefusal.ALREADY_USED, TokenRefusal.DIGEST_MISMATCH)


@pytest.mark.asyncio
async def test_the_token_cannot_be_replayed_by_another_session(tmp_path: Path) -> None:
    """#12 rule 4 again. A different session needs a different approval."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        await task
        nonce = _answered_nonce(harness, record.request_id)

    replay = harness.tokens.consume(
        nonce=nonce, session_id="s2", target_digest=record.target_digest
    )

    assert not replay.ok


# ------------------------------------------------------------------------ the record


@pytest.mark.asyncio
async def test_the_suspension_record_lands_before_the_wait(tmp_path: Path) -> None:
    """#16 raises on a write failure, so an action nothing recorded must not wait either."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with (
        patch.object(harness.audit, "record", side_effect=OSError("disk full")),
        patch(_SSH_BACKEND, new=AsyncMock()) as run,
    ):
        response = await harness.executor.handle(_request())

    assert not response.ok
    assert response.error
    run.assert_not_called()
    assert harness.pending.pending() == ()


@pytest.mark.asyncio
async def test_the_record_names_the_actor_and_both_paths(tmp_path: Path) -> None:
    """A reviewer needs to see who approved, and on which path (#13, #16)."""
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        await task

    granted = [item for item in harness.audit.read_all() if item["decision"] == "allow"]
    assert len(granted) == 1
    assert granted[0]["actor"] == "operator-1"
    assert granted[0]["origin_path"] == "telegram"
    assert granted[0]["approval_path"] == "webui"
    assert granted[0]["same_path"] is False


@pytest.mark.asyncio
async def test_no_record_carries_the_token_nonce(tmp_path: Path) -> None:
    """The audit log has more readers than the approval path has.

    A nonce in that log is a bearer value in a second place, so the record names the approval
    and omits the means to spend it (#12).
    """
    _ssh_server(tmp_path)
    harness = _Harness(tmp_path, _gates())

    with patch(_SSH_BACKEND, new=AsyncMock(return_value=_ok())):
        task = asyncio.create_task(harness.executor.handle(_request()))
        record = await harness.wait_for_one_pending(task=task)
        harness.service.approve(
            request_id=record.request_id,
            actor="operator-1",
            approval_path="webui",
            target_digest=record.target_digest,
        )
        await task
        nonce = _answered_nonce(harness, record.request_id)

    assert nonce
    assert nonce not in str(harness.audit.read_all())


def _answered_nonce(harness: _Harness, request_id: str) -> str:
    """Read the nonce the operator's answer produced, straight out of the store.

    No production path hands a nonce to the agent, so a test cannot read one from a response.
    """
    outcome = harness.pending.wait(request_id)
    nonce = outcome.token_nonce
    assert nonce is not None
    return nonce


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


async def test_a_refusal_reports_itself_instead_of_timing_out(tmp_path: Path) -> None:
    """The half of the wait that decides what a failure teaches (#82).

    A wait that only watched the pending store reported "the executor never suspended an
    action" for a run where the executor **refused** — the symptom, and never the cause. The
    refusal is already in the hand of the finished task, so the wait reads it.

    This drives the case directly: a task that is already done, and a pending store that will
    stay empty. The wait must answer at once and name what the task answered.
    """
    finished: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    finished.set_result("refused: no approver on a second path")
    task = asyncio.ensure_future(finished)
    await asyncio.sleep(0)

    harness = _Harness(tmp_path, GatesConfig())
    with pytest.raises(AssertionError, match="answered instead of suspending"):
        await harness.wait_for_one_pending(timeout_s=30.0, task=task)
