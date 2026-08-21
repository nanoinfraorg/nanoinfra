"""Token state for the embedded WebUI gateway."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from websockets.http11 import Request as WsRequest

from nanoinfra.webui.http_utils import bearer_token, parse_query, query_first

#: Issued-token prefix. Was ``nbwt_`` -- nanobot web token -- a leftover from the rename that was
#: baked into a credential this project hands out.
TOKEN_PREFIX = "nwt_"

IssuedTokenAudience = Literal["client", "webui"]

#: What a token is allowed to reach.
#:
#: ``api_tokens`` used to be a bare ``dict[str, float]`` and ``check_api_token`` a boolean, so any
#: API token was authority over every route the gateway serves. That was survivable while the WebUI
#: was the only holder. It stops being survivable the moment a second, narrower holder exists --
#: the TUI adoption needs exactly that, and one scope model beats two.
#:
#: ``operate`` implies every other scope. A route that has not been audited requires ``operate``,
#: so narrowing is opt-in and reviewable rather than a default that a new route silently inherits.
TokenScope = Literal["operate", "read", "chat", "approve", "secrets"]

OPERATE_SCOPE: TokenScope = "operate"
ALL_SCOPES: frozenset[TokenScope] = frozenset(
    {"operate", "read", "chat", "approve", "secrets"}
)


@dataclass
class GatewayTokenStore:
    """Own short-lived WebSocket and WebUI API tokens for one gateway process."""

    max_tokens: int = 10_000
    issued_tokens: dict[str, float] = field(default_factory=dict)
    issued_token_audiences: dict[str, IssuedTokenAudience] = field(default_factory=dict)
    api_tokens: dict[str, float] = field(default_factory=dict)
    #: Scopes granted per API token. A token absent from this map is treated as ``operate``, which
    #: keeps a token issued before scopes existed working for the process that issued it.
    api_token_scopes: dict[str, frozenset[TokenScope]] = field(default_factory=dict)

    def check_api_token(
        self,
        request: WsRequest,
        *,
        scope: TokenScope = OPERATE_SCOPE,
    ) -> bool:
        """True when the request carries a live API token that holds *scope*.

        The default is ``operate``, so a route that says nothing requires the widest scope. Fail
        closed: a new route added without thinking about scopes must be operator-only rather than
        reachable by whatever narrow token happens to exist.
        """
        self._purge_expired_api_tokens()
        token = bearer_token(request.headers) or query_first(
            parse_query(request.path), "token"
        )
        if not token:
            return False
        expiry = self.api_tokens.get(token)
        if expiry is None or time.monotonic() > expiry:
            self.api_tokens.pop(token, None)
            self.api_token_scopes.pop(token, None)
            return False
        granted = self.api_token_scopes.get(token)
        if granted is None:
            return True
        return OPERATE_SCOPE in granted or scope in granted

    def can_issue(self, *, include_api_token: bool = False) -> bool:
        self._purge_expired_issued_tokens()
        self._purge_expired_api_tokens()
        if len(self.issued_tokens) >= self.max_tokens:
            return False
        if include_api_token and len(self.api_tokens) >= self.max_tokens:
            return False
        return True

    def issue_token(
        self,
        ttl_s: int | float,
        *,
        audience: IssuedTokenAudience = "client",
    ) -> str:
        token_value = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.issued_tokens[token_value] = expiry
        self.issued_token_audiences[token_value] = audience
        return token_value

    def issue_api_token(
        self,
        ttl_s: int | float,
        *,
        scopes: frozenset[TokenScope] | set[TokenScope] | None = None,
    ) -> str:
        """Mint an API token. Without *scopes* it holds ``operate``, as every token did before."""
        token_value = f"{TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
        expiry = time.monotonic() + float(ttl_s)
        self.api_tokens[token_value] = expiry
        self.api_token_scopes[token_value] = frozenset(scopes or {OPERATE_SCOPE})
        return token_value

    def take_issued_token_if_valid(self, token_value: str | None) -> bool:
        return self.take_issued_token_audience(token_value) is not None

    def take_issued_token_audience(
        self,
        token_value: str | None,
    ) -> IssuedTokenAudience | None:
        if not token_value:
            return None
        self._purge_expired_issued_tokens()
        expiry = self.issued_tokens.pop(token_value, None)
        if expiry is None:
            self.issued_token_audiences.pop(token_value, None)
            return None
        audience = self.issued_token_audiences.pop(token_value, "client")
        if time.monotonic() > expiry:
            return None
        return audience

    def clear(self) -> None:
        self.issued_tokens.clear()
        self.issued_token_audiences.clear()
        self.api_tokens.clear()
        self.api_token_scopes.clear()

    def _purge_expired_api_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self.api_tokens.items()):
            if now > expiry:
                self.api_tokens.pop(token_key, None)
                self.api_token_scopes.pop(token_key, None)

    def _purge_expired_issued_tokens(self) -> None:
        now = time.monotonic()
        for token_key, expiry in list(self.issued_tokens.items()):
            if now > expiry:
                self.issued_tokens.pop(token_key, None)
                self.issued_token_audiences.pop(token_key, None)


def token_response_payload(token: str, expires_in: Any) -> dict[str, Any]:
    return {"token": token, "expires_in": expires_in}
