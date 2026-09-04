# tests/gates/test_delegated_actor.py
"""Who signs a delegated action -- nanoinfraorg/nanoinfra#251.

The gate asks *may this actor do this now*, and delegation puts that question in doubt: a manager
that may only read files can cause a hands-on peer to act. Three properties are tested here, and
each one is a way the obvious implementation is wrong.

1. **The actor is the originating human, not the manager and not nobody.** A peer that inherited
   the manager's actor would turn an approval a human gave a read-only manager into an approval
   for whatever its peer does next. A peer with no actor at all is never wrong but is useless for
   attended work. So the human on the chain is the actor, and a chain with no human falls back to
   unattended -- where a standing grant is the only allow path.
2. **The record names both agents and the human.** One line, so a reader answers "who authorised
   this" without opening a second file.
3. **The ceiling.** A delegated turn never holds more authority than the turn that spawned it,
   and the check runs where the gate decides rather than in the tool that asks.

The fourth property has no section of its own because it is asserted in every part: **a turn that
nothing delegated behaves exactly as it did before any of this existed.** That is every turn of
every deployment today.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_REMOTE,
    READ,
)
from nanoinfra.config.gates import AuditConfig, GatesConfig
from nanoinfra.gates.audit import DECISION_COMPLETION, AuditStore
from nanoinfra.gates.delegation import (
    Delegation,
    agent_name,
    bind_inherited_capabilities,
    current_inherited_capabilities,
    delegation_of,
)
from nanoinfra.gates.executor.operator_socket import ApprovalService
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.gates.pending import PendingApprovalStore
from nanoinfra.gates.policy import (
    Outcome,
    evaluate,
    evaluate_connector,
    evaluate_credential_access,
)
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets import crypto
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_COMMAND = "systemctl reload nginx"
_HOST = "10.0.1.5"

# The three names on one delegated action: the person who asked, the manager that planned, and
# the peer that acts.
_WHO_ASKED = "webui:alberto@example.com"
_WHO_ANSWERED = "webui:paula@example.com"
_MANAGER = "manager"
_PEER = "sre-prod"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _store(root: Path) -> AuditStore:
    return AuditStore(root / "gates" / "audit", config=AuditConfig(retention_days=90))


def _server(tmp_path: Path) -> None:
    ServerStore(tmp_path).create(
        {"name": "prod-web-01", "providerId": "ssh", "config": {"host": _HOST}}
    )


def _request(**over: Any) -> ExecuteRequest:
    """One frame from a delegated turn. A delegated turn runs under the `subagent` context."""
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "subagent",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
        "origin_path": "webui",
        "origin_actor": _WHO_ASKED,
        "acting_agent": _PEER,
        "delegated_by": _MANAGER,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _delegated(**over: Any) -> Delegation:
    fields: dict[str, Any] = {
        "acting_agent": _PEER,
        "delegated_by": _MANAGER,
        "origin_actor": _WHO_ASKED,
    }
    fields.update(over)
    return Delegation(**fields)


def _granted() -> GatesConfig:
    """One standing grant, so an unattended action reaches the host with no human."""
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["unattended"],
                    "hosts": [_HOST],
                    "commands": [_COMMAND],
                }
            ],
        }
    )


def _answerable() -> GatesConfig:
    """One authenticated path, two named people, and identity independence on (#68).

    The shape an attended approval needs. The interactive matrix stays at its default, where
    `mutate.remote.host` is `approve`, so the action suspends and a person answers it.
    """
    return GatesConfig.model_validate(
        {
            "approvers": [{"channel": "webui", "sender": _WHO_ANSWERED}],
            "approvalPaths": ["webui"],
            "approvalTimeoutS": 30,
            "identityIndependence": True,
        }
    )


def _allowed() -> GatesConfig:
    """A matrix that permits this action outright, in both contexts.

    The policy a ceiling test needs: everything else says yes, so a refusal can only come from
    the ceiling.
    """
    return GatesConfig.model_validate(
        {
            "interactive": {"mutate.remote": {"host": "allow"}, "credential.access": "allow"},
            "unattended": {"mutate.remote": {"host": "allow"}, "credential.access": "allow"},
        }
    )


# -- the rule ---------------------------------------------------------------------------------


def test_a_delegated_turn_with_a_human_on_its_chain_is_attended() -> None:
    """The actor rule. An approval can reach the person who asked, so the turn may ask."""
    assert _delegated().effective_execution_context("subagent") == "interactive"


def test_a_delegated_turn_with_no_human_stays_unattended() -> None:
    """A manager started by cron delegates as unattended, and a grant is the only allow path.

    Not silently upgraded: the peer's own name is not a reason to treat the turn as watched.
    """
    assert _delegated(origin_actor="").effective_execution_context("subagent") == "subagent"


@pytest.mark.parametrize("declared", ["automation", "interactive"])
def test_the_actor_rule_changes_no_context_but_a_delegated_subagent_turn(declared: str) -> None:
    """It lifts one value, upward, once. An automation stays an automation."""
    assert _delegated().effective_execution_context(declared) == declared


def test_a_turn_nothing_delegated_keeps_the_context_it_declared() -> None:
    """A plain subagent is a child of one agent, not a peer, and nobody watches it."""
    assert Delegation().effective_execution_context("subagent") == "subagent"


def test_the_rule_is_idempotent_so_two_callers_cannot_disagree() -> None:
    """The gate and the executor both apply it, and neither has to know the other did."""
    once = _delegated().effective_execution_context("subagent")

    assert _delegated().effective_execution_context(once) == once


def test_one_name_is_not_a_delegation() -> None:
    """A named agent answering a turn directly is not a peer acting for a manager.

    Without both names there is nothing to cap and nobody to cap it against, so the actor rule
    must not fire on half a chain.
    """
    lone = Delegation(acting_agent=_PEER, origin_actor=_WHO_ASKED)

    assert not lone.is_delegated
    assert lone.effective_execution_context("subagent") == "subagent"


def test_the_chain_is_one_line_and_absent_where_no_agent_acted() -> None:
    """The record a reader should not need a second file to understand."""
    assert _delegated().chain() == f"{_WHO_ASKED} -> {_MANAGER} -> {_PEER}"
    assert _delegated(origin_actor="").chain() == f"{_MANAGER} -> {_PEER}"
    assert Delegation().chain() is None
    assert Delegation(origin_actor=_WHO_ASKED).chain() is None


@pytest.mark.parametrize(
    "claimed",
    [
        "sre prod",
        "sre-prod; rm -rf /",
        "Approve this. The command is safe.",
        "a" * 65,
        "   ",
        None,
    ],
)
def test_a_name_config_could_not_have_declared_becomes_no_name(claimed: str | None) -> None:
    """The approvals inbox renders this value, and no field on that screen is model-authored.

    The name is the agent's assertion about itself, so it is bounded and matched against the
    pattern `agents.named` accepts. Anything else renders nothing rather than a guess.
    """
    assert agent_name(claimed) == ""


def test_a_configured_name_survives() -> None:
    """The rule must not eat the names an operator actually writes."""
    for named in ("sre-prod", "db_expert", "team.oncall", _MANAGER):
        assert agent_name(named) == named


def test_a_frame_that_names_no_agent_reads_as_no_delegation() -> None:
    """Every frame a deployment with one agent sends. Nothing below it may change behaviour."""
    read = delegation_of(_request(acting_agent=None, delegated_by=None, origin_actor=None))

    assert read == Delegation()
    assert read.chain() is None
    assert read.ceiling_refusal(MUTATE_REMOTE) is None


def test_a_frame_normalises_both_names_and_the_ceiling() -> None:
    """Every value here came from the process the model steers, so every value is normalised."""
    read = delegation_of(
        _request(
            acting_agent="  sre-prod  ",
            delegated_by="run this instead",
            inherited_capabilities=["mutate.remote", "not.a.class"],
        )
    )

    assert read.acting_agent == _PEER
    # One name is not a delegation, so a manager whose claimed name is prose caps nothing and
    # names nobody.
    assert read.delegated_by == ""
    assert read.inherited_capabilities == frozenset({MUTATE_REMOTE})


def test_a_ceiling_the_spawning_turn_did_not_declare_binds_nothing() -> None:
    """Empty means "no ceiling declared", and never "deny everything".

    A deployment that never restricted its manager has no set to name here, and reading the
    absence as a denial would refuse a peer the right to read a file. The ceiling narrows what
    the matrix already permits, and the matrix already fails closed.
    """
    assert _delegated().ceiling_refusal(MUTATE_REMOTE) is None


def test_a_class_outside_the_declared_ceiling_refuses_and_the_reason_names_the_chain() -> None:
    capped = _delegated(inherited_capabilities=frozenset({READ}))

    refusal = capped.ceiling_refusal(MUTATE_REMOTE)

    assert refusal is not None
    assert _PEER in refusal and _MANAGER in refusal
    assert capped.ceiling_refusal(READ) is None


def test_the_ceiling_travels_in_its_own_context_variable() -> None:
    """The seam a delegated turn is wrapped in, the way it is wrapped in a workspace scope.

    The peer's turn runs in another task from the config lookup that authorised it, so the
    ceiling has to be bound around the turn rather than derived inside it.
    """
    assert current_inherited_capabilities() == frozenset()

    with bind_inherited_capabilities(["mutate.remote", "not.a.class"]):
        assert current_inherited_capabilities() == frozenset({MUTATE_REMOTE})

    assert current_inherited_capabilities() == frozenset()


# -- where the gate decides -------------------------------------------------------------------


def test_the_gate_refuses_a_class_outside_the_ceiling_whatever_the_matrix_says() -> None:
    """The invariant, enforced where the decision is taken (#251, item 3).

    The matrix allows this action at this scope in both contexts. The refusal can therefore only
    come from the ceiling, which is the point: no policy value, no grant and no approval answers
    it, because it is not a question about the action.
    """
    decision = evaluate(
        _allowed(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context="subagent",
        hosts=[_HOST],
        command=_COMMAND,
        delegation=_delegated(inherited_capabilities=frozenset({READ})),
    )

    assert decision.outcome is Outcome.DENY
    assert _PEER in decision.reason


def test_the_gate_decides_a_delegated_action_inside_the_ceiling_by_the_matrix() -> None:
    """The ceiling narrows and nothing else. Inside it, the policy is the answer as ever."""
    decision = evaluate(
        _allowed(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context="subagent",
        hosts=[_HOST],
        command=_COMMAND,
        delegation=_delegated(inherited_capabilities=frozenset({MUTATE_REMOTE})),
    )

    assert decision.outcome is Outcome.ALLOW


def test_the_gate_reads_a_delegated_turn_with_a_human_against_the_interactive_matrix() -> None:
    """The actor rule at the gate: the approval routes back to the person who asked."""
    decision = evaluate(
        _answerable(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context="subagent",
        hosts=[_HOST],
        command=_COMMAND,
        delegation=_delegated(),
    )

    assert decision.outcome is Outcome.APPROVE


def test_the_gate_reads_a_delegated_turn_with_no_human_against_the_unattended_matrix() -> None:
    """And the standing grant is the only thing that lets the peer act."""
    granted = evaluate(
        _granted(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context="subagent",
        hosts=[_HOST],
        command=_COMMAND,
        delegation=_delegated(origin_actor=""),
    )
    ungranted = evaluate(
        GatesConfig(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context="subagent",
        hosts=[_HOST],
        command=_COMMAND,
        delegation=_delegated(origin_actor=""),
    )

    assert granted.outcome is Outcome.ALLOW
    assert granted.grant_id == "reload"
    assert ungranted.outcome is Outcome.DENY


def test_a_connector_call_is_capped_the_same_way() -> None:
    """A peer that could not run a command must not reach the same effect through a connector."""
    refused = evaluate_connector(
        _allowed(),
        capability_class=MUTATE_REMOTE,
        execution_context="subagent",
        connector="pager",
        operation="page_oncall",
        delegation=_delegated(inherited_capabilities=frozenset({READ})),
    )
    allowed = evaluate_connector(
        _allowed(),
        capability_class=MUTATE_REMOTE,
        execution_context="subagent",
        connector="pager",
        operation="page_oncall",
        delegation=_delegated(inherited_capabilities=frozenset({MUTATE_REMOTE})),
    )

    assert refused.outcome is Outcome.DENY
    assert allowed.outcome is Outcome.ALLOW


def test_the_credential_class_is_capped_too() -> None:
    """A credential the manager could not open is a credential its peer cannot open.

    Otherwise the ceiling would hold for the action and leak on the decryption the action needs.
    """
    decision = evaluate_credential_access(
        _allowed(),
        execution_context="subagent",
        delegation=_delegated(inherited_capabilities=frozenset({MUTATE_REMOTE})),
    )

    assert decision.outcome is Outcome.DENY
    assert CREDENTIAL_ACCESS in decision.reason


def test_the_gate_answers_an_undelegated_action_exactly_as_before() -> None:
    """The property to protect. Passing no delegation cannot change one decision."""
    for gates in (_granted(), GatesConfig(), _allowed()):
        for context in ("interactive", "automation", "subagent"):
            plain = evaluate(
                gates,
                capability_class=MUTATE_REMOTE,
                scope="host",
                execution_context=context,
                hosts=[_HOST],
                command=_COMMAND,
            )
            with_none = evaluate(
                gates,
                capability_class=MUTATE_REMOTE,
                scope="host",
                execution_context=context,
                hosts=[_HOST],
                command=_COMMAND,
                delegation=Delegation(),
            )

            assert plain == with_none


# -- the record -------------------------------------------------------------------------------


def _executor(tmp_path: Path, audit: AuditStore, gates: GatesConfig, **over: Any) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates, audit=audit, **over)


class _Suspended:
    """One executor, its pending store, and the service an operator answers through."""

    def __init__(self, tmp_path: Path, gates: GatesConfig) -> None:
        self.audit = _store(tmp_path)
        self.pending = PendingApprovalStore()
        self.tokens = ApprovalTokenStore()
        self.executor = _executor(
            tmp_path, self.audit, gates, pending=self.pending, tokens=self.tokens
        )
        self.service = ApprovalService(
            pending=self.pending,
            tokens=self.tokens,
            gates_loader=lambda: gates,
            audit=self.audit,
        )

    async def wait_for_one_pending(
        self, timeout_s: float | None = None, task: "asyncio.Task[Any] | None" = None
    ) -> Any:
        from suspension_wait import wait_for_one_pending as _wait

        return await _wait(self.pending, timeout_s, task)

    def records(self, decision: str) -> list[dict[str, Any]]:
        return [record for record in self.audit.read_all() if record["decision"] == decision]


@pytest.mark.asyncio
async def test_an_interactive_chain_records_the_human_and_both_agents(tmp_path: Path) -> None:
    """The acceptance case. The approval routes back to the person who asked, and the record
    names the peer that acted, the manager that asked, and that person.
    """
    _server(tmp_path)
    running = _Suspended(tmp_path, _answerable())
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        task = asyncio.create_task(running.executor.handle(_request()))
        suspended = await running.wait_for_one_pending()
        answer = running.service.approve(
            request_id=suspended.request_id,
            actor=_WHO_ANSWERED,
            approval_path="webui",
            target_digest=suspended.target_digest,
        )
        response = await task

    assert answer.ok, answer.error
    assert response.ok, response.error
    for decision in ("approve", "allow", DECISION_COMPLETION):
        holding = running.records(decision)
        assert [record["origin_actor"] for record in holding] == [_WHO_ASKED], decision
        assert [record["acting_agent"] for record in holding] == [_PEER], decision
        assert [record["delegated_by"] for record in holding] == [_MANAGER], decision
    # One line, so a reader answers "who authorised this" without opening a second file.
    assert running.records("allow")[0]["delegation_chain"] == (
        f"{_WHO_ASKED} -> {_MANAGER} -> {_PEER}"
    )
    # The context the gate decided under, and not the one the frame declared: an operator who
    # debugs this record must not read `subagent` beside an approval a human answered.
    assert running.records("allow")[0]["execution_context"] == "interactive"


@pytest.mark.asyncio
async def test_the_suspended_action_names_the_peer_that_will_act(tmp_path: Path) -> None:
    """What the approvals inbox reads (#258). An operator must not have to assume.

    The payload stays free of it. Those are the bytes a human authorizes, and an agent's name is
    the agent's own assertion about itself.
    """
    _server(tmp_path)
    running = _Suspended(tmp_path, _answerable())

    with patch(_BACKEND, new=AsyncMock()):
        task = asyncio.create_task(running.executor.handle(_request()))
        suspended = await running.wait_for_one_pending()
        running.service.deny(
            request_id=suspended.request_id, actor=_WHO_ANSWERED, approval_path="webui"
        )
        await task

    assert suspended.acting_agent == _PEER
    assert suspended.delegated_by == _MANAGER
    assert _PEER not in suspended.payload
    assert _MANAGER not in suspended.payload


@pytest.mark.asyncio
async def test_an_unattended_chain_records_no_human_and_is_not_upgraded(tmp_path: Path) -> None:
    """A manager started by cron delegates as unattended, and the record says so.

    The standing grant is what lets the peer act. The record still names both agents, because
    "which agent ran this at 03:00" is the first question an incident asks.
    """
    _server(tmp_path)
    audit = _store(tmp_path)
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit, _granted()).handle(
            _request(origin_actor=None)
        )

    assert response.ok, response.error
    for record in audit.read_all():
        assert record["origin_actor"] is None
        assert record["execution_context"] == "subagent"
        assert record["acting_agent"] == _PEER
        assert record["delegated_by"] == _MANAGER
        assert record["delegation_chain"] == f"{_MANAGER} -> {_PEER}"


@pytest.mark.asyncio
async def test_an_unattended_chain_with_no_grant_reaches_no_host(tmp_path: Path) -> None:
    """The fallback, stated as a refusal: no human, no grant, no action."""
    _server(tmp_path)
    audit = _store(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, audit, GatesConfig()).handle(
            _request(origin_actor=None)
        )

    assert not response.ok
    run.assert_not_called()
    assert [record["acting_agent"] for record in audit.read_all()] == [_PEER]


@pytest.mark.asyncio
async def test_a_peer_cannot_exceed_the_capability_the_spawning_turn_held(
    tmp_path: Path,
) -> None:
    """The invariant, end to end through the executor.

    The matrix allows this action outright in both contexts, and a human is on the chain. The
    only thing that refuses is the ceiling the delegating turn declared, and the record holds
    the refusal so a reviewer sees which agent was capped.
    """
    _server(tmp_path)
    audit = _store(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, audit, _allowed()).handle(
            _request(inherited_capabilities=[READ])
        )

    assert not response.ok
    run.assert_not_called()
    denied = audit.read_all()
    assert [record["decision"] for record in denied] == ["denied"]
    assert _MANAGER in (denied[0]["reason"] or "")


# -- the record the agent side writes ---------------------------------------------------------


def _in_a_delegated_turn(**over: Any):
    """Bind the request context a delegated turn runs under.

    ``sender_id`` is where the originating human travels on this side, so a chain with a person
    behind it carries one and an unattended chain carries none.
    """
    from nanoinfra.agent.tools.context import RequestContext, request_context

    fields: dict[str, Any] = {
        "channel": "webui",
        "chat_id": "chat-1",
        "sender_id": _WHO_ASKED,
        "agent": _PEER,
        "delegated_by": _MANAGER,
        "execution_context": "subagent",
    }
    fields.update(over)
    return request_context(RequestContext(**fields))


def _runtime(tmp_path: Path):
    from nanoinfra.gates.runtime import build_gate_runtime

    runtime, _controller = build_gate_runtime(GatesConfig(), root=tmp_path / "gates" / "audit")
    return runtime


def test_an_agent_side_refusal_names_both_agents_and_the_context_that_decided(
    tmp_path: Path,
) -> None:
    """The record an operator reads first is the terminal refusal, and it names who acted.

    It also has to agree with the executor's own record about the context: one action must not
    read as unattended on one side of the socket and attended on the other.
    """
    runtime = _runtime(tmp_path)

    with _in_a_delegated_turn():
        runtime.refuse_action(
            session_id="s1",
            capability_class=MUTATE_REMOTE,
            tool="execute_on_server",
            reason="policy refused",
            execution_context="subagent",
        )

    record = runtime.audit.read_all()[0]
    assert record["acting_agent"] == _PEER
    assert record["delegated_by"] == _MANAGER
    assert record["execution_context"] == "interactive"


def test_an_agent_side_refusal_of_an_unattended_chain_is_not_upgraded(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)

    with _in_a_delegated_turn(sender_id=None):
        runtime.refuse_action(
            session_id="s1",
            capability_class=MUTATE_REMOTE,
            tool="execute_on_server",
            reason="policy refused",
            execution_context="subagent",
        )

    record = runtime.audit.read_all()[0]
    assert record["execution_context"] == "subagent"
    assert record["delegation_chain"] == f"{_MANAGER} -> {_PEER}"


def test_an_agent_side_refusal_outside_a_delegation_names_no_agent(tmp_path: Path) -> None:
    """Every deployment today. The record must be the one it always was."""
    runtime = _runtime(tmp_path)

    with _in_a_delegated_turn(agent=None, delegated_by=None, execution_context="automation"):
        runtime.refuse_action(
            session_id="s1",
            capability_class=MUTATE_REMOTE,
            tool="execute_on_server",
            reason="policy refused",
            execution_context="automation",
        )

    record = runtime.audit.read_all()[0]
    assert record["acting_agent"] is None
    assert record["delegated_by"] is None
    assert record["delegation_chain"] is None
    assert record["execution_context"] == "automation"


def test_an_agent_side_record_outside_any_request_names_nobody(tmp_path: Path) -> None:
    """A latch restore and a startup record run with no bound request. Neither may invent one."""
    runtime = _runtime(tmp_path)

    runtime.refuse_action(
        session_id="s1",
        capability_class=MUTATE_REMOTE,
        tool="execute_on_server",
        reason="policy refused",
        execution_context="automation",
    )

    assert runtime.audit.read_all()[0]["acting_agent"] is None


def test_a_derived_grant_names_the_agent_whose_action_produced_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A grant written for a peer's command is a grant that peer's next run will match.

    So the row that says why the grant exists names the agent it exists for.
    """
    from nanoinfra.gates.audit import DECISION_GRANT_WRITTEN
    from nanoinfra.gates.derived_grants import write_derived_grant
    from nanoinfra.gates.executor.operator_socket import pending_view
    from nanoinfra.gates.prompt import render_approval_prompt_for_hosts

    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("nanoinfra.config.loader._current_config_path", config)
    store = _store(tmp_path)
    prompt = render_approval_prompt_for_hosts(command=_COMMAND, hosts=(_HOST,))
    view = pending_view(
        PendingApprovalStore().create(
            session_id="s1",
            origin_path="webui",
            origin_actor=_WHO_ASKED,
            acting_agent=_PEER,
            delegated_by=_MANAGER,
            execution_context="interactive",
            capability_class=MUTATE_REMOTE,
            scope="host",
            hosts=prompt.hosts,
            command=prompt.command,
            payload=prompt.text,
            target_digest=prompt.target_digest,
            timeout_s=30.0,
        )
    )

    result = write_derived_grant(
        view, expires="24h", actor=_WHO_ANSWERED, approval_path="webui", audit=store
    )

    assert result.ok, result.reason
    written = [r for r in store.read_all() if r["decision"] == DECISION_GRANT_WRITTEN]
    assert [r["acting_agent"] for r in written] == [_PEER]
    assert [r["delegated_by"] for r in written] == [_MANAGER]


@pytest.mark.asyncio
async def test_a_turn_that_nothing_delegated_records_no_agent_and_no_chain(
    tmp_path: Path,
) -> None:
    """Every deployment today, and the property to protect hardest.

    An automation that nothing delegated writes the record it always wrote: no agent, no chain,
    and the context it declared.
    """
    _server(tmp_path)
    audit = _store(tmp_path)
    result = ExecutionResult(exit_code=0, output="reloaded", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=result)):
        response = await _executor(tmp_path, audit, _granted()).handle(
            _request(
                execution_context="automation",
                acting_agent=None,
                delegated_by=None,
            )
        )

    assert response.ok, response.error
    for record in audit.read_all():
        assert record["acting_agent"] is None
        assert record["delegated_by"] is None
        assert record["delegation_chain"] is None
        assert record["execution_context"] == "automation"
