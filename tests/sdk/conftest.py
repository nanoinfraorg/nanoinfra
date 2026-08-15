# tests/sdk/conftest.py
"""Shared fixtures for the SDK tests.

The redaction path asks the executor to scrub a transcript (#41). So a test that asserts a
snapshot scrubs needs a scrubber, and every test needs its socket path inside tmp_path. Without
the second rule a test reaches the executor of the workstation it runs on.

The helper below repeats ``tests/agent/conftest.py``. The two suites share no package, and one
small copy reads better than an import path that spans two test directories.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from pathlib import Path
from typing import Callable, Iterator

import pytest

from nanoinfra.agent.tools.server_execution import EXECUTOR_SOCKET_ENV
from nanoinfra.gates.executor.protocol import read_frame, write_frame
from nanoinfra.gates.executor.scrub import answer_scrub
from nanoinfra.gates.executor.scrub_protocol import (
    decode_scrub_request,
    default_scrub_socket_path,
    encode_scrub_response,
)


@pytest.fixture(autouse=True)
def _executor_socket_under_tmp_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Keep every executor socket this suite names inside tmp_path."""
    monkeypatch.setenv(EXECUTOR_SOCKET_ENV, str(tmp_path / "run" / "executor.sock"))


class ScrubService:
    """A scrub socket for one workspace, running the executor's own answer path.

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
    """Return a factory that starts one scrub service for one workspace."""
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
