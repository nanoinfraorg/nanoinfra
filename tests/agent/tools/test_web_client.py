# tests/agent/tools/test_web_client.py
"""Item 16 (#19): web_fetch and web_search as thin clients of the fetcher.

The implementation lives in the fetcher process. The tool writes one frame and renders one reply.
So the tests here cover the seam, and not a fetch: what reaches the wire, and what the model reads
back.

Two properties carry the security value of the split:

- The tool imports no HTTP client and no provider code. A tool that cannot import one cannot send
  a request, and that is checkable rather than merely intended.
- An unreachable fetcher produces a deployment fault, and never an in-process fetch. A fallback
  would put the egress back in the agent and make the whole split false.

The alias parameters are the third property. The model calls this tool with camelCase names, and
the fetcher's request fields are snake_case. Nothing else translates between the two.
"""

from __future__ import annotations

import ast
import socket
import threading
import time
from pathlib import Path
from typing import Any

from nanoinfra.agent.tools.base import ToolResult
from nanoinfra.agent.tools.web import FETCHER_SOCKET_ENV, WebFetchTool, WebSearchTool
from nanoinfra.gates.fetcher.client import FetcherClient
from nanoinfra.gates.fetcher.protocol import (
    FetchRequest,
    FetchResponse,
    SearchRequest,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)

_TOOL = Path("nanoinfra/agent/tools/web.py")

# What the tool must not be able to reach. A module that imports any of these can send a request
# of its own, so it could answer a call without the fetcher.
_FORBIDDEN_IMPORTS = (
    "httpx",
    "requests",
    "aiohttp",
    "urllib.request",
    "ddgs",
    "duckduckgo_search",
    "olostep",
    "readability",
    "nanoinfra.security.network",
    "nanoinfra.gates.fetcher.egress",
    "nanoinfra.gates.fetcher.fetch",
    "nanoinfra.gates.fetcher.search",
    "nanoinfra.gates.fetcher.server",
)


def _imported_modules(path: Path) -> set[str]:
    """Every module name the file imports, at any depth, including inside a function."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _reply(**over: Any) -> FetchResponse:
    fields: dict[str, Any] = {
        "ok": True,
        "body": "the page text",
        "blocks": None,
        "is_error": False,
        "error": None,
    }
    fields.update(over)
    return FetchResponse(**fields)


def _serve_once(
    socket_path: Path, reply: FetchResponse, received: list[Any]
) -> threading.Thread:
    """Answer one request with *reply*, and record the request the tool sent."""

    def serve() -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(socket_path))
            server.listen(1)
            conn, _ = server.accept()
            with conn:
                received.append(decode_request(read_frame(conn)))
                write_frame(conn, encode_response(reply))

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    _wait_for(socket_path)
    return thread


def _wait_for(path: Path, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError(f"{path} never appeared")


# ------------------------------------------------------------- the request reaches the socket


async def test_the_fetch_tool_sends_the_url_over_the_socket(tmp_path: Path) -> None:
    """One call, one frame. The URL travels in the frame and the body comes back."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    thread = _serve_once(socket_path, _reply(body="the page text"), received)

    tool = WebFetchTool(client=FetcherClient(socket_path))
    result = await tool.execute(url="https://example.com/page")
    thread.join(timeout=10)

    assert isinstance(received[0], FetchRequest)
    assert received[0].url == "https://example.com/page"
    assert result == "the page text"


async def test_the_search_tool_sends_the_query_over_the_socket(tmp_path: Path) -> None:
    """The tool names no provider. The fetcher owns the provider choice and its key."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    thread = _serve_once(socket_path, _reply(body="Results for: nanoinfra"), received)

    tool = WebSearchTool(client=FetcherClient(socket_path))
    result = await tool.execute(query="nanoinfra", count=3)
    thread.join(timeout=10)

    assert isinstance(received[0], SearchRequest)
    assert received[0].query == "nanoinfra"
    assert received[0].count == 3
    assert result == "Results for: nanoinfra"


# ------------------------------------------------------------------------- failures the model reads


async def test_a_failed_fetch_comes_back_as_a_tool_error(tmp_path: Path) -> None:
    """``is_error`` marks a result the model must read as a failure, such as a rate limit."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    thread = _serve_once(
        socket_path, _reply(body="Error: Brave search rate limited", is_error=True), received
    )

    tool = WebSearchTool(client=FetcherClient(socket_path))
    result = await tool.execute(query="nanoinfra")
    thread.join(timeout=10)

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "rate limited" in result


