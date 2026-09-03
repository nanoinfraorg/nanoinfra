"""The API server says what it served (#215).

The hang that became 1.7.4 was diagnosed with a packet capture and a stand-in HTTP server on
loopback, because there was nothing to read: after a deploy and a dozen requests -- several of them
200s with real answers -- the container's log was 45 lines, all boot. Two causes, both fixed here:
the handler's own line sat after parsing, so a refusal never reached it, and `serve` called
`logger.disable("nanoinfra")`, which silenced the whole package including its exception handlers.

What must never appear is as pinned as what must: a request carries the conversation, so the body
and the `Authorization` header stay out.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from loguru import logger

from nanoinfra.api.server import create_app

try:
    from aiohttp.test_utils import TestClient, TestServer

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

pytest_plugins = ("pytest_asyncio",)

API_KEY = "secret-key-value"
AUTH = {"Authorization": f"Bearer {API_KEY}"}


@pytest.fixture
def lines() -> Any:
    captured: list[str] = []
    sink_id = logger.add(lambda message: captured.append(message), level="DEBUG")
    logger.enable("nanoinfra")
    try:
        yield captured
    finally:
        logger.remove(sink_id)


@pytest_asyncio.fixture
async def client() -> Any:
    if not HAS_AIOHTTP:
        pytest.skip("aiohttp is not installed")
    agent = MagicMock()
    agent.process_direct = AsyncMock(return_value="an answer")
    agent._last_usage = None
    app = create_app(agent, model_name="test-model", request_timeout=5.0, api_key=API_KEY)
    test_client = TestClient(TestServer(app))
    await test_client.start_server()
    try:
        yield test_client
    finally:
        await test_client.close()


def _api_lines(lines: list[str]) -> list[str]:
    return [line for line in lines if " api " in line or line.strip().startswith("api ")]


async def test_a_served_request_is_logged_with_its_status(client: Any, lines: Any) -> None:
    await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": [{"role": "user", "content": "hola"}]},
    )

    logged = " ".join(_api_lines(lines))
    assert "POST /v1/chat/completions" in logged
    assert "200" in logged


async def test_a_refusal_says_why(client: Any, lines: Any) -> None:
    """The line that was invisible, and the one that cost a packet capture: a request rejected in
    the parser, before any handler ran."""
    await client.post(
        "/v1/responses",
        headers=AUTH,
        json={"input": [{"type": "function_call_output", "call_id": "c", "output": "x"}]},
    )

    logged = " ".join(_api_lines(lines))
    assert "400" in logged
    assert "runs its own tools" in logged


async def test_an_unauthenticated_request_is_logged(client: Any, lines: Any) -> None:
    """The access log sits outside the auth layer on purpose: a 401 nobody can see is how a
    misconfigured client looks like a hang."""
    await client.get("/v1/models")

    logged = " ".join(_api_lines(lines))
    assert "401" in logged


async def test_the_log_never_carries_the_key(client: Any, lines: Any) -> None:
    await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={"messages": [{"role": "user", "content": "hola"}]},
    )

    assert API_KEY not in " ".join(lines)
    assert "Authorization" not in " ".join(_api_lines(lines))


async def test_the_log_never_carries_what_was_said(client: Any, lines: Any) -> None:
    """A log holding the prompt would be a second copy of the transcript somewhere nobody expects
    one. The session name is the caller's own label and is fine; the message is not."""
    await client.post(
        "/v1/chat/completions",
        headers=AUTH,
        json={
            "messages": [{"role": "user", "content": "una frase secreta muy identificable"}],
            "session_id": "alberto",
        },
    )

    logged = " ".join(_api_lines(lines))
    assert "secreta" not in logged
    assert "api:alberto" in logged


async def test_the_duration_is_reported(client: Any, lines: Any) -> None:
    await client.get("/health")

    assert any("ms" in line for line in _api_lines(lines))


def test_a_server_keeps_its_own_logger_audible_without_verbose() -> None:
    """`logger.disable("nanoinfra")` silenced the API's tracebacks too, which is what made the
    production hang unreadable.

    Exercised through a real call site *inside* `nanoinfra.api.server` -- `create_app` warns when
    no key is configured -- because loguru filters on the module that makes the call. A test that
    called `api_server.logger.info(...)` itself would record the test's own module name and prove
    nothing, which is how the first version of this test passed while the filter did nothing.
    """
    if not HAS_AIOHTTP:
        pytest.skip("aiohttp is not installed")
    from nanoinfra.cli.log_control import _set_nanoinfra_logs

    def _warning_lines(always_on: tuple[str, ...]) -> list[str]:
        captured: list[str] = []
        sink_id = logger.add(lambda message: captured.append(str(message)), level="DEBUG")
        try:
            _set_nanoinfra_logs(False, always_on=always_on)
            create_app(MagicMock(), model_name="m", request_timeout=1.0, api_key="")
        finally:
            logger.remove(sink_id)
            logger.enable("nanoinfra")
        return [line for line in captured if "no api_key" in line]

    assert _warning_lines(("nanoinfra.api",)), "the API's own warning must survive the silence"
    assert not _warning_lines(()), "and without the exemption the whole package stays quiet"
