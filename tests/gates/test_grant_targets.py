# tests/gates/test_grant_targets.py
"""Item 22 (#24): match standing grants on the resolved target, not the label.

A grant lists inventory names. A name is mutable. #23 stops an unattended context from
editing the inventory, but an interactive operator can still repoint a record, and a later
automation must not inherit a redirected grant from that edit.

So the gate resolves each grant host through the same resolver the action used, and compares
resolved targets.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates import policy as policy_module
from nanoinfra.gates.policy import Outcome, evaluate
from nanoinfra.servers.store import ServerStore

AUTOMATION = "automation"


def _policy() -> GatesConfig:
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant", "group": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["unattended"],
                    "hosts": ["staging-web-01"],
                    "commands": ["systemctl reload nginx"],
                }
            ],
        }
    )


def _store(tmp_path: Path, host: str = "10.0.1.5") -> ServerStore:
    store = ServerStore(tmp_path)
    store.create({"name": "staging-web-01", "providerId": "ssh", "config": {"host": host}})
    return store


def test_a_grant_host_name_matches_its_resolved_address(tmp_path: Path) -> None:
    """An operator writes the inventory name. Before this item only the address matched."""
    store = _store(tmp_path)

    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        servers=store,
    )

    assert decision.outcome is Outcome.ALLOW
    assert decision.grant_id == "reload"


def test_a_repointed_record_stops_matching(tmp_path: Path) -> None:
    """The redirect this item exists to stop. The name still matches, the target does not."""
    store = _store(tmp_path)
    server = store.list_servers()[0]
    store.update(
        server.id,
        {
            "name": "staging-web-01",
            "providerId": "ssh",
            "config": {"host": "10.9.9.9"},
            "secretRef": None,
            "tags": [],
        },
    )

    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        servers=store,
    )

    assert decision.outcome is Outcome.DENY


def test_the_repointed_grant_now_covers_the_new_address(tmp_path: Path) -> None:
    """Stated so the behaviour is deliberate: the grant follows the record it names.

    #23 stops an automation making that edit. An interactive operator who repoints a record
    is changing what they granted, on purpose, and the audit record carries the resolved
    targets so a reviewer sees which addresses ran.
    """
    store = _store(tmp_path, host="10.9.9.9")

    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("10.9.9.9",),
        command="systemctl reload nginx",
        servers=store,
    )

    assert decision.outcome is Outcome.ALLOW


def test_a_grant_host_that_resolves_to_nothing_never_matches(tmp_path: Path) -> None:
    """A grant naming a record that no longer exists must not match anything."""
    store = ServerStore(tmp_path)

    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        servers=store,
    )

    assert decision.outcome is Outcome.DENY


def test_a_literal_address_in_a_grant_still_matches(tmp_path: Path) -> None:
    """Until this item, a grant had to list resolved addresses. Those configs must keep working."""
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "by-address",
                    "contexts": ["unattended"],
                    "hosts": ["10.0.1.5"],
                    "commands": ["systemctl reload nginx"],
                }
            ],
        }
    )

    decision = evaluate(
        gates,
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        servers=_store(tmp_path),
    )

    assert decision.outcome is Outcome.ALLOW


def test_without_an_inventory_the_gate_compares_labels_only(tmp_path: Path) -> None:
    """`servers` is optional so #23's inventory gate can call evaluate with no store.

    An inventory write reaches no host, so there is nothing to resolve. A caller that passes
    no store gets label comparison, which is what every pre-#24 caller already had.
    """
    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("staging-web-01",),
        command="systemctl reload nginx",
    )

    assert decision.outcome is Outcome.ALLOW


def test_the_decision_reports_the_resolved_targets_for_the_audit_record(tmp_path: Path) -> None:
    """#16 records which addresses a grant permitted, beside the grant id."""
    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=("10.0.1.5",),
        command="systemctl reload nginx",
        servers=_store(tmp_path),
    )

    assert decision.resolved_targets == ("10.0.1.5",)


