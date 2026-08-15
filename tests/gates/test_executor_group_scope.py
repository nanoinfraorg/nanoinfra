# tests/gates/test_executor_group_scope.py
"""Item 18 (#9) and the `all` scope rule, both now owned by the executor (#18).

Group execution used to be unreachable. The old tool read ``inventoryHost`` alone, so an
ansible-runner server that named only ``group`` hit the "Cannot validate network target"
refusal before anything decrypted a credential. The ``group`` scope tier therefore described
an action the system could not perform.

A group is an inventory label and not an address, so ``_guard`` expands it with #4's resolver
and checks every host the label names. Two rules carry the security property. One blocked host
refuses the whole group, because ansible cannot run on the rest only, and a partial run on
hosts nobody cleared is worse than no run. An inventory the resolver cannot read still
refuses, because an unexpandable pattern is not an empty one.

The gate stays in front (#8). Item #9 turns an impossible action into a possible one, and an
unattended group action still needs a standing grant that covers every resolved host.

`all` scope has no path to execution, in any context. #7 types that field as
``Literal["deny"]`` so a config cannot even ask for another value, and #8 states the rule as
absolute. #9 then made group execution reachable, which exposed a hole: the interactive
short-circuit returns "allowed" before policy runs, so an interactive turn could run
``group: "all"``. The short-circuit exists because the interactive ``approve`` decision has no
approval surface before #13 and #27. That reason does not extend to `all`, because `all` has no
approval path by design. So the scope refusal runs before the short-circuit.

#18 moved the guard, the resolver, and the gate out of the agent and into the executor. These
properties therefore test ``Executor.handle`` directly. The agent cannot reach a transport, so
a test against the agent would no longer prove that a host stayed untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ansible_backend.AnsibleRunnerBackend.run"
# The guard moved into the executor with the transports, so this is where it now lives.
_GUARD = "nanoinfra.gates.executor.server.validate_server_target"
_RESOLVE_SECRET = "nanoinfra.secrets.store.SecretStore.resolve_plaintext"

_SERVER = "ansible-web-group"
_COMMAND = "systemctl reload nginx"

# Addresses rather than names, so the guard parses them and no test touches DNS.
# ansible accepts an address as a host entry, so this is a real inventory.
_WEB_HOSTS = ("10.0.2.11", "10.0.2.12", "10.0.2.13")

_INVENTORY = """\
[web]
10.0.2.11
10.0.2.12
10.0.2.13

[db]
10.0.3.21
"""

# The acceptance case: one cloud-metadata host inside an otherwise routine group.
_METADATA_INVENTORY = """\
[web]
10.0.2.11
169.254.169.254
10.0.2.13
"""

# A blocked host outside the group an operator would name, for the unbounded pattern.
_WIDE_INVENTORY = """\
[web]
10.0.2.11
10.0.2.12

[db]
169.254.169.254
"""


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _project(tmp_path: Path, text: str = _INVENTORY) -> str:
    """An ansible-runner private_data_dir that holds the inventory the backend reads.

    ansible-runner passes ``-i <private_data_dir>/inventory`` when that path exists, so the
    resolver and the backend read the same file.
    """
    project = tmp_path / "ansible-project"
    project.mkdir(exist_ok=True)
    (project / "inventory").write_text(text, encoding="utf-8")
    return str(project)


def _group_server(
    tmp_path: Path,
    *,
    project_path: str,
    group: str = "web",
    secret_ref: str | None = None,
    extra: dict[str, str] | None = None,
) -> None:
    config: dict[str, str] = {"group": group, "projectPath": project_path}
    config.update(extra or {})
    raw: dict[str, Any] = {
        "name": _SERVER,
        "providerId": "ansible-runner",
        "config": config,
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


def _executor(tmp_path: Path, gates: GatesConfig | None = None) -> Executor:
    return Executor(workspace=tmp_path, gates_loader=lambda: gates or GatesConfig())


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": _SERVER,
        "command": _COMMAND,
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _policy(**over: object) -> GatesConfig:
    return GatesConfig.model_validate(over) if over else GatesConfig()


def _ok() -> ExecutionResult:
    return ExecutionResult(exit_code=0, output="ok", error=None)


async def test_a_group_config_runs_when_every_resolved_host_passes_the_guard(
    tmp_path: Path,
) -> None:
    """The gap this item closes: a group-only config used to be refused outright."""
    _group_server(tmp_path, project_path=_project(tmp_path))

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await _executor(tmp_path).handle(_request())

    assert response.ok
    assert "ok" in response.output
    run.assert_called_once()
    jobs = JobStore(tmp_path).list_jobs()
    assert len(jobs) == 1
    assert jobs[0].status == "completed"


async def test_every_resolved_host_reaches_the_guard_not_only_the_first(tmp_path: Path) -> None:
    """One validated address plus a backend that dials three is the bypass class."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    guard = MagicMock(return_value=(True, ""))

    with (
        patch(_GUARD, new=guard),
        patch(_BACKEND, new=AsyncMock(return_value=_ok())),
    ):
        await _executor(tmp_path).handle(_request())

    checked = [call.args[0] for call in guard.call_args_list]
    assert sorted(checked) == sorted(_WEB_HOSTS)


