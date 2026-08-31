"""The confined host, and what it refuses even though the executor already checked (#195, part 4).

The host is close to stateless on purpose: it receives a rendered URL, a token and a deadline, makes
the call, projects the response, and answers. These tests pin the two re-checks -- because the frame
carries a URL, and a frame naming a real package with a URL from somewhere else would be a request
this process makes with a live token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.gates.connector_host.protocol import (
    PROTOCOL_VERSION,
    ConnectorHostRequest,
    ConnectorHostResponse,
    ProtocolError,
    decode_request,
    decode_response,
    encode_request,
    encode_response,
)
from nanoinfra.gates.connector_host.server import ConnectorHost

_MANIFEST: dict[str, Any] = {
    "$schema": "https://nanoinfra.org/schemas/connector/1.0.0/connector.schema.json",
    "name": "acme-crm",
    "displayName": "Acme CRM",
    "baseUrl": "https://api.acme.example",
    "credential": {
        "kind": "oauth2",
        "tokenUrl": "https://api.acme.example/oauth/token",
        "allowedHosts": ["api.acme.example"],
        "scopes": {"read": ["crm.read"]},
    },
    "operations": [
        {
            "name": "list_contacts",
            "class": "read",
            "method": "GET",
            "path": "/v1/contacts",
            "collection": "items",
            "returns": ["id", "name"],
        }
    ],
    "dependencies": [],
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    package = tmp_path / "acme-crm"
    package.mkdir()
    (package / "connector.json").write_text(json.dumps(_MANIFEST), encoding="utf-8")
    return tmp_path


def _request(**overrides: Any) -> ConnectorHostRequest:
    payload: dict[str, Any] = {
        "request_id": 7,
        "package": "acme-crm",
        "operation": "list_contacts",
        "method": "GET",
        "url": "https://api.acme.example/v1/contacts",
        "access_token": "at-1",
    }
    payload.update(overrides)
    return ConnectorHostRequest(**payload)


# --- the wire ----------------------------------------------------------------------------


def test_a_request_round_trips() -> None:
    request = _request(query={"limit": "10"})

    assert decode_request(encode_request(request)) == request


def test_a_response_round_trips() -> None:
    response = ConnectorHostResponse(
        request_id=7, ok=True, status=200, payload={"items": []}, error=None
    )

    assert decode_response(encode_response(response)) == response


def test_a_frame_with_an_unknown_field_is_refused() -> None:
    """Ignoring a field on a wire that hands out tokens is the hole."""
    payload = json.loads(encode_request(_request()))
    payload["scopes"] = ["crm.write"]

    with pytest.raises(ProtocolError, match="unknown field"):
        decode_request(json.dumps(payload).encode("utf-8"))


def test_a_frame_from_another_protocol_version_is_refused() -> None:
    payload = json.loads(encode_request(_request()))
    payload["v"] = PROTOCOL_VERSION + 1

    with pytest.raises(ProtocolError, match="protocol version"):
        decode_request(json.dumps(payload).encode("utf-8"))


def test_a_frame_naming_no_operation_is_refused() -> None:
    payload = json.loads(encode_request(_request()))
    payload.pop("op")

    with pytest.raises(ProtocolError, match="names no operation"):
        decode_request(json.dumps(payload).encode("utf-8"))


# --- the re-checks -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_url_from_another_host_is_refused(root: Path) -> None:
    """The frame carries a rendered URL, so the host confirms it belongs to the package it named.
    Without this, a frame naming a real package could make this process send a live token
    anywhere."""
    host = ConnectorHost(root)

    answer = await host.handle(_request(url="https://evil.example/v1/contacts"))

    assert answer.ok is False
    assert "evil.example" in (answer.error or "")


@pytest.mark.asyncio
async def test_an_operation_the_package_does_not_declare_is_refused(root: Path) -> None:
    host = ConnectorHost(root)

    answer = await host.handle(_request(operation="delete_everything"))

    assert answer.ok is False
    assert "declares no operation" in (answer.error or "")


@pytest.mark.asyncio
async def test_a_method_the_operation_does_not_declare_is_refused(root: Path) -> None:
    """A GET declared as a read must not become a DELETE because a frame said so."""
    host = ConnectorHost(root)

    answer = await host.handle(_request(method="DELETE"))

    assert answer.ok is False
    assert "declared GET" in (answer.error or "")


@pytest.mark.asyncio
async def test_a_package_that_is_not_installed_is_refused(root: Path) -> None:
    host = ConnectorHost(root)

    answer = await host.handle(_request(package="other-crm"))

    assert answer.ok is False
    assert "not loadable" in (answer.error or "")


@pytest.mark.asyncio
async def test_an_http_url_is_refused(root: Path) -> None:
    host = ConnectorHost(root)

    answer = await host.handle(_request(url="http://api.acme.example/v1/contacts"))

    assert answer.ok is False


# --- the call ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successful_call_returns_the_package_s_own_projection(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response is reduced to the declared `returns` before it crosses back, so a connector
    that returned more than it promised cannot use this process to carry it into the context."""
    seen: dict[str, Any] = {}

    async def _request_impl(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        seen["method"] = method
        seen["url"] = url
        seen["headers"] = dict(kwargs.get("headers") or {})
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "1", "name": "Ada", "internalNotes": "do not show the model"},
                ],
                "nextPageToken": "p2",
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "request", _request_impl)
    host = ConnectorHost(root)

    answer = await host.handle(_request())

    assert answer.ok is True
    assert answer.status == 200
    body = json.dumps(answer.payload)
    assert "Ada" in body
    assert "internalNotes" not in body
    # The token travels as a bearer header, and nothing else about the credential does.
    assert seen["headers"]["Authorization"] == "Bearer at-1"


@pytest.mark.asyncio
async def test_an_api_error_carries_no_provider_prose(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An error body is the field most likely to quote the request back, and a projection built
    from it would read as data."""

    async def _request_impl(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        return httpx.Response(403, json={"error": {"message": "caller lacks crm.read"}})

    monkeypatch.setattr(httpx.AsyncClient, "request", _request_impl)
    host = ConnectorHost(root)

    answer = await host.handle(_request())

    assert answer.ok is False
    assert answer.status == 403
    assert "crm.read" not in (answer.error or "")
    assert answer.retryable is False


@pytest.mark.asyncio
async def test_a_rate_limit_is_retryable_and_a_permission_error_is_not(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    statuses = [429, 500, 401]
    for status in statuses:

        async def _request_impl(
            self: httpx.AsyncClient, method: str, url: str, _status: int = status, **kwargs: Any
        ) -> httpx.Response:
            return httpx.Response(_status, json={})

        monkeypatch.setattr(httpx.AsyncClient, "request", _request_impl)
        answer = await ConnectorHost(root).handle(_request())
        assert answer.retryable is (status in {429, 500}), status


@pytest.mark.asyncio
async def test_a_transport_failure_is_retryable_and_names_no_url(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _request_impl(
        self: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        raise httpx.ConnectError("no route to host api.acme.example")

    monkeypatch.setattr(httpx.AsyncClient, "request", _request_impl)

    answer = await ConnectorHost(root).handle(_request())

    assert answer.ok is False
    assert answer.retryable is True
    assert "api.acme.example" not in (answer.error or "")
