"""Shared HTTP helpers for the embedded WebUI gateway."""

from __future__ import annotations

import email.utils
import gzip
import hmac
import http
import ipaddress
import json
import re
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from websockets.datastructures import Headers
from websockets.http11 import Response

QueryParams = dict[str, list[str]]

_JSON_GZIP_MIN_BYTES = 4 * 1024
_JSON_GZIP_LEVEL = 5

# The two attributes ``ws_http.dispatch`` writes on a request, once per request (#62). One file
# owns the two names, because a reader that misspelled one would read "nobody asserted an
# identity" and the person would silently become the shared ``webui`` actor.
#
# The identity is the value. The flag is derived from it, so the two cannot disagree, and a
# route that needs only a yes or no reads the flag rather than repeat the derivation.
TRUSTED_PROXY_IDENTITY_ATTR = "_nanoinfra_trusted_proxy_identity"
TRUSTED_PROXY_AUTHENTICATED_ATTR = "_nanoinfra_trusted_proxy_authenticated"
# The storage key of a verified identity, which is not its name. Empty for a
# `plain` assertion and for a token that carries no such claim, and a caller that
# reads nothing here gets the shared default workspace rather than a refusal.
TRUSTED_PROXY_WORKSPACE_KEY_ATTR = "_nanoinfra_trusted_proxy_workspace_key"

# The longest identity this gateway will name (#63). An identity reaches an audit record and
# ``gates.approvers`` compares it whole, so a longer value refuses rather than arrive cut: a
# truncated name belongs to nobody, and two identities that share one prefix would collapse into
# one authority. The bound is above the longest address RFC 5321 permits, so no legal email
# address meets it.
MAX_IDENTITY_CHARS = 254


def strip_trailing_slash(path: str) -> str:
    if len(path) > 1 and path.endswith("/"):
        return path.rstrip("/")
    return path or "/"


def normalize_config_path(path: str) -> str:
    return strip_trailing_slash(path)


def case_insensitive_header(headers: Any, key: str) -> str:
    """Read a header from websockets/http test stubs without assuming casing."""
    try:
        value = headers.get(key)
    except Exception:
        value = None
    if value is None:
        try:
            value = headers.get(key.lower())
        except Exception:
            value = None
    return str(value or "").strip()


def combined_list_header(headers: Any, key: str) -> str:
    """Combine repeated values for a comma-separated HTTP list header."""
    try:
        values = headers.get_all(key)
    except (AttributeError, KeyError):
        return case_insensitive_header(headers, key)
    return ", ".join(str(value).strip() for value in values if str(value).strip())


