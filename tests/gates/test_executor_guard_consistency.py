# tests/gates/test_executor_guard_consistency.py
"""The guard must validate the SAME target the backend dials -- nanoinfraorg/nanoinfra#18.

This is the regression test for the whole class of finding that produced a Critical in this area
twice. The old ``_target_host()`` read ``config["host"]`` for ansible-runner while
AnsibleRunnerBackend targeted ``inventoryHost``, ``group``, or ``"all"``. It also read
``inventoryHost`` as an ssh fallback that SSHBackend never looks at. Both mean the guard approves
an address nothing connects to.

#18 moved the guard out of the tool and into ``nanoinfra/gates/executor/server.py``, where
``_guard`` inlines the old ``_HOST_FIELDS_BY_PROVIDER`` table as a branch per provider. So the
table lives in this module now, as the declared contract, and every test below holds the real
``_guard`` to it. The guard reports a refusal rather than a target, so these tests observe which
addresses it hands to ``validate_server_target``. That is the same question in the new shape.

Each case drives the real backend with a mocked transport. It then compares what the backend
actually tried to reach against what the guard checked.

The rule has two forms, because a backend targets two kinds of field (#9). An ADDRESS field
(``host``, ``inventoryHost``, ``baseUrl``) must equal the one value the backend dials. A PATTERN
field (``group``) names an inventory label, so the guard expands it with #4's resolver and checks
every host it names. Its rule compares host SETS instead, and the label itself must never pass as
an address. Both forms answer one question. Does the guard check what the backend reaches?
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.executor import server as executor_module
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import (
    Executor,
    _guard,  # pyright: ignore[reportPrivateUsage]
)
from nanoinfra.secrets import crypto
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.scope import resolve_scope
from nanoinfra.servers.store import ServerStore
from nanoinfra.servers.types import Server

# The contract the executor's ``_guard`` must honour, one entry per provider that names a target.
# It used to be a table in production code. ``_guard`` inlines it now, so the declaration lives
# here and the tests below prove the branches agree with it. ssm is absent on purpose: it carries
# no dialed address, and IAM authorizes the call.
_HOST_FIELDS_BY_PROVIDER: dict[str, tuple[str, ...]] = {
    "ssh": ("host",),
    "ansible-runner": ("inventoryHost", "group"),
    "api": ("baseUrl",),
}

# One representative, fully-populated config per provider.
_CONFIGS: dict[str, dict[str, str]] = {
    "ssh": {"host": "10.0.1.5", "port": "22", "username": "deploy"},
    "ansible-runner": {"inventoryHost": "10.0.1.6", "projectPath": "/srv/ansible/project"},
    "ssm": {"instanceId": "i-0123456789abcdef0", "region": "us-east-1"},
    "api": {"baseUrl": "http://10.0.1.8:8080"},
}

# Config keys that carry no target meaning. They stay when a config narrows to a single host
# field, so the backend still has what it needs to run.
_NON_TARGET_KEYS = {"port", "username", "projectPath", "region"}

# Every host-shaped key any provider uses, for the "an unlisted field must not satisfy the guard"
# direction of the check.
_ALL_HOST_SHAPED_KEYS = ("host", "inventoryHost", "baseUrl")

# Listed fields that name an inventory PATTERN rather than an address (#9). One
# validate_server_target call cannot check these. The guard expands the pattern first and checks
# every host it names, so their consistency rule compares host sets.
_PATTERN_FIELDS: dict[str, tuple[str, ...]] = {"ansible-runner": ("group",)}

# One group of three, written as addresses so the guard parses them and no test needs DNS.
_GROUP_INVENTORY = "[web]\n10.0.1.11\n10.0.1.12\n10.0.1.13\n"


def _address_fields(provider_id: str) -> tuple[str, ...]:
    patterns = _PATTERN_FIELDS.get(provider_id, ())
    return tuple(
        field for field in _HOST_FIELDS_BY_PROVIDER.get(provider_id, ()) if field not in patterns
    )


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _server(provider_id: str, config: dict[str, str]) -> Server:
    return Server(
        id="a" * 32,
        name=f"test-{provider_id}",
        provider_id=provider_id,
        config=dict(config),
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


def _request(**over: object) -> ExecuteRequest:
    """One request with an interactive context, so #8's unattended rule does not answer first.

    These tests ask a guard question, not a policy question. Policy lives in
    tests/agent/tools/test_unattended_enforcement.py.
    """
    fields: dict[str, Any] = {
        "server_id_or_name": "unset",
        "command": "true",
        "session_id": "s1",
        "execution_context": "interactive",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _interactive_allow() -> GatesConfig:
    """Interactive policy that permits the action, so these tests ask a guard question.

    The shipped interactive default is ``approve``, and #38 suspends an ``approve`` outcome
    until an operator answers on a second path. The guard is what these tests check, so they
    declare the permission. tests/gates/test_approval_gate.py drives an approval.
    """
    return GatesConfig.model_validate(
        {"interactive": {"mutate.remote": {"host": "allow", "group": "allow"}}}
    )


@dataclass(frozen=True)
class _GuardResult:
    """What the guard checked, and what it answered."""

    checked: tuple[str, ...]
    refusal: str | None


def _guard_result(provider_id: str, config: dict[str, str]) -> _GuardResult:
    """Run the real ``_guard`` and report every address it handed the network guard.

    ``validate_server_target`` always answers yes here. The question is which value reaches it,
    and not whether that value is a safe address.
    """
    checked: list[str] = []

    def recording_guard(host: str) -> tuple[bool, str]:
        checked.append(host)
        return True, ""

    with patch.object(executor_module, "validate_server_target", new=recording_guard):
        refusal = _guard(_server(provider_id, config))
    return _GuardResult(checked=tuple(checked), refusal=refusal)


def _guarded_target(provider_id: str, config: dict[str, str]) -> str | None:
    """The one address the guard validated, or None when it validated no address.

    This is the successor of the old ``_target_host()``. A guard that checks a set of hosts
    belongs to the pattern form of the rule below, so more than one checked host is a bug in the
    caller of this helper.
    """
    result = _guard_result(provider_id, config)
    assert len(result.checked) <= 1, (
        f"{provider_id}: the guard checked {result.checked}, which is a host set rather than one "
        "address. Use the pattern form of the rule."
    )
    return result.checked[0] if result.checked else None


class _EmptyReader:
    async def read(self, _n: int) -> bytes:
        return b""


async def _dialed_target(provider_id: str, config: dict[str, str]) -> str | None:
    """Run the real backend against a mocked transport, and report the address it reached.

    The answer is None for ssm, which addresses an instance id through IAM.
    """
    server = _server(provider_id, config)

    if provider_id == "ssh":
        from nanoinfra.servers.execution.ssh_backend import SSHBackend

        process = MagicMock()
        process.stdout = _EmptyReader()
        process.stderr = _EmptyReader()
        process.wait = AsyncMock(return_value=MagicMock(exit_status=0))
        process.__aenter__ = AsyncMock(return_value=process)
        process.__aexit__ = AsyncMock(return_value=False)
        conn = MagicMock()
        conn.create_process = AsyncMock(return_value=process)
        conn.__aenter__ = AsyncMock(return_value=conn)
        conn.__aexit__ = AsyncMock(return_value=False)
        connect_mock = AsyncMock(return_value=conn)
        with patch("asyncssh.connect", connect_mock):
            await SSHBackend().run(server, "true", None, on_activity=lambda _c: None)
        _, kwargs = connect_mock.call_args
        return kwargs["host"] or None

    if provider_id == "ansible-runner":
        from nanoinfra.servers.execution.ansible_backend import AnsibleRunnerBackend

        runner = MagicMock()
        runner.rc = 0
        runner.status = "successful"
        runner.stdout.read.return_value = "ok"
        with patch("ansible_runner.run", return_value=runner) as run_mock:
            await AnsibleRunnerBackend().run(server, "true", None, on_activity=lambda _c: None)
        if not run_mock.called:  # the backend refused: it had nothing to target
            return None
        _, kwargs = run_mock.call_args
        return kwargs["host_pattern"]

    if provider_id == "api":
        from nanoinfra.servers.execution.api_backend import ApiBackend

        seen: list[str | None] = []

        async def fake_send(
            _self: object, request: httpx.Request, **_kwargs: object
        ) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(200, text="ok", request=request)

        with patch.object(httpx.AsyncClient, "send", fake_send):
            await ApiBackend().run(server, "/status", None, on_activity=lambda _c: None)
        return seen[0] if seen else None

    if provider_id == "ssm":
        from nanoinfra.servers.execution.ssm_backend import SSMBackend

        client = MagicMock()
        client.send_command.return_value = {"Command": {"CommandId": "cmd-1"}}
        client.get_command_invocation.return_value = {
            "Status": "Success",
            "ResponseCode": 0,
            "StandardOutputContent": "ok",
            "StandardErrorContent": "",
        }
        with patch("boto3.client", return_value=client):
            await SSMBackend().run(
                server, "true", None, on_activity=lambda _c: None, poll_interval_s=0.001
            )
        _, kwargs = client.send_command.call_args
        # An instance id, and not a network address. Nothing here resolves for
        # validate_server_target, which is why ssm has no host check.
        assert kwargs["InstanceIds"] == [config["instanceId"]]
        return None

    raise AssertionError(f"unhandled provider {provider_id!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", sorted(_CONFIGS))
async def test_the_guard_checks_exactly_what_the_backend_dials(provider_id: str) -> None:
    guarded = _guarded_target(provider_id, _CONFIGS[provider_id])
    dialed = await _dialed_target(provider_id, _CONFIGS[provider_id])

    assert guarded == dialed, (
        f"{provider_id}: guard validates {guarded!r}, backend dials {dialed!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "field"),
    [
        (provider, field)
        for provider in _HOST_FIELDS_BY_PROVIDER
        for field in _address_fields(provider)
    ],
)
async def test_each_listed_address_field_alone_matches_what_the_backend_dials(
    provider_id: str, field: str
) -> None:
    """An address field this table names must, on its own, be the thing the backend dials.

    ansible-runner once listed "host" here. The guard validated it while the backend, which never
    reads that key, fell through to the entire inventory ("all").

    Pattern fields answer to the set form of the same rule below.
    """
    config = {
        key: value
        for key, value in _CONFIGS[provider_id].items()
        if key == field or key in _NON_TARGET_KEYS
    }
    config.setdefault(field, "10.0.1.9")

    guarded = _guarded_target(provider_id, config)
    dialed = await _dialed_target(provider_id, config)

    assert guarded == dialed, (
        f"{provider_id} configured with only {field!r}: guard validates {guarded!r}, "
        f"backend dials {dialed!r}"
    )


@pytest.mark.asyncio
async def test_a_pattern_field_guards_every_host_the_backend_pattern_reaches(
    tmp_path: Path,
) -> None:
    """The set form of the rule (#9), checked in one real execution through the executor.

    The backend hands ansible a pattern, so "what it dials" is every host that pattern covers in
    the inventory it passes. The guard must therefore check that whole set. One validated host
    beside a pattern that reaches three is the same bypass this module exists for, only wider.
    """
    project = tmp_path / "ansible-project"
    project.mkdir()
    (project / "inventory").write_text(_GROUP_INVENTORY, encoding="utf-8")
    config = {"group": "web", "projectPath": str(project)}
    ServerStore(tmp_path).create(
        {"name": "ansible-group", "providerId": "ansible-runner", "config": dict(config)}
    )
    checked: list[str] = []

    def recording_guard(host: str) -> tuple[bool, str]:
        checked.append(host)
        return True, ""

    runner = MagicMock()
    runner.rc = 0
    runner.status = "successful"
    runner.stdout.read.return_value = "ok"

    with (
        patch.object(executor_module, "validate_server_target", new=recording_guard),
        patch("ansible_runner.run", return_value=runner) as run_mock,
    ):
        response = await Executor(
            workspace=tmp_path, gates_loader=_interactive_allow
        ).handle(_request(server_id_or_name="ansible-group"))

    assert response.ok, response.error or response.reason
    assert "ok" in response.output
    _, kwargs = run_mock.call_args
    dialed = kwargs["host_pattern"]
    reached = resolve_scope(_server("ansible-runner", config)).hosts
    assert sorted(checked) == sorted(reached), (
        f"guard checked {sorted(checked)}, backend pattern {dialed!r} reaches {sorted(reached)}"
    )
    # The label itself is never an argument to the guard. A resolvable name that happens to match
    # a group would otherwise pass as an address while the group covered other hosts.
    assert dialed not in checked


def test_a_pattern_field_is_listed_but_never_passes_as_an_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pattern field stays in the contract, because the backend targets it.

    Two halves. Absent from the contract, nothing would owe it a check, and a config that carried
    only that field would reach a backend unguarded. Treated as an address, the guard would
    validate a label instead of the hosts behind it.

    The chdir keeps the answer a fact about the guard. The config names no projectPath, so the
    resolver reads the inventory beside the working directory, and an empty one leaves the host
    set unknown.
    """
    monkeypatch.chdir(tmp_path)
    for provider_id, patterns in _PATTERN_FIELDS.items():
        for field in patterns:
            assert field in _HOST_FIELDS_BY_PROVIDER[provider_id]
            result = _guard_result(provider_id, {field: "web"})
            assert result.checked == (), (
                f"{provider_id}: the guard passed the label {field!r} to the network guard as an "
                "address"
            )
            # No hosts checked means no execution. An unexpandable pattern is an unknown blast
            # radius, and the guard refuses it rather than proceeds unguarded.
            assert result.refusal is not None


@pytest.mark.parametrize("provider_id", sorted(_CONFIGS))
def test_unlisted_host_shaped_fields_never_satisfy_the_guard(provider_id: str) -> None:
    """The other direction. A host-shaped key this provider's backend ignores must not produce a
    validated target.

    ssh once fell back to inventoryHost, which SSHBackend never reads. ansible-runner accepted an
    arbitrary extra "host" key, and the config schema permits arbitrary extra string keys.
    """
    listed = _HOST_FIELDS_BY_PROVIDER.get(provider_id, ())
    for key in _ALL_HOST_SHAPED_KEYS:
        if key in listed:
            continue
        result = _guard_result(provider_id, {key: "8.8.8.8"})
        assert result.checked == (), (
            f"{provider_id}: {key!r} is not a target field for this provider but the guard "
            "treats it as one"
        )
        if provider_id == "ssm":
            # ssm names no address at all, so the guard checks nothing and refuses nothing. Every
            # other provider owes an address, so an absent one is a refusal.
            continue
        assert result.refusal is not None


def test_ssm_has_no_host_field_entry() -> None:
    """ssm carries no dialed address, so it owes the guard no check."""
    assert "ssm" not in _HOST_FIELDS_BY_PROVIDER
    result = _guard_result("ssm", _CONFIGS["ssm"])

    assert result.checked == ()
    assert result.refusal is None


@pytest.mark.asyncio
async def test_ssm_execution_never_reaches_the_network_guard(tmp_path: Path) -> None:
    """ssm's "no host concept, authorized through IAM" design must stay unambiguous.

    validate_server_target is not called at all for this provider. A call with a wrong or an
    empty value would leave the design ambiguous instead.
    """
    ServerStore(tmp_path).create(
        {"name": "ssm-box", "providerId": "ssm", "config": dict(_CONFIGS["ssm"])}
    )

    with (
        patch.object(
            executor_module,
            "validate_server_target",
            new=MagicMock(return_value=(True, "")),
        ) as guard_mock,
        patch(
            "nanoinfra.servers.execution.ssm_backend.SSMBackend.run",
            new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="ok", error=None)),
        ) as run_mock,
    ):
        response = await Executor(workspace=tmp_path, gates_loader=_interactive_allow).handle(
            _request(server_id_or_name="ssm-box", command="uptime")
        )

    assert response.ok, response.error or response.reason
    assert "ok" in response.output
    run_mock.assert_called_once()
    guard_mock.assert_not_called()