async def test_the_guard_never_checks_the_pattern_string_itself(tmp_path: Path) -> None:
    """A pattern is a label. A check of the label validates a name nothing ever dials."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    guard = MagicMock(return_value=(True, ""))

    with (
        patch(_GUARD, new=guard),
        patch(_BACKEND, new=AsyncMock(return_value=_ok())) as run,
    ):
        await _executor(tmp_path).handle(_request())

    # The run itself proves the guard ran and passed. Without it this test would also
    # hold for an executor that checked nothing at all.
    run.assert_called_once()
    assert "web" not in [call.args[0] for call in guard.call_args_list]


async def test_one_metadata_host_refuses_the_whole_group(tmp_path: Path) -> None:
    """The acceptance case. Partial execution must not happen.

    No job record, no decrypted credential, and no backend call. The guard runs before all
    three, so a blocked host inside the group stops the action before it can start.
    """
    secret = SecretStore(tmp_path).create(
        {"name": "ansible-key", "kind": "ssh_key", "providerId": "local", "value": "s3cr3t"}
    )
    _group_server(
        tmp_path,
        project_path=_project(tmp_path, _METADATA_INVENTORY),
        secret_ref=secret.id,
    )

    with (
        patch(_BACKEND, new=AsyncMock()) as run,
        patch(_RESOLVE_SECRET, new=Mock()) as resolve,
    ):
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    # The blocked address proves the expansion happened. A refusal that only named the
    # missing config field would mean the group was never expanded at all.
    assert "169.254.169.254" in str(response.error)
    run.assert_not_called()
    resolve.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


async def test_the_refusal_names_the_blocked_host_and_says_none_of_them_ran(
    tmp_path: Path,
) -> None:
    """An operator must learn which host stopped the run, and that none of them ran.

    The executor's words are narrower than the old tool's. It names the blocked host and it
    states that one blocked host refuses all of them. It no longer names the pattern or the
    host count, so this test holds the executor to the part that an operator needs.
    """
    _group_server(tmp_path, project_path=_project(tmp_path, _METADATA_INVENTORY))

    with patch(_BACKEND, new=AsyncMock()):
        response = await _executor(tmp_path).handle(_request())

    text = str(response.error)
    assert "169.254.169.254" in text
    assert "One blocked host refuses all of them." in text


async def test_an_unreadable_inventory_still_refuses(tmp_path: Path) -> None:
    """Requirement 3. The refusal survives where the expansion cannot happen."""
    empty = tmp_path / "no-inventory-here"
    empty.mkdir()
    _group_server(tmp_path, project_path=str(empty))

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    text = str(response.error)
    assert "Cannot validate network target" in text
    # The refusal carries the resolver's own reason, so it says WHY the host set is unknown.
    # The exact sentence depends on the machine, and #37 is why: with ansible-core installed
    # the resolver asks ansible for its own configuration and quotes ansible's failure, and
    # without it the resolver stops at the missing file. Both refuse, and the property under
    # test is the refusal plus a stated cause, not one wording.
    assert "inventory" in text.lower()
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


async def test_a_pattern_that_matches_nothing_refuses_rather_than_reads_as_empty(
    tmp_path: Path,
) -> None:
    """An unexpandable pattern is not an empty one, so a typo is never a safe no-op."""
    _group_server(tmp_path, project_path=_project(tmp_path), group="staging")

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    assert "Cannot validate network target" in str(response.error)
    assert "staging" in str(response.error)
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


async def test_a_host_shaped_extra_key_cannot_stand_in_for_the_group_hosts(
    tmp_path: Path,
) -> None:
    """AnsibleRunnerBackend never reads ``host``, so that key must satisfy nothing.

    A config that pairs a group with a host-shaped extra key used to be the way to make the
    guard validate a safe address while the backend targeted the group.
    """
    _group_server(
        tmp_path,
        project_path=_project(tmp_path, _METADATA_INVENTORY),
        extra={"host": "8.8.8.8"},
    )
    checked: list[str] = []

    def recording_guard(host: str) -> tuple[bool, str]:
        from nanoinfra.servers.network_guard import validate_server_target

        checked.append(host)
        return validate_server_target(host)

    with (
        patch(_GUARD, new=recording_guard),
        patch(_BACKEND, new=AsyncMock()) as run,
    ):
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    assert "8.8.8.8" not in checked
    assert "169.254.169.254" in checked
    run.assert_not_called()


async def test_the_widest_pattern_also_gets_every_host_checked(tmp_path: Path) -> None:
    """An unbounded pattern reaches the whole inventory, so the guard reads the whole set.

    ``all`` covers every host an operator adds tomorrow, and #4 keeps its scope at ``all``
    for that reason. The check is the same one a named group gets: one blocked host anywhere
    in the inventory refuses the action. The guard runs before the gate, so the refusal names
    the blocked host rather than the scope.
    """
    _group_server(tmp_path, project_path=_project(tmp_path, _WIDE_INVENTORY), group="all")

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    assert "169.254.169.254" in str(response.error)
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.skip(
    reason=(
        "No home after #18. The old tool built its own preview text and listed the resolved "
        "hosts, so an operator read the blast radius before an approval. The executor's "
        "_preview_line names the server, the provider, and the command only, and the agent "
        "renders what it gets. Somebody must decide whether the preview carries the resolved "
        "hosts again. That needs a change in nanoinfra/gates/executor/server.py, which this "
        "lane must not touch."
    )
)
async def test_a_preview_of_a_group_names_the_hosts_the_guard_validated(tmp_path: Path) -> None:
    """A pattern alone hides the blast radius, so the preview shows the resolved hosts."""
    _group_server(tmp_path, project_path=_project(tmp_path))

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(preview_requested=True))

    text = response.output
    assert "Preview (not executed)" in text
    assert "web" in text
    for host in _WEB_HOSTS:
        assert host in text
    run.assert_not_called()


async def test_an_unattended_group_call_without_a_grant_is_still_withheld(tmp_path: Path) -> None:
    """#8 stays in front. A possible action is not a permitted action."""
    _group_server(tmp_path, project_path=_project(tmp_path))

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(execution_context="automation"))

    assert not response.ok
    assert "standing grant" in response.reason.lower()
    # The withheld action still comes back as a preview, so the agent can show what stayed
    # unexecuted. The agent adds the operator-facing note (#10).
    assert "Preview (not executed)" in response.output
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