async def test_a_refused_frame_comes_back_as_a_tool_error(tmp_path: Path) -> None:
    """``ok`` False means the fetcher could not act on the frame. The reason reaches the model."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    reply = _reply(ok=False, body="", is_error=True, error="Malformed request: no version")
    thread = _serve_once(socket_path, reply, received)

    tool = WebFetchTool(client=FetcherClient(socket_path))
    result = await tool.execute(url="https://example.com/page")
    thread.join(timeout=10)

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "Malformed request" in result


async def test_an_unreachable_fetcher_names_a_deployment_fault(tmp_path: Path) -> None:
    """"The fetcher is not running" and "the page failed" must not read the same.

    The first sends an operator to the deployment. The second sends the model to another URL.
    """
    tool = WebFetchTool(client=FetcherClient(tmp_path / "absent.sock"))

    result = await tool.execute(url="https://example.com/page")

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "deployment fault" in result
    assert "absent.sock" in result


async def test_an_unreachable_fetcher_gets_no_fetch_of_its_own(tmp_path: Path) -> None:
    """No fallback. A fallback would put the egress back in the agent.

    The tool holds no reader and no provider, so there is nothing to fall back to. This states
    the absence, because a later edit that adds one has to delete this test to pass.
    """
    import nanoinfra.agent.tools.web as tool_module

    tool = WebSearchTool(client=FetcherClient(tmp_path / "absent.sock"))
    result = await tool.execute(query="nanoinfra")

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    for attribute in ("WebFetch", "WebSearch", "httpx", "build_image_content_blocks"):
        assert not hasattr(tool_module, attribute), attribute
    for attribute in ("_search_duckduckgo", "_search_brave", "_fetch_jina", "_fetch_readability"):
        assert not hasattr(tool, attribute), attribute


# ------------------------------------------------------------------------ the alias parameters


async def test_the_fetch_aliases_reach_the_request(tmp_path: Path) -> None:
    """The model writes ``extractMode`` and ``maxChars``. The wire fields are snake_case."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    thread = _serve_once(socket_path, _reply(), received)

    tool = WebFetchTool(client=FetcherClient(socket_path))
    await tool.execute(url="https://example.com/page", extractMode="text", maxChars=1200)
    thread.join(timeout=10)

    request = received[0]
    assert isinstance(request, FetchRequest)
    assert request.extract_mode == "text"
    assert request.max_chars == 1200


async def test_the_search_aliases_reach_the_request(tmp_path: Path) -> None:
    """Three filters arrive under camelCase names, and the fetcher reads snake_case ones."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    thread = _serve_once(socket_path, _reply(), received)

    tool = WebSearchTool(client=FetcherClient(socket_path))
    await tool.execute(
        query="nanoinfra", timeRange="OneWeek", authLevel=1, queryRewrite=True
    )
    thread.join(timeout=10)

    request = received[0]
    assert isinstance(request, SearchRequest)
    assert request.time_range == "OneWeek"
    assert request.auth_level == 1
    assert request.query_rewrite is True


async def test_the_snake_case_names_still_reach_the_request(tmp_path: Path) -> None:
    """A caller that writes the snake_case name gets the same request."""
    socket_path = tmp_path / "fetcher.sock"
    received: list[Any] = []
    thread = _serve_once(socket_path, _reply(), received)

    tool = WebSearchTool(client=FetcherClient(socket_path))
    await tool.execute(query="nanoinfra", time_range="OneDay", auth_level=0, query_rewrite=False)
    thread.join(timeout=10)

    request = received[0]
    assert isinstance(request, SearchRequest)
    assert request.time_range == "OneDay"
    assert request.auth_level == 0
    assert request.query_rewrite is False


# ------------------------------------------------------------------------------- image blocks


async def test_an_image_reply_comes_back_as_content_blocks(tmp_path: Path) -> None:
    """A fetched image crosses the wire as blocks, and the tool returns them unchanged.

    The model needs the native image block. A JSON string of the same data would reach it as text.
    """
    socket_path = tmp_path / "fetcher.sock"
    blocks: list[dict[str, Any]] = [
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,QUJD"},
            "_meta": {"path": "https://example.com/cat.png"},
        },
        {"type": "text", "text": "(Image fetched from: https://example.com/cat.png)"},
    ]
    received: list[Any] = []
    thread = _serve_once(socket_path, _reply(body="", blocks=blocks), received)

    tool = WebFetchTool(client=FetcherClient(socket_path))
    result = await tool.execute(url="https://example.com/cat.png")
    thread.join(timeout=10)

    assert result == blocks


# ---------------------------------------------------------------------------- structural checks


def test_the_tool_imports_no_http_client_and_no_provider() -> None:
    """The acceptance criterion of the cutover, as a check rather than a promise.

    A lazy import inside a function would satisfy a naive grep, so this walks the whole tree.
    """
    imported = _imported_modules(_TOOL)

    assert [name for name in _FORBIDDEN_IMPORTS if name in imported] == []


def test_the_deployment_names_the_socket() -> None:
    """A deployment that starts the fetcher elsewhere tells the tool where to look.

    The environment variable is how the container hands the path over. Without it the tool would
    guess, and a guess reads to an operator as a fetcher that is not running.
    """
    import os

    from nanoinfra.agent.tools.web import default_socket_path

    previous = os.environ.get(FETCHER_SOCKET_ENV)
    os.environ[FETCHER_SOCKET_ENV] = "/run/nanoinfra-fetch/fetcher.sock"
    try:
        assert default_socket_path() == Path("/run/nanoinfra-fetch/fetcher.sock")
    finally:
        if previous is None:
            del os.environ[FETCHER_SOCKET_ENV]
        else:
            os.environ[FETCHER_SOCKET_ENV] = previous
