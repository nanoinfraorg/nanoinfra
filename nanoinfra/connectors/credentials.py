"""Mint one short-lived access token per action, for the scopes one class declared.

This module runs where the refresh token lives, which is the process that holds the secret
store. It is deliberately not imported by the agent side: a connector receives an access
token, never the refresh token, so a compromised connector holds minutes of one scope set
rather than a standing key to an account.

Two rules do the real work, and both are RFC 6749 rather than anything invented here:

- **The refresh exchange asks for a subset** (§6). A connector declares the scopes it needs
  per capability class, and the token is minted for the intersection of that and what the
  credential was granted. So the Docs connector cannot send mail with the shared Google
  credential even though the credential holds `gmail.send` — and a read receives a token that
  cannot write, for minutes.
- **A connector whose declared scopes are not a subset of the credential's is refused when it
  is enabled**, naming both sets. The mismatch is told to a person rather than discovered by
  an action failing at 03:00.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol, cast

import httpx
from loguru import logger

from nanoinfra.connectors.contracts import ConnectorCredentialSpec, ConnectorPlugin
from nanoinfra.security.network import (
    PinnedDNSAsyncTransport,
    httpx_env_proxy_mounts,
    validate_url_target,
)

# Refresh this far before expiry. A token that expires between the mint and the call would
# cost a retry for no reason.
EXPIRY_MARGIN_S = 60.0

# What a token is assumed to last when the provider sends no `expires_in`.
DEFAULT_LIFETIME_S = 3600.0


class CredentialError(Exception):
    """The credential cannot mint a token, and a person has to act.

    Separate from a call failure on purpose: "the calendar said no" and "this connector is no
    longer authorised" need different words, because only the second one has a fix an operator
    can perform.
    """


class SecretResolver(Protocol):
    """Reads one secret's plaintext. `SecretStore.resolve_plaintext` satisfies this."""

    def resolve_plaintext(self, secret_id: str) -> str | None: ...


@dataclass(frozen=True, slots=True)
class ConnectorCredential:
    """One OAuth credential a deployment holds, and what it was granted.

    The connector names this; it never holds it. That is `Server.secret_ref` again, and for the
    same reason: who may resolve a credential is an authority decision, so it lives in config
    where a reviewer sees it, not in a package that could name itself a peer.
    """

    name: str
    client_id: str
    secret_ref: str
    token_url: str
    scopes: tuple[str, ...] = ()
    # The OAuth client secret's own reference. Google issues one for a "web" or "desktop"
    # client and requires it in the refresh exchange.
    client_secret_ref: str = ""

    def granted(self) -> frozenset[str]:
        return frozenset(self.scopes)


def scope_subset(declared: tuple[str, ...], credential: ConnectorCredential) -> tuple[str, ...]:
    """The scopes to ask for, or raise naming both sets.

    Order follows the declaration so the request is stable, which matters only because a
    provider's error messages quote it back.
    """
    granted = credential.granted()
    missing = [scope for scope in declared if scope not in granted]
    if missing:
        raise CredentialError(
            f"credential {credential.name!r} was not granted {sorted(missing)}, so this "
            f"operation cannot run. Granted: {sorted(granted)}."
        )
    return declared


def check_connector_scopes(plugin: ConnectorPlugin, credential: ConnectorCredential) -> None:
    """Refuse a connector the credential cannot serve, at enable time.

    Every class the connector offers is checked, not just the one an operator happens to try
    first, because a connector that reads today and refuses to write at 03:00 is the failure
    this replaces.
    """
    for capability_class in plugin.classes:
        scope_subset(plugin.credential.scopes_for(capability_class), credential)


@dataclass
class _CachedToken:
    value: str
    expires_at: float

    def usable(self, now: float) -> bool:
        return self.value != "" and now < self.expires_at - EXPIRY_MARGIN_S


@dataclass
class RefreshTokenSource:
    """Exchanges a stored refresh token for an access token, per connector and class.

    One cache entry per (connector, class), because the classes ask for different scopes and a
    read must not be handed the write token that happens to be warm.
    """

    credential: ConnectorCredential
    secrets: SecretResolver
    spec: ConnectorCredentialSpec
    _cache: dict[tuple[str, str], _CachedToken] = dataclass_field(
        default_factory=dict[tuple[str, str], _CachedToken]
    )

    def _secret(self, ref: str, what: str) -> str:
        if not ref:
            raise CredentialError(
                f"credential {self.credential.name!r} names no {what}, so no token can be minted"
            )
        value = self.secrets.resolve_plaintext(ref)
        if not value:
            raise CredentialError(
                f"credential {self.credential.name!r} names {what} {ref!r} and the secret store "
                "holds no value for it"
            )
        return value

    def _client(self) -> httpx.AsyncClient:
        mounts = httpx_env_proxy_mounts()
        kwargs: dict[str, Any] = {"transport": PinnedDNSAsyncTransport()}
        if mounts:
            kwargs["mounts"] = mounts
        return httpx.AsyncClient(follow_redirects=False, **kwargs)

    async def _exchange(self, scopes: tuple[str, ...]) -> _CachedToken:
        token_url = self.credential.token_url or self.spec.token_url
        ok, error = validate_url_target(token_url)
        if not ok:
            raise CredentialError(f"refusing token endpoint {token_url}: {error}")

        form = {
            "grant_type": "refresh_token",
            "refresh_token": self._secret(self.credential.secret_ref, "refresh token"),
            "client_id": self.credential.client_id,
        }
        if self.credential.client_secret_ref:
            form["client_secret"] = self._secret(
                self.credential.client_secret_ref, "client secret"
            )
        if scopes:
            # RFC 6749 §6: the request may narrow the scope, never widen it.
            form["scope"] = " ".join(scopes)

        async with self._client() as client:
            response = await client.post(token_url, data=form, timeout=30.0)

        if response.status_code >= 400:
            # The body of a failed token exchange carries `error` and `error_description` and no
            # secret, so quoting it is what turns "it stopped working" into "the grant was
            # revoked".
            detail = response.text[:400]
            raise CredentialError(
                f"the token exchange for {self.credential.name!r} failed with HTTP "
                f"{response.status_code}: {detail}. The connector needs re-authorising."
            )
        try:
            payload = cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise CredentialError(f"the token endpoint returned no JSON: {exc}") from exc

        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise CredentialError("the token endpoint returned no access_token")
        lifetime = payload.get("expires_in")
        seconds = float(lifetime) if isinstance(lifetime, (int, float)) else DEFAULT_LIFETIME_S
        logger.debug(
            "minted an access token for {} covering {} scope(s), good for {}s",
            self.credential.name,
            len(scopes),
            int(seconds),
        )
        return _CachedToken(value=access, expires_at=time.monotonic() + seconds)

    async def access_token(
        self, connector: str, capability_class: str, *, force_refresh: bool = False
    ) -> str:
        """The token for one class of one connector. Satisfies `engine.TokenSource`."""
        key = (connector, capability_class)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and not force_refresh and cached.usable(now):
            return cached.value
        scopes = scope_subset(self.spec.scopes_for(capability_class), self.credential)
        minted = await self._exchange(scopes)
        self._cache[key] = minted
        return minted.value


__all__ = [
    "DEFAULT_LIFETIME_S",
    "EXPIRY_MARGIN_S",
    "ConnectorCredential",
    "CredentialError",
    "RefreshTokenSource",
    "SecretResolver",
    "check_connector_scopes",
    "scope_subset",
]
