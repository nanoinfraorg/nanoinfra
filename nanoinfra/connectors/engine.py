"""Perform one declared operation, and return only the fields it declared.

The engine is data-driven on purpose. A manifest gives a method, a path and a projection;
this builds the request, sends it over the same pinned-DNS client the MCP HTTP transport
uses, and reduces the response to the declared fields. No connector ships code that makes
the call, so "this operation is a read" stays checkable: the class sits beside the verb.

Three behaviours are worth stating because they are the ones an operator will meet:

- **A path placeholder is filled from a named argument, URL-quoted whole.** A calendar id is
  often an email address, and `/` inside a value must stay a value rather than becoming a
  path segment.
- **An expired token is refreshed once, then retried once.** A refresh that fails is a
  refusal naming the connector, not a traceback, because the fix is re-authorisation.
- **A 429 or a 5xx is a failure of the action, not of the gate.** It carries `Retry-After`
  when the server sent one, and the caller decides.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol, cast
from urllib.parse import quote

import httpx
from loguru import logger

from nanoinfra.connectors.contracts import ConnectorOperation, ConnectorPlugin
from nanoinfra.security.network import (
    PinnedDNSAsyncTransport,
    httpx_env_proxy_mounts,
    validate_url_target,
)

# A connector answers a tool call, so it cannot wait as long as a backup job.
DEFAULT_TIMEOUT_S = 30.0

# What one call may bring back. A mailbox listing with no cap is a context window spent on
# one tool result; the projection cuts the fields and this cuts the bytes.
MAX_RESPONSE_BYTES = 2 * 1024 * 1024

_PLACEHOLDER_OPEN = "{"


class ConnectorCallError(Exception):
    """A call that did not happen, or did not succeed, with the operator's next step.

    ``reauthorize`` marks the one failure a person has to act on: the credential no longer
    works. ``retryable`` marks the ones that may work unchanged.
    """

    def __init__(self, message: str, *, retryable: bool = False, reauthorize: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.reauthorize = reauthorize


class TokenSource(Protocol):
    """Mints one short-lived access token for one connector and one class.

    The connector never sees the refresh token. `force_refresh` exists so a 401 costs one
    exchange rather than a whole re-authorisation, and the class is a parameter because the
    token is minted for the scopes that class declared — a read receives a token that cannot
    write, for minutes.
    """

    async def access_token(
        self, connector: str, capability_class: str, *, force_refresh: bool = False
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class PreparedRequest:
    """Exactly what will go on the wire, before a token is minted.

    Built separately so it can be shown. A preview renders this and asks nobody, which is
    the same split `ExecuteResponse.preview_command` already makes for a shell command.
    """

    method: str
    url: str
    params: dict[str, Any] = dataclass_field(default_factory=dict[str, Any])
    body: dict[str, Any] | None = None

    def describe(self) -> str:
        """One line an approver can read. No token, no body values."""
        query = "?" + "&".join(sorted(self.params)) if self.params else ""
        fields = ", ".join(sorted(self.body)) if self.body else ""
        suffix = f" with {fields}" if fields else ""
        return f"{self.method} {self.url}{query}{suffix}"


def _placeholders(path: str) -> tuple[str, ...]:
    parts: list[str] = []
    rest = path
    while _PLACEHOLDER_OPEN in rest:
        _, _, tail = rest.partition(_PLACEHOLDER_OPEN)
        name, closed, rest = tail.partition("}")
        if not closed:
            raise ConnectorCallError(f"operation path {path!r} has an unclosed placeholder")
        parts.append(name)
    return tuple(parts)


def prepare(
    plugin: ConnectorPlugin,
    op: ConnectorOperation,
    arguments: dict[str, Any],
    *,
    defaults: dict[str, Any] | None = None,
) -> PreparedRequest:
    """Turn declared metadata plus call arguments into one request.

    Arguments that fill a placeholder leave the argument set. What remains becomes a query
    string on a `GET` and a JSON body on anything else — the shape every Workspace API
    documents, and the reason a connector needs no code of its own for the common case.
    """
    supplied: dict[str, Any] = {**(defaults or {}), **arguments}
    path = op.path
    for name in _placeholders(op.path):
        value = supplied.pop(name, None)
        if value in (None, ""):
            raise ConnectorCallError(
                f"{plugin.tool_name(op)} needs {name!r}: the path names it and no value was given"
            )
        # Quoted whole, with no safe characters. A value that carries '/' is a value, not a
        # path segment, and a value that carries '..' must not climb the API's own routes.
        path = path.replace(f"{{{name}}}", quote(str(value), safe=""))

    url = plugin.base_url.rstrip("/") + path
    ok, error = validate_url_target(url)
    if not ok:
        raise ConnectorCallError(f"{plugin.name}: refusing {url} ({error})")

    if op.method == "GET":
        return PreparedRequest(method=op.method, url=url, params=supplied)
    return PreparedRequest(method=op.method, url=url, body=supplied)


def _pluck(payload: Any, parts: tuple[str, ...]) -> tuple[bool, Any]:
    """Read one dotted path, mapping over a list rather than indexing into it."""
    if not parts:
        return True, payload
    head, rest = parts[0], parts[1:]
    if isinstance(payload, list):
        items: list[Any] = []
        for element in cast(list[Any], payload):
            found, value = _pluck(element, parts)
            if found:
                items.append(value)
        return bool(items), items
    if isinstance(payload, dict):
        mapping = cast(dict[str, Any], payload)
        if head not in mapping:
            return False, None
        return _pluck(mapping[head], rest)
    return False, None


def _graft(target: dict[str, Any], parts: tuple[str, ...], value: Any) -> None:
    """Write one dotted path into the projection, keeping the response's shape."""
    head, rest = parts[0], parts[1:]
    if not rest:
        target[head] = value
        return
    if isinstance(value, list):
        # `attendees.email` came back as a list of emails; put each one back under its key so
        # the model reads the same shape the API documents.
        target[head] = [{rest[-1]: item} for item in cast(list[Any], value)]
        return
    nested = target.setdefault(head, {})
    if isinstance(nested, dict):
        _graft(cast(dict[str, Any], nested), rest, value)


