# tests/gates/test_fetcher_server.py
"""Item 16 (#19): the fetcher process, its socket, and how it fails.

The fetcher is the process with broad egress. Untrusted content arrives in it, so its availability
is part of what the split has to keep: a peer that speaks nonsense must not take it down, and a
page that fails must come back as a result rather than a crash.

The socket tests never reach the network. A refused URL and an unknown provider both answer before
any client opens a connection, and that is enough to exercise the wire end to end.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

from nanoinfra.gates.fetcher import server as server_module
from nanoinfra.gates.fetcher.protocol import (
    PROTOCOL_VERSION,
    FetchRequest,
    SearchRequest,
    decode_response,
    encode_request,
    read_frame,
    write_frame,
)
from nanoinfra.gates.fetcher.server import Fetcher, WebSettings, serve_forever


@pytest.fixture(autouse=True)
def _no_app_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the operator's real config file.

    ``serve_forever`` builds its own Fetcher, so the settings it uses come from this module level
    function. A test that read the real config would depend on the machine it runs on.
    """
    monkeypatch.setattr(
        server_module, "load_web_settings", lambda: WebSettings(provider="no-such-provider")
    )


def _fetcher(**settings: object) -> Fetcher:
    resolved = WebSettings(**settings)  # pyright: ignore[reportArgumentType]
    return Fetcher(settings_loader=lambda: resolved)


@pytest.mark.asyncio
async def test_the_fetcher_answers_a_refused_url_without_any_egress() -> None:
    """A URL check that fails must cost no request. The refusal is the whole answer."""
    response = await _fetcher().handle(
        FetchRequest(url="ftp://example.com/file", extract_mode="markdown", max_chars=None)
    )

    assert response.ok
    document = json.loads(response.body)
    assert "URL validation failed" in document["error"]


@pytest.mark.asyncio
async def test_the_fetcher_answers_an_unknown_search_provider() -> None:
    response = await _fetcher(provider="not-a-provider").handle(
        SearchRequest(
            query="nanoinfra",
            count=None,
            time_range=None,
            auth_level=None,
            query_rewrite=None,
            freshness=None,
        )
    )

    assert response.ok
    assert response.is_error
    assert "unknown search provider" in response.body


@pytest.mark.asyncio
async def test_the_settings_reload_on_every_request() -> None:
    """An operator changes the provider in the WebUI, and the next request must use it.

    The tool used to hold this refresh, because the tool held the search code. The code moved to a
    long-lived process, so the reload moved with it. A fetcher that cached its settings at startup
    would answer with the provider it was born with until someone restarted it.
    """
    providers = ["first-provider", "second-provider"]
    fetcher = Fetcher(settings_loader=lambda: WebSettings(provider=providers.pop(0)))
    request = SearchRequest(
        query="nanoinfra",
        count=None,
        time_range=None,
        auth_level=None,
        query_rewrite=None,
        freshness=None,
    )

    first = await fetcher.handle(request)
    second = await fetcher.handle(request)

    assert "first-provider" in first.body
    assert "second-provider" in second.body


@pytest.mark.asyncio
async def test_a_failed_settings_reload_keeps_the_last_settings() -> None:
    """A broken config file must not silently drop the operator's choice.

    Serving defaults after a failed reload would send traffic to another provider, and through no
    proxy, without a word to anybody.
    """
    calls = {"n": 0}

    def loader() -> WebSettings:
        calls["n"] += 1
        if calls["n"] == 1:
            return WebSettings(provider="configured-provider")
        raise OSError("config file is gone")

    fetcher = Fetcher(settings_loader=loader)
    request = SearchRequest(
        query="nanoinfra",
        count=None,
        time_range=None,
        auth_level=None,
        query_rewrite=None,
        freshness=None,
    )

    await fetcher.handle(request)
    second = await fetcher.handle(request)

    assert "configured-provider" in second.body


def test_the_socket_serves_a_request_end_to_end(tmp_path: Path) -> None:
    """One real socket round trip, because the wire is the boundary under test."""
    socket_path = tmp_path / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(
            client,
            encode_request(
                FetchRequest(url="ftp://example.com/f", extract_mode="markdown", max_chars=None)
            ),
        )
        response = decode_response(read_frame(client))
    thread.join(timeout=10)

    assert response.ok
    assert "URL validation failed" in json.loads(response.body)["error"]


