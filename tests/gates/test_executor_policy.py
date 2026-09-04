# tests/gates/test_executor_policy.py
"""Items 6 and 7 (#8, #10) as the executor enforces them after the split (#18).

These properties used to be tested through the tool. The gate, the credential store, and the
transports moved to the executor, so the properties moved with them. The ordering assertions
matter most: a refused call must not decrypt a credential and must leave no job record.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest

from nanoinfra.config.gates import GatesConfig
from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.executor.protocol import ExecuteRequest
from nanoinfra.gates.executor.server import Executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import ExecutionResult
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.store import ServerStore

_BACKEND = "nanoinfra.servers.execution.ssh_backend.SSHBackend.run"
_GRANTED_COMMAND = "systemctl reload nginx"


@pytest.fixture(autouse=True)
def _configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())


def _server(tmp_path: Path, *, secret_ref: str | None = None) -> None:
    raw: dict[str, Any] = {
        "name": "prod-web-01",
        "providerId": "ssh",
        "config": {"host": "10.0.1.5"},
    }
    if secret_ref:
        raw["secretRef"] = secret_ref
    ServerStore(tmp_path).create(raw)


def _request(**over: object) -> ExecuteRequest:
    fields: dict[str, Any] = {
        "server_id_or_name": "prod-web-01",
        "command": "uptime",
        "session_id": "s1",
        "execution_context": "automation",
        "preview_requested": False,
        "timeout_s": None,
        "token_nonce": None,
    }
    fields.update(over)
    return ExecuteRequest(**fields)


def _executor(tmp_path: Path, gates: GatesConfig | None = None, *, audit: Any = None) -> Executor:
    return Executor(
        workspace=tmp_path, gates_loader=lambda: gates or GatesConfig(), audit=audit
    )


def _granted_with_credential() -> GatesConfig:
    """A grant plus the credential.access allow the decryption needs.

    The grant alone covers mutate.remote, and credential.access refuses under the unattended
    defaults -- which is correct, and which means a test about the decryption has to get past it.
    """
    gates = _granted()
    gates.unattended.credential_access = "allow"  # type: ignore[assignment]
    return gates


def _granted() -> GatesConfig:
    return GatesConfig.model_validate(
        {
            "unattended": {"mutate.remote": {"host": "grant"}},
            "standingGrants": [
                {
                    "id": "reload",
                    "contexts": ["unattended"],
                    "hosts": ["10.0.1.5"],
                    "commands": [_GRANTED_COMMAND],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_an_unattended_call_without_a_grant_is_refused(tmp_path: Path) -> None:
    _server(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request())

    assert not response.ok
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_call_never_decrypts_a_credential(tmp_path: Path) -> None:
    """The gate runs before resolve_plaintext. A denial must not touch the secret store."""
    secret = SecretStore(tmp_path).create(
        {"name": "web-key", "kind": "password", "providerId": "local", "value": "s3cr3t"}
    )
    _server(tmp_path, secret_ref=secret.id)

    with (
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
        patch(_BACKEND, new=AsyncMock()),
    ):
        await _executor(tmp_path).handle(_request())

    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_a_refused_call_leaves_no_job_record(tmp_path: Path) -> None:
    """A job record for an action that never ran would misreport history."""
    _server(tmp_path)

    with patch(_BACKEND, new=AsyncMock()):
        await _executor(tmp_path).handle(_request())

    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_the_refusal_names_the_class_and_the_missing_grant(tmp_path: Path) -> None:
    """An operator debugging a broken automation at 03:00 must learn which grant to write."""
    _server(tmp_path)

    response = await _executor(tmp_path).handle(_request())

    assert "mutate.remote" in response.reason
    assert "grant" in response.reason


@pytest.mark.asyncio
async def test_a_matching_grant_lets_an_unattended_call_run(tmp_path: Path) -> None:
    _server(tmp_path)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)) as run:
        response = await _executor(tmp_path, _granted()).handle(
            _request(command=_GRANTED_COMMAND)
        )

    assert response.ok
    run.assert_called_once()


@pytest.mark.asyncio
async def test_an_interactive_call_now_reaches_policy(tmp_path: Path) -> None:
    """#38 removed the interactive short-circuit that #8 and #10 both named.

    The executor used to allow every interactive turn, so an `approve` decision executed. The
    default interactive policy for a host is `approve`, so this call must now suspend or refuse.
    It refuses here, because this executor carries no approval store.
    """
    _server(tmp_path)
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)) as run:
        response = await _executor(tmp_path).handle(_request(execution_context="interactive"))

    assert not response.ok
    assert "approval" in response.reason
    run.assert_not_called()


@pytest.mark.asyncio
async def test_an_interactive_call_the_policy_allows_still_runs(tmp_path: Path) -> None:
    """The other half of the same rule. An operator who declares `allow` gets no prompt."""
    _server(tmp_path)
    gates = GatesConfig.model_validate({"interactive": {"mutate.remote": {"host": "allow"}}})
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)) as run:
        response = await _executor(tmp_path, gates).handle(
            _request(execution_context="interactive")
        )

    assert response.ok
    run.assert_called_once()


@pytest.mark.asyncio
async def test_an_unattended_approve_policy_refuses_instead_of_waiting(tmp_path: Path) -> None:
    """#8's rule survives #38. Nobody waits on an automation, so a prompt there is a hang.

    An operator who writes `approve` for an unattended context reads which key to change.
    """
    _server(tmp_path)
    gates = GatesConfig.model_validate({"unattended": {"mutate.remote": {"host": "approve"}}})

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, gates).handle(_request())

    assert not response.ok
    assert "unattended" in response.reason
    assert "standing grant" in response.reason
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_preview_is_never_refused_by_the_gate(tmp_path: Path) -> None:
    """A preview connects to nothing, so refusing it would teach nothing and block reading."""
    _server(tmp_path)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path).handle(_request(preview_requested=True))

    assert response.ok
    assert "Preview" in response.output
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_preview_asks_the_policy_and_still_needs_no_permission(tmp_path: Path) -> None:
    """#10 held that a preview asked no policy question at all. #179 changed that deliberately.

    A preview now reads the policy, because reading it is how the answer an operator needs -- the
    grant this action would require -- arrives without a refusal having to happen first. Both
    evaluations are pure. What #10 was protecting is asserted here as what it always meant: the
    preview reaches no host and resolves no credential, so it needs no permission of its own,
    and an empty policy that refuses every real action still returns a preview.
    """
    _server(tmp_path)
    asked: list[int] = []

    def loader() -> GatesConfig:
        asked.append(1)
        return GatesConfig()

    executor = Executor(workspace=tmp_path, gates_loader=loader)
    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await executor.handle(_request(preview_requested=True))

    assert asked, "the preview has to read the policy to report what a real run would meet"
    run.assert_not_called()
    assert response.ok
    assert "Preview" in response.output
    assert response.preview_outcome == "deny"


@pytest.mark.asyncio
async def test_an_unreadable_policy_refuses_an_unattended_call(tmp_path: Path) -> None:
    """Unparseable policy fails closed. A broken config is not a reason to skip the gate."""
    _server(tmp_path)

    def loader() -> GatesConfig:
        from nanoinfra.gates.policy import load_policy

        return load_policy()

    with (
        patch("nanoinfra.config.loader.load_config", side_effect=RuntimeError("bad config")),
        patch(_BACKEND, new=AsyncMock()) as run,
    ):
        response = await Executor(workspace=tmp_path, gates_loader=loader).handle(_request())

    assert not response.ok
    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_denial_is_recorded(tmp_path: Path) -> None:
    """The executor decides, so the executor records (#16)."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")

    with patch(_BACKEND, new=AsyncMock()):
        await _executor(tmp_path, audit=audit).handle(_request())

    assert [r["decision"] for r in audit.read_all()] == ["denied"]


@pytest.mark.asyncio
async def test_an_allowed_action_is_recorded_too(tmp_path: Path) -> None:
    """A record only of denials would leave a reviewer unable to see what ran."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")
    fake = ExecutionResult(exit_code=0, output="ok", error=None)

    with patch(_BACKEND, new=AsyncMock(return_value=fake)):
        await _executor(tmp_path, _granted(), audit=audit).handle(
            _request(command=_GRANTED_COMMAND)
        )

    # The completion record #46 appends when the action ends.
    assert [r["decision"] for r in audit.read_all()] == ["allow", "completion"]


@pytest.mark.asyncio
async def test_an_audit_write_failure_refuses_the_action(tmp_path: Path) -> None:
    """An action that nothing recorded must not run. #16 raises so the gate can fail closed."""
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")

    with (
        patch.object(audit, "record", side_effect=OSError("disk full")),
        patch(_BACKEND, new=AsyncMock()) as run,
        patch("nanoinfra.secrets.store.SecretStore.resolve_plaintext", new=Mock()) as resolve,
    ):
        response = await _executor(tmp_path, _granted(), audit=audit).handle(
            _request(command=_GRANTED_COMMAND)
        )

    assert not response.ok
    assert response.error
    run.assert_not_called()
    resolve.assert_not_called()


@pytest.mark.asyncio
async def test_the_command_text_never_reaches_the_audit_record(tmp_path: Path) -> None:
    _server(tmp_path)
    audit = AuditStore(tmp_path / "gates")

    with patch(_BACKEND, new=AsyncMock()):
        await _executor(tmp_path, audit=audit).handle(
            _request(command="mysql -u root -p'hunter2'")
        )

    assert "hunter2" not in str(audit.read_all())


@pytest.mark.asyncio
async def test_an_unset_key_says_so_instead_of_blaming_a_deleted_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Found the hard way: the operator had approved the action and got a message about a secret
    that "no longer exists", while the secret was untouched and the key was simply not in that
    process's environment. Two different facts with two different fixes, so they read differently.
    """
    secret = SecretStore(tmp_path).create(
        {"name": "prod-key", "kind": "token", "providerId": "local", "value": "k"}
    )
    _server(tmp_path, secret_ref=secret.id)
    # The gateway was started without it. The secret file is still on disk.
    monkeypatch.delenv("NANOINFRA_SECRETS_KEY", raising=False)

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, _granted_with_credential()).handle(
            _request(command=_GRANTED_COMMAND)
        )

    assert not response.ok
    text = str(response.error or response.output)
    assert "NANOINFRA_SECRETS_KEY" in text
    assert "no longer exists" not in text
    # And it says the secret is fine, so nobody goes looking through the store for it.
    assert "untouched" in text
    # No transport was opened and no job was recorded, because nothing ran.
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_key_that_does_not_match_the_secret_says_which_of_the_three_it_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The third fact, and the only one that used to be silent.

    Found in the field: a rotated `NANOINFRA_SECRETS_KEY` left one server's credential encrypted
    under the old key while newer secrets used the new one. The key was set and valid, the secret
    was on disk and intact, and they simply did not match — so `InvalidToken` escaped this
    handler, the approved action ended with **no output at all**, and the only trace was a
    `WARNING` on the scrub path. The operator's agent was left guessing at SSH.
    """
    from cryptography.fernet import Fernet

    secret = SecretStore(tmp_path).create(
        {"name": "prod-key", "kind": "token", "providerId": "local", "value": "k"}
    )
    _server(tmp_path, secret_ref=secret.id)
    # A different, perfectly valid key: what a rotation leaves behind.
    monkeypatch.setenv("NANOINFRA_SECRETS_KEY", Fernet.generate_key().decode())

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, _granted_with_credential()).handle(
            _request(command=_GRANTED_COMMAND)
        )

    assert not response.ok
    text = str(response.error or response.output)
    # It names the actual state: both halves are fine and they do not match.
    assert "cannot be decrypted" in text
    assert "intact" in text
    assert "NANOINFRA_SECRETS_KEY" in text
    # And it is none of the other two stories.
    assert "no longer exists" not in text
    assert "is not set" not in text
    # Nothing ran, and nothing was recorded as having run.
    run.assert_not_called()
    assert JobStore(tmp_path).list_jobs() == []


@pytest.mark.asyncio
async def test_a_genuinely_missing_secret_still_says_that(tmp_path: Path) -> None:
    """The other half of the distinction: this one really is gone."""
    _server(tmp_path, secret_ref="deadbeefdeadbeefdeadbeefdeadbeef")

    with patch(_BACKEND, new=AsyncMock()) as run:
        response = await _executor(tmp_path, _granted_with_credential()).handle(
            _request(command=_GRANTED_COMMAND)
        )

    assert not response.ok
    text = str(response.error or response.output)
    assert "no longer exists" in text
    assert "NANOINFRA_SECRETS_KEY" not in text
    run.assert_not_called()