def _project_one(payload: Any, returns: tuple[str, ...]) -> Any:
    if not returns or not isinstance(payload, dict):
        return payload
    projected: dict[str, Any] = {}
    for path in returns:
        parts = tuple(path.split("."))
        found, value = _pluck(payload, parts)
        if found:
            _graft(projected, parts, value)
    return projected


def project(payload: Any, op: ConnectorOperation) -> Any:
    """Reduce a response to the fields the operation declared.

    An operation with a ``collection`` projects each element and keeps the rest of the
    envelope, which is where the paging key lives. Without one the whole object is projected.
    """
    if not op.returns:
        return payload
    if op.collection and isinstance(payload, dict):
        envelope = cast(dict[str, Any], payload)
        items = envelope.get(op.collection)
        if isinstance(items, list):
            kept = {
                key: value
                for key, value in envelope.items()
                if key != op.collection and not isinstance(value, (dict, list))
            }
            kept[op.collection] = [
                _project_one(item, op.returns) for item in cast(list[Any], items)
            ]
            return kept
    return _project_one(payload, op.returns)


def _client() -> httpx.AsyncClient:
    """The same client shape the MCP HTTP transport uses: pinned DNS, no redirects.

    Following a redirect would let the answer to a validated URL come from another one, and
    a connector has no reason to be redirected: the base URL is in its manifest.
    """
    mounts = httpx_env_proxy_mounts()
    kwargs: dict[str, Any] = {"transport": PinnedDNSAsyncTransport()}
    if mounts:
        kwargs["mounts"] = mounts
    return httpx.AsyncClient(follow_redirects=False, **kwargs)


def _decode(response: httpx.Response, plugin_name: str) -> Any:
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise ConnectorCallError(
            f"{plugin_name} returned {len(body)} bytes, above the {MAX_RESPONSE_BYTES} cap"
        )
    if not body:
        return {}
    try:
        return cast(object, json.loads(body))
    except ValueError as exc:
        raise ConnectorCallError(f"{plugin_name} returned a body that is not JSON: {exc}") from exc


def _api_error_message(payload: Any, status: int) -> str:
    """The API's own message, when it sent one. Google nests it under `error.message`."""
    if isinstance(payload, dict):
        error = cast(dict[str, Any], payload).get("error")
        if isinstance(error, dict):
            message = cast(dict[str, Any], error).get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(error, str) and error:
            return error
    return f"HTTP {status}"


async def _send(
    client: httpx.AsyncClient, prepared: PreparedRequest, *, token: str, timeout_s: float
) -> httpx.Response:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return await client.request(
        prepared.method,
        prepared.url,
        params=prepared.params or None,
        json=prepared.body if prepared.body is not None else None,
        headers=headers,
        timeout=timeout_s,
    )


async def call(
    plugin: ConnectorPlugin,
    op: ConnectorOperation,
    arguments: dict[str, Any],
    *,
    tokens: TokenSource,
    defaults: dict[str, Any] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Any:
    """Perform one operation and return its projection.

    The retry is deliberately one attempt and only for an expired token. A retry loop on a
    `mutate.remote` operation would repeat a write that may have succeeded, and "did it send
    the mail twice" is a worse question than "it failed, run it again".
    """
    prepared = prepare(plugin, op, arguments, defaults=defaults)
    # A connector against a public API declares `credential.kind = "none"`, and asking for a token
    # it never had would fail before the call rather than during it.
    token = (
        ""
        if plugin.credential.kind == "none"
        else await tokens.access_token(plugin.name, op.capability_class)
    )

    async with _client() as client:
        response = await _send(client, prepared, token=token, timeout_s=timeout_s)
        if response.status_code == 401 and token:
            logger.debug("connector {} got 401 on {}; refreshing once", plugin.name, op.name)
            token = await tokens.access_token(
                plugin.name, op.capability_class, force_refresh=True
            )
            response = await _send(client, prepared, token=token, timeout_s=timeout_s)

        payload = _decode(response, plugin.name)
        status = response.status_code

        if status == 401:
            raise ConnectorCallError(
                f"{plugin.name} was refused after a refresh: the credential no longer works, "
                "so the connector needs re-authorising.",
                reauthorize=True,
            )
        if status == 403:
            raise ConnectorCallError(
                f"{plugin.name} refused {op.name}: {_api_error_message(payload, status)}. "
                f"This operation needs {', '.join(plugin.credential.scopes_for(op.capability_class)) or 'a scope'}, "
                "so check the scopes the credential was granted.",
                reauthorize=True,
            )
        if status == 429 or status >= 500:
            retry_after = response.headers.get("retry-after", "")
            hint = f" Retry-After: {retry_after}." if retry_after else ""
            raise ConnectorCallError(
                f"{plugin.name} {op.name} failed with HTTP {status}: "
                f"{_api_error_message(payload, status)}.{hint}",
                retryable=True,
            )
        if status >= 400:
            raise ConnectorCallError(
                f"{plugin.name} {op.name} failed with HTTP {status}: "
                f"{_api_error_message(payload, status)}"
            )

    return project(payload, op)


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "ConnectorCallError",
    "PreparedRequest",
    "TokenSource",
    "call",
    "prepare",
    "project",
]
