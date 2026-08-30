"""Exchange a stored refresh token for one short-lived access token.

**This module lives in the executor tree because it calls ``resolve_plaintext``.**
``tests/agent/test_redaction_isolation.py`` walks every module the agent process can load and
fails on that name, which is exactly the right answer here: a refresh token in the gateway
would put a standing key to somebody's account in the process the model steers. The token is
minted here, the call that spends it is made here, and the agent process holds neither.

The declarations this works from -- what a credential is, and which scopes each class may ask
for -- live in ``nanoinfra/connectors/credentials.py``, which the agent may load because it
reads nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Protocol, cast

import httpx
from loguru import logger

from nanoinfra.connectors.contracts import ConnectorCredentialSpec
from nanoinfra.connectors.credentials import (
    ConnectorCredential,
    CredentialError,
    scope_subset,
)
from nanoinfra.connectors.setup import ActiveConnector
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


class SecretResolver(Protocol):
    """Reads one secret's plaintext. ``SecretStore`` satisfies this."""

    def resolve_plaintext(self, secret_id: str) -> str | None: ...


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
        try:
            value = self.secrets.resolve_plaintext(ref)
        except Exception as exc:
            # A store this process may not read is not an absent secret, and saying "no value"
            # would send an operator to re-authorise a credential that is fine.
            raise CredentialError(
                f"the secret store would not answer for {ref!r}: {exc}"
            ) from exc
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
        """The token for one class of one connector. Satisfies ``engine.TokenSource``."""
        key = (connector, capability_class)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and not force_refresh and cached.usable(now):
            return cached.value
        scopes = scope_subset(self.spec.scopes_for(capability_class), self.credential)
        minted = await self._exchange(scopes)
        self._cache[key] = minted
        return minted.value


def token_source_for(active: ActiveConnector, secrets: SecretResolver) -> RefreshTokenSource:
    """The token source for one active connector, in the process that may read the secret."""
    return RefreshTokenSource(
        credential=active.credential,
        secrets=secrets,
        spec=active.plugin.credential,
    )


__all__ = [
    "DEFAULT_LIFETIME_S",
    "EXPIRY_MARGIN_S",
    "RefreshTokenSource",
    "SecretResolver",
    "token_source_for",
]