def test_an_inventory_host_that_names_a_group_gets_every_host_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`inventoryHost` is a pattern to ansible, so the guard must treat it as one.

    AnsibleRunnerBackend passes `inventoryHost or group` to ansible as host_pattern, and
    resolve_scope expands that same field. The guard used to validate the field as a single
    address, so a label naming three hosts was checked as one name.

    Today that fails closed only by accident of DNS. A group label that does resolve to a
    permitted address would pass the guard while the play ran against every host in the group,
    which is the bypass this whole module exists to prevent.
    """
    project = tmp_path / "proj"
    project.mkdir()
    (project / "inventory").write_text(
        "[web]\n10.0.1.11\n10.0.1.12\n10.0.1.13\n", encoding="utf-8"
    )
    store = ServerStore(tmp_path)
    server = store.create(
        {
            "name": "group-by-inventory-host",
            "providerId": "ansible-runner",
            "config": {"inventoryHost": "web", "projectPath": str(project)},
        }
    )
    checked: list[str] = []

    def record(host: str) -> tuple[bool, str | None]:
        checked.append(host)
        return True, None

    monkeypatch.setattr(executor_module, "validate_server_target", record)
    refusal = executor_module._guard(server)

    assert refusal is None
    assert checked == ["10.0.1.11", "10.0.1.12", "10.0.1.13"]
    assert "web" not in checked


def test_an_unresolvable_inventory_host_still_checks_the_literal_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ansible reads ansible.cfg and /etc/ansible/hosts, and the resolver cannot see either.

    So a config with no local inventory keeps the single-address check it had before. That
    preserves interactive runs that work today, and it still fails closed for a group name,
    because a group name does not resolve in DNS.
    """
    store = ServerStore(tmp_path)
    server = store.create(
        {
            "name": "no-local-inventory",
            "providerId": "ansible-runner",
            "config": {"inventoryHost": "10.0.5.5", "projectPath": str(tmp_path / "absent")},
        }
    )
    checked: list[str] = []

    def record(host: str) -> tuple[bool, str | None]:
        checked.append(host)
        return True, None

    monkeypatch.setattr(executor_module, "validate_server_target", record)
    refusal = executor_module._guard(server)

    assert refusal is None
    assert checked == ["10.0.5.5"]
