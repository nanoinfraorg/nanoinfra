# tests/servers/execution/test_api_backend.py
from __future__ import annotations

import httpx
import pytest

from nanoinfra.servers.execution.api_backend import ApiBackend
from nanoinfra.servers.types import Server


def _server(base_url: str = "http://10.0.1.5:8080") -> Server:
    return Server(
        id="a" * 32,
        name="test-server",
        provider_id="api",
        config={"baseUrl": base_url},
        secret_ref=None,
        tags=[],
        created_at="t",
        updated_at="t",
    )


@pytest.mark.asyncio
async def test_run_makes_get_request_by_default(respx_mock):
    respx_mock.get("http://10.0.1.5:8080/status").mock(return_value=httpx.Response(200, text="ok"))

    backend = ApiBackend()
    result = await backend.run(_server(), "/status", None, on_activity=lambda _c: None)

    assert result.exit_code == 0
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_run_parses_method_prefix(respx_mock):
    respx_mock.post("http://10.0.1.5:8080/reboot").mock(return_value=httpx.Response(202, text="rebooting"))

    backend = ApiBackend()
    result = await backend.run(_server(), "POST /reboot", None, on_activity=lambda _c: None)

    assert result.exit_code == 0
    assert result.output == "rebooting"


@pytest.mark.asyncio
async def test_non_2xx_status_reported_as_nonzero_exit(respx_mock):
    respx_mock.get("http://10.0.1.5:8080/status").mock(return_value=httpx.Response(500, text="oops"))

    backend = ApiBackend()
    result = await backend.run(_server(), "/status", None, on_activity=lambda _c: None)

    assert result.exit_code == 1
    assert "oops" in result.output


@pytest.mark.asyncio
async def test_secret_value_sent_as_bearer_token():
    server = _server()
    backend = ApiBackend()
    captured: dict[str, object] = {}

    async def fake_send(self, request, **kwargs):
        captured["auth_header"] = request.headers.get("authorization")
        return httpx.Response(200, text="ok", request=request)

    import httpx as httpx_module

    original = httpx_module.AsyncClient.send
    httpx_module.AsyncClient.send = fake_send  # type: ignore[method-assign]
    try:
        await backend.run(server, "/status", "my-token", on_activity=lambda _c: None)
    finally:
        httpx_module.AsyncClient.send = original  # type: ignore[method-assign]

    assert captured["auth_header"] == "Bearer my-token"


@pytest.mark.asyncio
async def test_blocked_target_returns_error_without_making_a_request():
    backend = ApiBackend()
    result = await backend.run(_server(base_url="http://169.254.169.254/latest"), "/meta", None, on_activity=lambda _c: None)

    assert result.exit_code is None
    assert "blocked" in result.error.lower()


@pytest.mark.asyncio
async def test_connection_error_is_reported_not_raised(respx_mock):
    respx_mock.get("http://10.0.1.5:8080/status").mock(side_effect=httpx.ConnectError("refused"))

    backend = ApiBackend()
    result = await backend.run(_server(), "/status", None, on_activity=lambda _c: None)

    assert result.exit_code is None
    assert result.error is not None
