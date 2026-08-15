# tests/gates/test_socket_readiness.py
"""Existence is not readiness, and a test must not race on it.

Three tests failed on the Python 3.11 job and passed on 3.13 and on every developer machine:

    tests/agent/tools/test_web_client.py::test_the_search_tool_sends_the_query_over_the_socket
    tests/agent/tools/test_web_client.py::test_an_image_reply_comes_back_as_content_blocks
    tests/gates/test_fetcher_server.py::test_the_socket_serves_one_request_per_connection

    ConnectionRefusedError: [Errno 111] Connection refused

The cause is one line of sequencing, and no part of it is version specific. A server calls
``bind()``, which creates the socket file, and then ``listen()``, which makes the socket accept a
peer. A test that waited for the file to appear and then connected once was dialing inside that
gap. The 3.11 job lost the race, and a faster machine won it.

**So this file removes the timing from the question.** Each test holds the server between ``bind()``
and ``listen()`` for a fixed delay, which makes the gap certain rather than likely. A test that
fails only on one CI job is a test nobody can act on.

Two answers are correct, and the choice depends on who owns the connect:

- The test dials: it retries a refused connect. `connect_to_unix_socket` in the root ``conftest.py``.
- The code under test dials: the server says when it listens, with a ``threading.Event``. A
  retrying client would be wrong there, because other tests assert that an unreachable fetcher
  produces a deployment fault.

A probe connection is the third answer and it is wrong for most of these tests: several count
connections, and several serve a fixed number of requests, so an extra connect changes what they
measure. A retry of a *failed* connect adds nothing to a successful count.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest

# Long enough that no machine wins the race by accident, and short enough to pay per test.
_GAP_S = 0.3


def _server_that_waits_before_listen(
    socket_path: Path, *, gap_s: float = _GAP_S
) -> tuple[threading.Thread, threading.Event]:
    """Bind now, listen after *gap_s*. The event fires when the socket accepts a peer."""
    listening = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            time.sleep(gap_s)
            server.listen(1)
            listening.set()
            conn, _ = server.accept()
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return thread, listening


def _wait_for_the_path(path: Path, timeout_s: float = 5.0) -> None:
    """The wait these tests used to use. It returns while the server is still not listening."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.005)
    raise AssertionError(f"{path} never appeared")


def test_the_path_appears_before_the_socket_accepts_a_peer(tmp_path: Path) -> None:
    """The defect itself, stated as a property rather than as a flake.

    This is the assertion that explains all three CI failures: the file is there and the socket
    refuses. Every wait built on `exists` is racing.
    """
    socket_path = tmp_path / "gap.sock"
    thread, listening = _server_that_waits_before_listen(socket_path)

    _wait_for_the_path(socket_path)

    assert socket_path.exists()
    assert not listening.is_set()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        with pytest.raises(ConnectionRefusedError):
            client.connect(str(socket_path))

    assert listening.wait(5.0)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
    thread.join(timeout=5)


def test_the_retrying_connect_crosses_the_gap(
    tmp_path: Path, connect_to_unix_socket: Callable[..., socket.socket]
) -> None:
    """The answer for a test that dials the socket itself."""
    socket_path = tmp_path / "gap.sock"
    thread, _ = _server_that_waits_before_listen(socket_path)

    _wait_for_the_path(socket_path)
    client = connect_to_unix_socket(socket_path)

    try:
        assert client.fileno() != -1
    finally:
        client.close()
    thread.join(timeout=5)


def test_the_retrying_connect_gives_up_and_says_which_socket(
    tmp_path: Path, connect_to_unix_socket: Callable[..., socket.socket]
) -> None:
    """A retry must not become an unbounded wait, and the failure must name the path.

    A bare `ConnectionRefusedError` after a ten second retry says nothing about which of the four
    sockets a deployment holds was the one that never answered.
    """
    absent = tmp_path / "nobody-listens.sock"

    with pytest.raises(AssertionError, match="never accepted a connection"):
        connect_to_unix_socket(absent, 0.2)


def test_the_retrying_connect_opens_one_connection_for_one_call(
    tmp_path: Path, connect_to_unix_socket: Callable[..., socket.socket]
) -> None:
    """A retry of a refused connect must not count as a connection.

    This is the reason a probe connection was the wrong fix: several tests in `tests/gates` count
    connections, and several serve a fixed number of requests.

    The helper arrives as a fixture and never as an import. ``tests`` holds no ``__init__.py``, so
    several directories each carry a module called ``conftest`` and an import of one picks
    whichever reached ``sys.modules`` first.
    """
    socket_path = tmp_path / "counted.sock"
    accepted: list[int] = []
    listening = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            time.sleep(_GAP_S)
            server.listen(2)
            listening.set()
            conn, _ = server.accept()
            accepted.append(1)
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_the_path(socket_path)

    client = connect_to_unix_socket(socket_path)
    client.close()
    thread.join(timeout=5)

    assert accepted == [1], "a retry added a connection the server counted"


def test_an_event_after_listen_never_returns_early(tmp_path: Path) -> None:
    """The answer for a test whose code under test owns the connect.

    ``tests/agent/tools/test_web_client.py`` uses this shape: the tool holds the client, so the
    test cannot retry on its behalf, and the server has to say when it listens.
    """
    socket_path = tmp_path / "event.sock"
    thread, listening = _server_that_waits_before_listen(socket_path)

    assert listening.wait(5.0)

    # The event fires after `listen()`, so a connect right after it never sees the gap.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
    thread.join(timeout=5)
