# tests/gates/test_scrub_process.py
"""Item 39 (#41) end to end, against a real executor process.

An in-process test proves that the code calls the code. Only a real child proves the split:
the sentinels exist in the executor, the agent sends one text over a socket, and the persisted
transcript holds the secret name and never the value.

The last test is the one that matters most. It breaks ``resolve_plaintext`` inside this process
and the scrub still works, so the decryption happened somewhere else.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.agent.memory import MemoryStore
from nanoinfra.agent.subagent_transcript import SubagentTranscriptStore
from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.gates.executor.scrub_protocol import default_scrub_socket_path
from nanoinfra.gates.executor.supervisor import start_executor
from nanoinfra.secrets import crypto
from nanoinfra.secrets.store import SecretStore

SECRET_NAME = "prod-db-password"
SECRET_VALUE = "hunter2-correct-horse-battery"


class _Deployment:
    """One executor child, plus the workspace whose secrets it can read."""

    def __init__(self, *, workspace: Path, handle: Any) -> None:
        self.workspace = workspace
        self.handle = handle


@pytest.fixture(scope="module")
def deployment(tmp_path_factory: pytest.TempPathFactory):
    """Start one real executor for this module, and stop it before the module ends.

    One child serves every test here. A child per test would pay the start cost several times,
    and a leaked child would hold a socket after the run.
    """
    patch = pytest.MonkeyPatch()
    root = tmp_path_factory.mktemp("scrub")
    home = root / "home"
    (home / ".nanoinfra").mkdir(parents=True)
    (home / ".nanoinfra" / "config.json").write_text("{}", encoding="utf-8")
    # The child is a separate process, so HOME is what places its config and its audit root.
    patch.setenv("HOME", str(home))
    patch.setenv("NANOINFRA_SECRETS_KEY", crypto.generate_key_for_setup())
    patch.delenv("NANOINFRA_SECRETS_POSTGRES_DSN", raising=False)

    workspace = root / "ws"
    workspace.mkdir()
    SecretStore(workspace).create(
        {
            "name": SECRET_NAME,
            "kind": "password",
            "providerId": "local",
            "value": SECRET_VALUE,
        }
    )

    execute_socket = root / "r" / "e.sock"
    # The agent derives the scrub socket from this path, the way a deployment names it.
    patch.setenv(EXECUTOR_SOCKET_ENV, str(execute_socket))

    handle = start_executor(socket_path=execute_socket, workspace=workspace, timeout_s=30.0)
    try:
        # The scrub socket binds before the execute socket, so a handle proves both are up. A
        # check here keeps the whole module from passing for the wrong reason.
        assert handle.is_running(), handle.read_log_tail(tail=20)
        assert default_scrub_socket_path(execute_socket).exists(), handle.read_log_tail(tail=20)
        yield _Deployment(workspace=workspace, handle=handle)
    finally:
        handle.stop(timeout_s=10)
        patch.undo()


def _tool_message(content: str) -> dict[str, Any]:
    return {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": "execute_on_server",
        "content": content,
    }


def test_the_child_writes_no_frame_locals_to_its_log() -> None:
    """The child's log must hold no plaintext, and a diagnosed traceback holds several.

    This process holds a resolved command, a decrypted credential, and the text under scrub.
    The supervisor sends its stderr to a file in the run directory, so one unexpected exception
    would write one of those values into that file.

    loguru exposes no handler options to a reader, so the check reads the entry point. That is
    the one place the child configures its own sink.
    """
    import inspect

    from nanoinfra.gates.executor import __main__ as entry_point

    source = inspect.getsource(entry_point)

    assert "diagnose=False" in source
    assert 'if __name__ == "__main__":\n    configure_child_logging()' in source


def test_a_history_entry_persists_scrubbed(deployment: _Deployment) -> None:
    """``memory/history.jsonl`` is a durable transcript, and the executor scrubbed it."""
    store = MemoryStore(deployment.workspace)

    store.append_history(f"[..] TOOL: output {SECRET_VALUE}")

    persisted = (deployment.workspace / "memory" / "history.jsonl").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted


def test_a_subagent_transcript_persists_scrubbed(deployment: _Deployment) -> None:
    store = SubagentTranscriptStore(deployment.workspace)

    store.write("abc12345", [_tool_message(f"connected with {SECRET_VALUE}")])

    persisted = store.path_for("abc12345").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted


def test_ordinary_text_survives_the_round_trip(deployment: _Deployment) -> None:
    """The scrub removes the value and nothing else."""
    store = SubagentTranscriptStore(deployment.workspace)

    store.write("abc12346", [{"role": "user", "content": "restart nginx please"}])

    assert "restart nginx please" in json.dumps(store.read("abc12346"))


def test_the_agent_process_never_decrypts_a_secret(
    deployment: _Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof of the split, as a test rather than a claim.

    ``resolve_plaintext`` raises in this process. The transcript still persists scrubbed, so
    another address space held the sentinels.
    """

    def _refuse(self: SecretStore, secret_id: str) -> str | None:
        raise AssertionError("the agent process decrypted a secret")

    monkeypatch.setattr(SecretStore, "resolve_plaintext", _refuse)
    store = SubagentTranscriptStore(deployment.workspace)

    store.write("abc12347", [_tool_message(f"connected with {SECRET_VALUE}")])

    persisted = store.path_for("abc12347").read_text(encoding="utf-8")
    assert SECRET_VALUE not in persisted
    assert SECRET_NAME in persisted
