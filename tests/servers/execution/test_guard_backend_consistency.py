# tests/servers/execution/test_guard_backend_consistency.py
"""The guard must validate the SAME target the backend actually dials.

This is the regression test for the whole class of finding that produced
a Critical in this module twice: execute_on_server's ``_target_host()``
read ``config["host"]`` for ansible-runner while AnsibleRunnerBackend
targeted ``inventoryHost``/``group``/``"all"``, and read ``inventoryHost``
as an ssh fallback that SSHBackend never looks at. Both mean the guard
approves an address nothing connects to.

Each case below drives the real backend with a mocked transport and
compares what it *actually* tried to reach against what ``_target_host()``
would have handed to ``validate_server_target``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from nanoinfra.agent.tools.context import (
    EXECUTION_CONTEXT_INTERACTIVE,
    RequestContext,
    request_context,
)
from nanoinfra.agent.tools.server_execution import (  # pyright: ignore[reportPrivateUsage]
    _HOST_FIELDS_BY_PROVIDER,
    ExecuteOnServerTool,
    _target_host,
)
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore
from nanoinfra.servers.types import Server

# One representative, fully-populated config per provider.
_CONFIGS: dict[str, dict[str, str]] = {
    "ssh": {"host": "10.0.1.5", "port": "22", "username": "deploy"},
    "ansible-runner": {"inventoryHost": "10.0.1.6", "projectPath": "/srv/ansible/project"},
    "ssm": {"instanceId": "i-0123456789abcdef0", "region": "us-east-1"},
    "api": {"baseUrl": "http://10.0.1.8:8080"},
}

# Config keys that carry no target meaning, kept when narrowing a config down to a
# single host field so the backend still has what it needs to run.
_NON_TARGET_KEYS = {"port", "username", "projectPath", "region"}

# Every host-shaped key any provider uses, for the "an unlisted field must not
# satisfy the guard" direction of the check.
_ALL_HOST_SHAPED_KEYS = ("host", "inventoryHost", "baseUrl")



@pytest.fixture(autouse=True)
def _interactive_turn():
    """Bind an interactive turn so the #8 gate does not refuse these tests.

    With no request context bound, execution_context falls back to unattended (#5), and #8
    refuses an unattended remote action without a standing grant. That refusal is correct.
    These tests exercise execution mechanics rather than policy, so they declare a present
    operator. Policy itself is covered by tests/agent/tools/test_unattended_enforcement.py.
    """
    ctx = RequestContext(
        channel="telegram",
        chat_id="c1",
        session_key="s1",
        execution_context=EXECUTION_CONTEXT_INTERACTIVE,
    )
    with request_context(ctx):
        yield

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


class _EmptyReader:
    async def read(self, _n: int) -> bytes:
        return b""


async def _dialed_target(provider_id: str, config: dict[str, str]) -> str | None:
    """Run the real backend against a mocked transport and report the address it
    tried to reach (None for ssm, which addresses an instance id via IAM)."""
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
        if not run_mock.called:  # backend refused: nothing to target
            return None
        _, kwargs = run_mock.call_args
        return kwargs["host_pattern"]

    if provider_id == "api":
        from nanoinfra.servers.execution.api_backend import ApiBackend

        seen: list[str | None] = []

        async def fake_send(_self: object, request: httpx.Request, **_kwargs: object) -> httpx.Response:
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
        # An instance id, not a network address -- there is nothing here for
        # validate_server_target to resolve, which is why ssm has no host check.
        assert kwargs["InstanceIds"] == [config["instanceId"]]
        return None

    raise AssertionError(f"unhandled provider {provider_id!r}")


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", sorted(_CONFIGS))
async def test_guard_checks_exactly_what_the_backend_dials(provider_id: str) -> None:
    guarded = _target_host(provider_id, _CONFIGS[provider_id])
    dialed = await _dialed_target(provider_id, _CONFIGS[provider_id])
    assert guarded == dialed, f"{provider_id}: guard validates {guarded!r}, backend dials {dialed!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_id", "field"),
    [(provider, field) for provider, fields in _HOST_FIELDS_BY_PROVIDER.items() for field in fields],
)
async def test_each_listed_host_field_alone_matches_what_the_backend_dials(
    provider_id: str, field: str
) -> None:
    """A field this table names as checkable must, on its own, be the thing the
    backend dials. ansible-runner used to list "host" here: the guard validated it
    while the backend, which never reads that key, fell through to the entire
    inventory ("all")."""
    config = {
        key: value
        for key, value in _CONFIGS[provider_id].items()
        if key == field or key in _NON_TARGET_KEYS
    }
    config.setdefault(field, "10.0.1.9")

    guarded = _target_host(provider_id, config)
    dialed = await _dialed_target(provider_id, config)
    assert guarded == dialed, (
        f"{provider_id} configured with only {field!r}: guard validates {guarded!r}, "
        f"backend dials {dialed!r}"
    )


@pytest.mark.parametrize("provider_id", sorted(_CONFIGS))
def test_unlisted_host_shaped_fields_never_satisfy_the_guard(provider_id: str) -> None:
    """The other direction: a host-shaped key this provider's backend ignores must
    not produce a validated target (ssh used to fall back to inventoryHost, which
    SSHBackend never reads; ansible-runner accepted an arbitrary extra "host" key,
    and the config schema permits arbitrary extra string keys)."""
    listed = _HOST_FIELDS_BY_PROVIDER.get(provider_id, ())
    for key in _ALL_HOST_SHAPED_KEYS:
        if key in listed:
            continue
        assert _target_host(provider_id, {key: "8.8.8.8"}) is None, (
            f"{provider_id}: {key!r} is not a target field for this provider but the guard "
            "treats it as one"
        )


def test_ssm_has_no_host_field_entry() -> None:
    assert "ssm" not in _HOST_FIELDS_BY_PROVIDER
    assert _target_host("ssm", _CONFIGS["ssm"]) is None


@pytest.mark.asyncio
async def test_ssm_execution_never_reaches_the_network_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ssm's "no host concept, authorized via IAM" design must stay unambiguous:
    validate_server_target is not called at all for this provider, rather than
    being called with a wrong or empty value."""
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    ServerStore(tmp_path).create(
        {"name": "ssm-box", "providerId": "ssm", "config": dict(_CONFIGS["ssm"])}
    )
    tool = ExecuteOnServerTool(
        servers=ServerStore(tmp_path), secrets=SecretStore(tmp_path), jobs=JobStore(tmp_path)
    )

    with (
        patch(
            "nanoinfra.agent.tools.server_execution.validate_server_target",
            new=MagicMock(return_value=(True, "")),
        ) as guard_mock,
        patch(
            "nanoinfra.servers.execution.ssm_backend.SSMBackend.run",
            new=AsyncMock(return_value=ExecutionResult(exit_code=0, output="ok", error=None)),
        ) as run_mock,
    ):
        result = await tool.execute(server_id_or_name="ssm-box", command="uptime", dry_run=False)

    assert "ok" in result
    run_mock.assert_called_once()
    guard_mock.assert_not_called()