async def test_a_grant_covering_every_group_host_lets_an_unattended_group_run(
    tmp_path: Path,
) -> None:
    _group_server(tmp_path, project_path=_project(tmp_path))
    granted = _policy(
        unattended={"mutate.remote": {"group": "grant"}},
        standingGrants=[
            {
                "id": "reload-web",
                "contexts": ["unattended"],
                "hosts": list(_WEB_HOSTS),
                "commands": [_COMMAND],
            }
        ],
    )

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await _executor(tmp_path, granted).handle(
            _request(execution_context="automation")
        )

    assert response.ok
    run.assert_called_once()


async def test_a_grant_that_misses_one_group_host_refuses_the_whole_group(
    tmp_path: Path,
) -> None:
    """Partial coverage is no coverage. A group never runs on hosts nobody granted."""
    _group_server(tmp_path, project_path=_project(tmp_path))
    partial = _policy(
        unattended={"mutate.remote": {"group": "grant"}},
        standingGrants=[
            {
                "id": "reload-two-of-three",
                "contexts": ["unattended"],
                "hosts": list(_WEB_HOSTS[:2]),
                "commands": [_COMMAND],
            }
        ],
    )

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, partial).handle(
            _request(execution_context="automation")
        )

    assert not response.ok
    assert "standing grant" in response.reason.lower()
    assert "Preview (not executed)" in response.output
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.parametrize("pattern", ["all", "*"])
@pytest.mark.parametrize("execution_context", ["interactive", "automation"])
async def test_an_unbounded_pattern_never_executes(
    tmp_path: Path, pattern: str, execution_context: str
) -> None:
    """`all` scope refuses in every context, and the interactive turn is one of them.

    The interactive short-circuit inside ``_gate`` returns "allowed" before policy runs. This
    case proves the scope refusal runs ahead of that short-circuit. Otherwise an interactive
    turn would reach every host in the inventory.
    """
    _group_server(tmp_path, project_path=_project(tmp_path), group=pattern)

    with (
        patch(_BACKEND, new=AsyncMock()) as run,
        patch(_RESOLVE_SECRET, new=Mock()) as resolve,
    ):
        response = await _executor(tmp_path).handle(
            _request(execution_context=execution_context)
        )

    assert not response.ok
    run.assert_not_called()
    resolve.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []
    assert "all" in response.reason.lower()


async def test_a_bounded_group_still_executes_interactively(tmp_path: Path) -> None:
    """The refusal must be about unbounded scope, and not about groups."""
    _group_server(tmp_path, project_path=_project(tmp_path), group="web")

    with patch(_BACKEND, new=AsyncMock(return_value=_ok())) as run:
        response = await _executor(tmp_path).handle(_request())

    run.assert_called_once()
    assert response.ok


async def test_a_preview_of_an_unbounded_pattern_is_still_a_preview(tmp_path: Path) -> None:
    """A preview reaches no host, so the scope rule does not turn it into an error."""
    _group_server(tmp_path, project_path=_project(tmp_path), group="all")

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(preview_requested=True))

    assert response.ok
    run.assert_not_called()
    assert "Preview (not executed)" in response.output
