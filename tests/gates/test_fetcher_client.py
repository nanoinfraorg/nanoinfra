# tests/gates/test_fetcher_client.py
"""Item 16 (#19): the agent side of the fetcher wire.

The agent writes one frame and reads one frame. It holds no fetch code, no provider key, and no
transport, and ``tests/gates/test_fetcher_isolation.py`` asserts that structurally.

The property this file adds is the one an operator feels: "the fetcher is not running" and "the page
failed" must not read the same. The first is a deployment fault and the second is a result.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

import pytest

from nanoinfra.gates.fetcher.client import FetcherClient, FetcherUnavailableError
from nanoinfra.gates.fetcher.protocol import (
    FetchRequest,
    FetchResponse,
    SearchRequest,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)


def _reply(**over: object) -> FetchResponse:
    fields: dict[str, object] = {
        "ok": True,
        "body": "Results for: nanoinfra",
        "blocks": None,
        "is_error": False,
        "error": None,
    }
    fields.update(over)
    return FetchResponse(**fields)  # pyright: ignore[reportArgumentType]


def _serve_once(
    socket_path: Path, reply: FetchResponse, received: list[object]
) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            with conn:
                received.append(decode_request(read_frame(conn)))
                write_frame(conn, encode_response(reply))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)
    return thread


def test_the_client_carries_a_fetch_and_returns_the_reply(tmp_path: Path) -> None:
    socket_path = tmp_path / "fetch.sock"
    received: list[object] = []
    thread = _serve_once(socket_path, _reply(body='{"text": "hello"}'), received)

    response = FetcherClient(socket_path).fetch(url="https://example.com/page", max_chars=1000)
    thread.join(timeout=10)

    assert isinstance(received[0], FetchRequest)
    assert received[0].url == "https://example.com/page"
    assert received[0].max_chars == 1000
    assert response.body == '{"text": "hello"}'


def test_the_client_carries_a_search_and_returns_the_reply(tmp_path: Path) -> None:
    socket_path = tmp_path / "fetch.sock"
    received: list[object] = []
    thread = _serve_once(socket_path, _reply(), received)

    response = FetcherClient(socket_path).search(query="nanoinfra", count=3, time_range="OneWeek")
    thread.join(timeout=10)

    assert isinstance(received[0], SearchRequest)
    assert received[0].query == "nanoinfra"
    assert received[0].count == 3
    assert received[0].time_range == "OneWeek"
    assert "Results for: nanoinfra" in response.body


def test_a_tool_level_failure_comes_back_as_a_response(tmp_path: Path) -> None:
    """A rate-limited provider is a result. Only an unreachable fetcher is an exception."""
    socket_path = tmp_path / "fetch.sock"
    received: list[object] = []
    thread = _serve_once(
        socket_path, _reply(is_error=True, body="Error: Brave search rate limited"), received
    )

    response = FetcherClient(socket_path).search(query="nanoinfra")
    thread.join(timeout=10)

    assert response.ok
    assert response.is_error
    assert "rate limited" in response.body


def test_a_missing_socket_raises_fetcher_unavailable(tmp_path: Path) -> None:
    """A caller must be able to tell "the fetcher is not there" from "the page failed".

    Those two need different words for an operator, and conflating them would read as a broken web
    page when it is a broken deployment.
    """
    with pytest.raises(FetcherUnavailableError):
        FetcherClient(tmp_path / "absent.sock").fetch(url="https://example.com/page")


def test_a_socket_that_dies_mid_reply_raises_fetcher_unavailable(tmp_path: Path) -> None:
    socket_path = tmp_path / "fetch.sock"

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            ready.set()
            conn, _ = server.accept()
            conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    with pytest.raises(FetcherUnavailableError):
        FetcherClient(socket_path).search(query="nanoinfra")
    thread.join(timeout=10)


def test_one_request_per_connection(tmp_path: Path) -> None:
    """Two calls mean two connections. The fetcher serves one request at a time."""
    socket_path = tmp_path / "fetch.sock"
    connections: list[int] = []

    ready = threading.Event()

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(2)
            ready.set()
            for _ in range(2):
                conn, _ = server.accept()
                with conn:
                    connections.append(1)
                    decode_request(read_frame(conn))
                    write_frame(conn, encode_response(_reply()))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for_listen(ready)

    client = FetcherClient(socket_path)
    client.search(query="one")
    client.search(query="two")
    thread.join(timeout=10)

    assert connections == [1, 1]


def _wait_for_listen(ready: threading.Event, timeout_s: float = 10.0) -> None:
    """Wait until the server thread has called listen().

    The old helper waited for the socket file. `bind()` creates that file and `listen()` accepts a
    peer after it, so a connect between the two fails with ConnectionRefusedError. CI caught that on
    Python 3.14 while every other version passed.

    A probe connection is the wrong fix here. These tests count the connections the client makes,
    and a probe would spend one of them. The server thread says when it is ready instead.
    """
    if not ready.wait(timeout_s):
        raise AssertionError("the server thread never reached listen()")