@pytest.mark.asyncio
async def test_a_grant_written_with_an_inventory_name_runs_end_to_end(tmp_path: Path) -> None:
    """The operator-facing point of this item.

    Before #24 a grant had to list the resolved address, so the natural config -- the name the
    operator sees in the inventory -- silently matched nothing.

    This drives the executor rather than the tool. #18 moved the gate, the credential store, and
    the transports there, and the tool is now a thin client over a socket.
    """
    from unittest.mock import AsyncMock, patch

    from nanoinfra.gates.executor.protocol import ExecuteRequest
    from nanoinfra.gates.executor.server import Executor
    from nanoinfra.secrets import crypto
    from nanoinfra.servers.execution.base import ExecutionResult

    crypto_key = crypto.generate_key_for_setup()
    store = _store(tmp_path)
    assert store is not None
    executor = Executor(workspace=tmp_path, gates_loader=_policy)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)
    request = ExecuteRequest(
        server_id_or_name="staging-web-01",
        command="systemctl reload nginx",
        session_id="s1",
        execution_context=AUTOMATION,
        preview_requested=False,
        timeout_s=None,
        token_nonce=None,
    )

    with (
        patch.dict("os.environ", {"NANOINFRA_SECRETS_KEY": crypto_key}),
        patch(
            "nanoinfra.servers.execution.ssh_backend.SSHBackend.run",
            new=AsyncMock(return_value=fake),
        ) as run,
    ):
        response = await executor.handle(request)

    assert response.ok
    run.assert_called_once()


@pytest.mark.parametrize("changed_host", ["10.0.1.6", "10.0.1.50"])
def test_a_near_miss_address_does_not_match(tmp_path: Path, changed_host: str) -> None:
    decision = evaluate(
        _policy(),
        capability_class=MUTATE_REMOTE,
        scope="host",
        execution_context=AUTOMATION,
        hosts=(changed_host,),
        command="systemctl reload nginx",
        servers=_store(tmp_path),
    )

    assert decision.outcome is Outcome.DENY


def test_one_evaluation_resolves_each_grant_host_once(tmp_path: Path) -> None:
    """#35: a real resolve shells out to ansible, so it must not repeat inside one decision.

    Every grant in the policy is checked against the same action, so a naive loop resolves the
    same host once per grant. The cache lives for one evaluate() call and no longer: #24
    re-resolves on purpose, so an inventory write between two actions must still invalidate a
    match.
    """
    store = _store(tmp_path)
    gates = GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            # Three grants that all name the same host. Only the last one matches the command,
            # so the evaluator walks every grant before it decides.
            "standingGrants": [
                {"id": "a", "contexts": ["unattended"], "hosts": ["staging-web-01"],
                 "commands": ["one"]},
                {"id": "b", "contexts": ["unattended"], "hosts": ["staging-web-01"],
                 "commands": ["two"]},
                {"id": "c", "contexts": ["unattended"], "hosts": ["staging-web-01"],
                 "commands": ["systemctl reload nginx"]},
            ],
        }
    )
    calls: list[str] = []
    real = policy_module.resolve_scope_for_grant_host

    def counted(server: object) -> object:
        calls.append(getattr(server, "name", "?"))
        return real(server)

    policy_module.resolve_scope_for_grant_host = counted  # pyright: ignore[reportAttributeAccessIssue]
    try:
        decision = evaluate(
            gates,
            capability_class=MUTATE_REMOTE,
            scope="host",
            execution_context=AUTOMATION,
            hosts=("10.0.1.5",),
            command="systemctl reload nginx",
            servers=store,
        )
    finally:
        policy_module.resolve_scope_for_grant_host = real  # pyright: ignore[reportAttributeAccessIssue]

    assert decision.outcome is Outcome.ALLOW
    assert calls == ["staging-web-01"]
