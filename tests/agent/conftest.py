"""Shared fixtures and helpers for agent tests."""

from __future__ import annotations

import contextlib
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanoinfra.agent.loop import AgentLoop
from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.bus.queue import MessageBus
from nanoinfra.gates.executor.protocol import read_frame, write_frame
from nanoinfra.gates.executor.scrub import answer_scrub
from nanoinfra.gates.executor.scrub_protocol import (
    decode_scrub_request,
    default_scrub_socket_path,
    encode_scrub_response,
)
from nanoinfra.providers.base import LLMProvider


@pytest.fixture(autouse=True)
def _executor_socket_under_tmp_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep every executor socket this suite names inside tmp_path.

    The redaction path asks the executor to scrub a transcript (#41), and the client resolves
    its socket from this variable. Without the variable a test would reach the executor of the
    workstation it runs on.
    """
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(tmp_path / "run" / "executor.sock"))


class ScrubService:
    """A scrub socket for one workspace, for a test that asserts a transcript scrubs.

    The executor performs the scrub after #41, so a boundary test needs a scrubber. This runs
    the real answer path of ``nanoinfra/gates/executor/scrub.py`` in a thread of the test
    process, and it stops before the test ends.

    The accept deadline is short on purpose. A close from another thread does not always wake a
    blocked accept, and a leaked thread would outlive the test.
    """

    def __init__(self, workspace: Path, socket_path: Path) -> None:
        self.socket_path = socket_path
        self.requests = 0
        self._workspace = workspace
        self._stop = threading.Event()
        socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.settimeout(0.05)
        self._listener.bind(str(socket_path))
        self._listener.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.requests += 1
            with conn:
                with contextlib.suppress(OSError, ValueError):
                    request = decode_scrub_request(read_frame(conn))
                    answer = answer_scrub(request, workspace=self._workspace)
                    write_frame(conn, encode_scrub_response(answer))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        self._listener.close()
        with contextlib.suppress(OSError):
            self.socket_path.unlink()


@pytest.fixture
def scrub_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Iterator[Callable[[Path | str], ScrubService]]:
    """Return a factory that starts one scrub service for one workspace.

    The factory also names the execute socket, because the client derives the scrub path from
    it. One test starts one service, since one variable names one path.
    """
    services: list[ScrubService] = []

    def _start(workspace: Path | str) -> ScrubService:
        execute_socket = tmp_path / "run" / "e.sock"
        monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(execute_socket))
        service = ScrubService(Path(workspace), default_scrub_socket_path(execute_socket))
        services.append(service)
        return service

    try:
        yield _start
    finally:
        for service in services:
            service.stop()


@pytest.fixture
def cmd_python() -> str:
    """Return the Python command name available to ExecTool tests."""
    return "python" if os.name == "nt" else "python3"


def make_provider(
    default_model: str = "test-model",
    *,
    max_tokens: int = 4096,
    spec: bool = True,
) -> MagicMock:
    """Create a spec-limited LLM provider mock."""
    mock_type = MagicMock(spec=LLMProvider) if spec else MagicMock()
    provider = mock_type
    provider.get_default_model.return_value = default_model
    provider.generation = SimpleNamespace(
        max_tokens=max_tokens,
        temperature=0.1,
        reasoning_effort=None,
    )
    provider.estimate_prompt_tokens.return_value = (10_000, "test")
    return provider


def make_loop(
    tmp_path: Path,
    *,
    model: str = "test-model",
    context_window_tokens: int = 128_000,
    session_ttl_minutes: int = 0,
    unified_session: bool = False,
    mcp_servers: dict | None = None,
    tools_config=None,
    model_presets: dict | None = None,
    hooks: list | None = None,
    provider: MagicMock | None = None,
    patch_deps: bool = False,
) -> AgentLoop:
    """Create a real AgentLoop for testing.

    Args:
        patch_deps: If True, patch ContextBuilder/SessionManager/SubagentManager
                    during construction (needed when workspace has no real files).
    """
    bus = MessageBus()
    if provider is None:
        provider = make_provider(default_model=model)

    kwargs = dict(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model=model,
        context_window_tokens=context_window_tokens,
        session_ttl_minutes=session_ttl_minutes,
        unified_session=unified_session,
    )
    if mcp_servers is not None:
        kwargs["mcp_servers"] = mcp_servers
    if tools_config is not None:
        kwargs["tools_config"] = tools_config
    if model_presets is not None:
        kwargs["model_presets"] = model_presets
    if hooks is not None:
        kwargs["hooks"] = hooks

    if patch_deps:
        with patch("nanoinfra.agent.loop.ContextBuilder"), \
             patch("nanoinfra.agent.loop.SessionManager"), \
             patch("nanoinfra.agent.loop.SubagentManager") as mock_sub_mgr:
            mock_sub_mgr.return_value.cancel_by_session = AsyncMock(return_value=0)
            return AgentLoop(**kwargs)
    return AgentLoop(**kwargs)


@pytest.fixture
def loop_factory(tmp_path):
    """Fixture providing a factory for creating AgentLoop instances."""
    def _factory(**kwargs):
        return make_loop(tmp_path, **kwargs)
    return _factory