def test_the_socket_serves_one_request_per_connection(tmp_path: Path) -> None:
    """Two requests mean two connections, and the fetcher answers them one after the other.

    ``ddgs`` is not safe to call concurrently. The old tool asked the agent's runner to serialize
    web_search for that reason. The serialization now comes from the process, so the property holds
    for any caller rather than only for the runner that knew about it.
    """
    socket_path = tmp_path / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=2)
    answers: list[bool] = []

    for _ in range(2):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(socket_path))
            write_frame(
                client,
                encode_request(
                    FetchRequest(url="ftp://example.com/f", extract_mode="text", max_chars=None)
                ),
            )
            answers.append(decode_response(read_frame(client)).ok)
    thread.join(timeout=10)

    assert answers == [True, True]


def test_a_malformed_frame_gets_a_refusal_and_not_a_crash(tmp_path: Path) -> None:
    """A peer that speaks nonsense must not take the fetcher down."""
    socket_path = tmp_path / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(client, b"not a request")
        response = decode_response(read_frame(client))
    thread.join(timeout=10)

    assert not response.ok
    assert response.error
    assert response.is_error


def test_a_frame_with_an_unknown_operation_gets_a_refusal(tmp_path: Path) -> None:
    """Fail closed on an operation this side does not implement."""
    socket_path = tmp_path / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)

    payload = json.dumps({"v": PROTOCOL_VERSION, "op": "exec", "command": "id"}).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(client, payload)
        response = decode_response(read_frame(client))
    thread.join(timeout=10)

    assert not response.ok
    assert "unknown operation" in str(response.error)


def test_a_reply_above_the_wire_limit_gets_an_answer_and_not_a_hang_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A page too large for the wire must not read as a fetcher that is not running.

    A frame the writer refuses would close the connection with nothing on it, and the client turns
    an empty close into "unavailable". An operator would then look for a dead process rather than
    a large page.
    """
    monkeypatch.setattr(server_module, "MAX_FRAME_BYTES", 32)
    socket_path = tmp_path / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(
            client,
            encode_request(
                FetchRequest(url="ftp://example.com/f", extract_mode="markdown", max_chars=None)
            ),
        )
        response = decode_response(read_frame(client))
    thread.join(timeout=10)

    assert not response.ok
    assert "above the" in str(response.error)


def test_the_socket_is_removed_on_exit(tmp_path: Path) -> None:
    """A stale socket file blocks the next bind, so the server cleans up after itself."""
    socket_path = tmp_path / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)
    _round_trip(socket_path)
    thread.join(timeout=10)

    assert not socket_path.exists()


def test_the_socket_directory_excludes_other_users(tmp_path: Path) -> None:
    """A socket's own mode is not honoured everywhere, so the directory carries the control."""
    socket_dir = tmp_path / "run"
    socket_path = socket_dir / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)
    try:
        assert socket_dir.stat().st_mode & 0o077 == 0
    finally:
        _round_trip(socket_path)
        thread.join(timeout=10)


def test_an_existing_socket_directory_keeps_the_mode_the_deployment_set(tmp_path: Path) -> None:
    """A two-uid deployment cannot use 0700, and the fetcher must not clobber its choice.

    With separate accounts the socket directory is owned by the fetcher and carries setgid plus
    group traversal (2710), so the agent account can reach a known socket name without listing the
    directory. A blanket chmod to 0700 here would lock the agent out, and a split the agent cannot
    talk to is worse than the mode it replaced.

    So the fetcher sets a private mode only on a directory it creates itself.
    """
    socket_dir = tmp_path / "run"
    socket_dir.mkdir()
    socket_dir.chmod(0o2710)
    before = socket_dir.stat().st_mode & 0o7777
    socket_path = socket_dir / "fetch.sock"
    thread = _serve(socket_path, tmp_path, max_requests=1)
    try:
        assert socket_dir.stat().st_mode & 0o7777 == before
    finally:
        _round_trip(socket_path)
        thread.join(timeout=10)


def _serve(socket_path: Path, workspace: Path, *, max_requests: int) -> threading.Thread:
    thread = threading.Thread(
        target=serve_forever,
        kwargs={
            "socket_path": socket_path,
            "workspace": workspace,
            "max_requests": max_requests,
        },
        daemon=True,
    )
    thread.start()
    _wait_for(socket_path)
    return thread


def _round_trip(socket_path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(socket_path))
        write_frame(
            client,
            encode_request(
                FetchRequest(url="ftp://example.com/f", extract_mode="markdown", max_chars=None)
            ),
        )
        read_frame(client)


def _wait_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared")