def safe_host_header(value: str) -> str:
    """Return a safe Host header value, or empty when it should not be echoed."""
    value = value.strip()
    if not value:
        return ""
    if re.fullmatch(r"\[[0-9A-Fa-f:.]+\](?::\d{1,5})?", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", value):
        return value
    return ""


def host_for_url(host: str, port: int) -> str:
    host = host.strip()
    if host in ("0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"{host}:{port}"


def _accepts_gzip(value: str) -> bool:
    wildcard_quality: float | None = None
    for item in value.split(","):
        name, *params = (part.strip() for part in item.split(";"))
        quality = 1.0
        for param in params:
            key, separator, raw_value = param.partition("=")
            if separator and key.strip().lower() == "q":
                try:
                    quality = float(raw_value.strip())
                except ValueError:
                    quality = 0.0
                break
        if name.lower() == "gzip":
            return quality > 0
        if name == "*":
            wildcard_quality = quality
    return wildcard_quality is not None and wildcard_quality > 0


def http_json_response(
    data: dict[str, Any],
    *,
    status: int = 200,
    accept_encoding: str | None = None,
) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Type", "application/json; charset=utf-8"),
    ]
    if accept_encoding is not None:
        headers.append(("Vary", "Accept-Encoding"))
        if len(body) >= _JSON_GZIP_MIN_BYTES and _accepts_gzip(accept_encoding):
            body = gzip.compress(body, compresslevel=_JSON_GZIP_LEVEL, mtime=0)
            headers.append(("Content-Encoding", "gzip"))
    headers.append(("Content-Length", str(len(body))))
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def http_response(
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "text/plain; charset=utf-8",
    extra_headers: list[tuple[str, str]] | None = None,
) -> Response:
    headers = [
        ("Date", email.utils.formatdate(usegmt=True)),
        ("Connection", "close"),
        ("Content-Length", str(len(body))),
        ("Content-Type", content_type),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    reason = http.HTTPStatus(status).phrase
    return Response(status, reason, Headers(headers), body)


def http_error(status: int, message: str | None = None) -> Response:
    body = (message or http.HTTPStatus(status).phrase).encode("utf-8")
    return http_response(body, status=status)


def parse_request_path(path_with_query: str) -> tuple[str, QueryParams]:
    """Parse normalized path and query parameters in one pass."""
    parsed = urlparse("ws://x" + path_with_query)
    path = strip_trailing_slash(parsed.path or "/")
    return path, parse_qs(parsed.query, keep_blank_values=True)


def parse_query(path_with_query: str) -> QueryParams:
    return parse_request_path(path_with_query)[1]


def query_first(query: QueryParams, key: str) -> str | None:
    values = query.get(key)
    return values[0] if values else None


def is_localhost(connection: Any) -> bool:
    addr = getattr(connection, "remote_address", None)
    if not addr:
        return False
    host = cast(Any, addr[0] if isinstance(addr, tuple) else addr)
    if not isinstance(host, str):
        return False
    if host.startswith("::ffff:"):
        host = host[7:]
    return host in {"127.0.0.1", "::1", "localhost"}


def _connection_ip(connection: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    addr = getattr(connection, "remote_address", None)
    host = cast(Any, addr[0] if isinstance(addr, tuple) else addr)
    if not isinstance(host, str):
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _address_matches_network(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    network: ipaddress.IPv4Network | ipaddress.IPv6Network,
) -> bool:
    if isinstance(address, ipaddress.IPv4Address):
        if isinstance(network, ipaddress.IPv4Network):
            return address in network
        return ipaddress.IPv6Address(f"::ffff:{address}") in network
    if isinstance(network, ipaddress.IPv6Network):
        return address in network
    mapped = address.ipv4_mapped
    return mapped is not None and mapped in network


def trusted_proxy_peer_assertion(
    connection: Any,
    headers: Any,
    trusted_proxy_auth: Any,
) -> str:
    """Return the raw assertion when a trusted proxy peer sent one, else an empty string.

    This is the peer check and nothing more. It proves that the TCP peer sits inside
    ``trustedPeerCidrs`` and that the assertion header is not empty. It proves **nothing about
    the person**, so it must not be read as an authentication (nanoinfraorg/nanoinfra#58).

    The function used to answer a boolean named ``is_trusted_proxy_authenticated_request``, and
    that name was the whole gap: anything that reached the gateway from the trusted CIDR could
    invent a person. ``assertion_identity.TrustedProxyAuthenticator`` is the only caller that
    turns this string into an identity, and on the ``jwt`` path it verifies a signature first.

    ``trusted_proxy_auth`` is the block itself rather than the channel config, so a caller
    cannot pass a config whose block is missing and receive a value.
    """
    if trusted_proxy_auth is None:
        return ""
    address = _connection_ip(connection)
    if address is None:
        return ""
    networks = getattr(trusted_proxy_auth, "_trusted_peer_networks", ())
    if not any(_address_matches_network(address, network) for network in networks):
        return ""
    assertion_header = getattr(trusted_proxy_auth, "assertion_header", "")
    if not assertion_header:
        return ""
    return case_insensitive_header(headers, assertion_header)


def _host_without_port(value: str) -> str:
    value = value.strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host
    return value


def is_loopback_host(value: str) -> bool:
    host = _host_without_port(value)
    if host.startswith("::ffff:"):
        host = host[7:]
    host = host.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_comma_header(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _forwarded_header_values(value: str, key: str) -> list[str]:
    values: list[str] = []
    for entry in _split_comma_header(value):
        for part in entry.split(";"):
            name, sep, raw = part.partition("=")
            if sep and name.strip().lower() == key:
                cleaned = raw.strip().strip('"')
                if cleaned:
                    values.append(cleaned)
    return values


def _all_forwarded_values_are_loopback(headers: Any) -> bool:
    checks: list[str] = []
    checks.extend(_split_comma_header(case_insensitive_header(headers, "X-Forwarded-For")))
    checks.extend(_split_comma_header(case_insensitive_header(headers, "X-Real-IP")))
    checks.extend(_split_comma_header(case_insensitive_header(headers, "X-Forwarded-Host")))
    forwarded = case_insensitive_header(headers, "Forwarded")
    checks.extend(_forwarded_header_values(forwarded, "for"))
    checks.extend(_forwarded_header_values(forwarded, "host"))
    return all(is_loopback_host(value) for value in checks)


def is_local_browser_request(connection: Any, headers: Any) -> bool:
    """Return True only for a local TCP peer presenting a local browser origin."""
    if not is_localhost(connection):
        return False
    host = case_insensitive_header(headers, "Host")
    if not is_loopback_host(host):
        return False
    return _all_forwarded_values_are_loopback(headers)


def bearer_token(headers: Any) -> str | None:
    auth = headers.get("Authorization") or headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def issue_route_secret_matches(headers: Any, configured_secret: str) -> bool:
    if not configured_secret:
        return True
    authorization = headers.get("Authorization") or headers.get("authorization")
    if authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
        return hmac.compare_digest(supplied, configured_secret)
    header_token = headers.get("X-Nanoinfra-Auth") or headers.get("x-nanoinfra-auth")
    if not header_token:
        return False
    return hmac.compare_digest(header_token.strip(), configured_secret)
