"""Who may enter, and who the gateway says they are -- nanoinfraorg/nanoinfra#62.

**Two lists, two jobs.** The proxy's own allowlist decides who reaches the agent at all.
``gates.approvers`` decides whose approval counts. They are not the same list and they do not
protect the same thing.

That matters more here than it looks. In trusted-proxy mode the assertion alone authorizes the
WebSocket handshake and the REST routes, so whoever is admitted gets a chat session with the
agent, which is ``read`` and ``mutate.local`` in the ``interactive`` context. With a public
identity provider that is a real exposure: any account that completes the flow with the
deployment's client id holds a token whose signature, issuer and audience all check out.
Verification is doing its job correctly and the person is still a stranger. They are not in
``gates.approvers``, so they cannot approve a remote action, and they can talk to the agent.

So a deployment that configures ``gates.approvers`` and leaves the proxy open has an open
agent, and the approver list gives no warning, because it was never the list for that job.
**The gateway therefore does not rely on the operator's proxy configuration for this.** The
``jwt`` block declares who may enter, the schema refuses a block that names nobody, and
``allowAnyVerifiedIdentity`` is the only way to open it.

Five things live here:

* ``admit_identity`` is the access decision, and it is pure. No clock, no key, no socket.
* ``named_identity`` is the one rule about the identity itself: this gateway names it whole or
  it names nobody (#63). Both assertion formats end there, so one answer holds for both.
* ``TrustedProxyAuthenticator`` is the one seam the gateway holds. It reads the peer address,
  the header, the signature and the access rules, and it answers with an identity or with
  nothing. A failure is never a fall back to the anonymous ``webui`` actor, because a forged
  token would then buy the privileges of the shared token, which is a downgrade attack.
* ``describe_trusted_proxy_posture`` is the startup echo. A posture an operator can forget
  about is a posture that surprises them later.
* ``identity_panel_payload`` is the same posture for a screen (#85). It carries a posture kind
  and facts, and never a sentence, because the WebUI carries ten locales and no server text
  reaches nine of them in the right language.

``trusted_proxy_posture_kind`` names the posture, and the two answers above both read it. So the
startup log and the gate panel cannot disagree about one deployment.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from loguru import logger as _default_log

from nanoinfra.webui.assertion_jwks import JwksSource
from nanoinfra.webui.assertion_jwt import (
    AssertionRefusal,
    AssertionRefusedError,
    read_key_id,
    verify_assertion,
)
from nanoinfra.webui.http_utils import MAX_IDENTITY_CHARS, trusted_proxy_peer_assertion
from nanoinfra.webui.identity_workspaces import identity_workspace_key
from nanoinfra.webui.latch_api import PATH_ACTOR, operator_actor

if TYPE_CHECKING:
    from nanoinfra.channels.websocket.runtime import TrustedProxyAuthConfig

# How much of an attacker-chosen value reaches a log line. A key id and a claim value both come
# from a token this gateway has not admitted, so both are truncated and quoted.
_MAX_LOGGED_CHARS = 128

# The posture of a deployment, in four values. ``trusted_proxy_posture_kind`` answers one of
# them, and both the startup line and the panel payload read that answer.
PostureKind = Literal["no_proxy", "verified", "any_verified", "plain"]

#: No ``trustedProxyAuth`` block. The shared token authorizes, and the actor is the path name.
POSTURE_NO_PROXY: PostureKind = "no_proxy"
#: A ``jwt`` block whose identity list or required claims name who may enter.
POSTURE_VERIFIED: PostureKind = "verified"
#: A ``jwt`` block with ``allowAnyVerifiedIdentity`` set.
POSTURE_ANY_VERIFIED: PostureKind = "any_verified"
#: A ``plain`` block. The header is read and never verified.
POSTURE_PLAIN: PostureKind = "plain"


def _short(value: object) -> str:
    """Quote and shorten a value for a log line.

    ``repr`` is the point rather than the length: a claim value that carried a newline could
    otherwise forge a second log line, and an operator reading the log would believe it.
    """
    text = repr(value)
    return text if len(text) <= _MAX_LOGGED_CHARS else f"{text[:_MAX_LOGGED_CHARS]}..."


@dataclass(frozen=True)
class AssertionPosture:
    """What the gateway trusts on this path, for the startup echo.

    ``warn`` is a field rather than a log call, so the text is testable without a logger and
    one caller decides the level.
    """

    warn: bool
    message: str


@dataclass(frozen=True, slots=True)
class AdmittedIdentity:
    """What one verified assertion proves.

    ``name`` is what ``gates.approvers`` compares and what a record shows, and it
    comes from ``identityClaim``. ``key`` is what per-identity storage is filed
    under, and it comes from the issuer plus ``workspaceKeyClaim``. They are two
    values because they answer to two different requirements: a name has to stay
    readable for a reviewer, and a key has to stay stable when the name changes.

    An empty ``key`` is not a failure. A ``plain`` assertion carries no claims at
    all, and a token can verify without the claim that keys storage; both are
    admitted, and both land on the shared workspace.
    """

    name: str
    key: str = ""


def admit_identity(
    claims: Mapping[str, Any],
    *,
    identity_claim: str,
    allowed_identities: Sequence[str],
    required_claims: Mapping[str, str],
    allow_any_verified_identity: bool,
) -> str:
    """Return the identity of a verified token, or raise ``AssertionRefusedError``.

    The rules read together:

    * every entry of ``required_claims`` must match exactly, which covers a whole workspace
      domain through ``hd`` or a mapped group claim without naming every person;
    * ``allowed_identities``, when it holds anything, must name the identity;
    * a rule set that names nobody admits nobody, even though the schema refuses one at load.
      Two checks, because a config that reached this code by another route must fail closed.

    The comparison is exact. Two spellings are therefore two identities, and the cost is that a
    provider which emits a different case needs a config that matches. The refusal reports the
    value it read, so an operator sees that in one log line, and no two accounts of one
    provider can collapse into one authority.
    """
    if not (allowed_identities or required_claims or allow_any_verified_identity):
        raise AssertionRefusedError(
            AssertionRefusal.NOT_AN_ADMITTED_IDENTITY,
            "the trustedProxyAuth block names nobody who may enter",
        )
    identity = cast(object, claims.get(identity_claim))
    if not isinstance(identity, str) or not identity.strip():
        raise AssertionRefusedError(
            AssertionRefusal.NO_IDENTITY_CLAIM,
            f"the token carries no usable {identity_claim} claim",
        )
    identity = identity.strip()
    for name, expected in required_claims.items():
        present = cast(object, claims.get(name))
        if present != expected:
            raise AssertionRefusedError(
                AssertionRefusal.CLAIM_DOES_NOT_MATCH,
                f"claim {name} is {_short(present)} and requiredClaims wants {_short(expected)}",
            )
    if allowed_identities and identity not in allowed_identities:
        raise AssertionRefusedError(
            AssertionRefusal.NOT_AN_ADMITTED_IDENTITY,
            f"{identity_claim} {_short(identity)} is not in allowedIdentities",
        )
    return identity


def named_identity(identity: str) -> str:
    """Answer the identity this gateway can name in full, or raise ``AssertionRefusedError``.

    Two values refuse here, and both would otherwise reach a route as the anonymous ``webui``
    actor of a deployment that authenticated a shared token (#63):

    * an identity longer than ``MAX_IDENTITY_CHARS``, which no record can hold whole;
    * an identity of blank space, which proves a peer address and names nobody.

    The length rule is the live one. A cut name is its reason: ``gates.approvers`` compares the
    whole string, so a truncated identity names a person who does not exist, and two identities
    that share one prefix would collapse into one authority. The bound is above the longest
    address RFC 5321 permits, so no legal email address refuses.

    The blank rule is a second lock. ``case_insensitive_header`` already strips what it reads, so
    a header of blank space arrives here as nothing and never reaches this function. The check
    stays for a caller that reads a header some other way, because the answer must fail closed
    rather than depend on a strip two modules away.

    The detail reports the length rather than the value. The value is attacker-chosen, and the
    refusal that names it already logs it once.
    """
    named = identity.strip()
    if not named:
        raise AssertionRefusedError(
            AssertionRefusal.NO_IDENTITY_CLAIM,
            "the assertion holds no identity, so it names nobody",
        )
    if len(named) > MAX_IDENTITY_CHARS:
        raise AssertionRefusedError(
            AssertionRefusal.IDENTITY_TOO_LONG,
            f"the identity is {len(named)} characters and the bound is {MAX_IDENTITY_CHARS}. "
            "A name this gateway cannot record whole is a name it must not record at all.",
        )
    return named


class TrustedProxyAuthenticator:
    """The one seam that turns a request into an identity, for both assertion formats.

    ``plain`` answers the header value, which is the behaviour every deployment had before
    #58: the peer address and a non-empty header are the whole check, and the proxy alone
    decides who reaches the agent. ``jwt`` verifies a signature and applies the access rules.

    An empty answer means "not authenticated on this path". The caller then falls through to
    the token checks it already had, and a request with no token gets the 401 it would have got
    with no header at all. So the client learns that it is not authorized and learns no rule.
    """

    def __init__(
        self,
        config: TrustedProxyAuthConfig,
        *,
        key_source: JwksSource | None,
        clock: Callable[[], float] = time.time,
        log: Any = _default_log,
    ) -> None:
        self._config = config
        self._key_source = key_source
        self._clock = clock
        self._log = log

    async def authenticate(self, connection: Any, headers: Any) -> str:
        """Answer the name this request proves, or an empty string."""
        return (await self.admit(connection, headers)).name

    async def admit(self, connection: Any, headers: Any) -> AdmittedIdentity:
        """Answer what this request proves: a name, and the key storage files it under."""
        assertion = trusted_proxy_peer_assertion(connection, headers, self._config)
        if not assertion:
            return AdmittedIdentity("")
        try:
            resolved = (
                await self._verified_identity(assertion)
                if self._config.assertion_format == "jwt"
                else AdmittedIdentity(assertion)
            )
            # Both formats end here, so one rule decides which identities this gateway can
            # name. The ``plain`` path needs it as much as the ``jwt`` path: a header value is
            # no shorter than a claim, and a name this gateway cut belongs to nobody.
            return AdmittedIdentity(named_identity(resolved.name), resolved.key)
        except AssertionRefusedError as refusal:
            # One line per refusal, and the line never carries the token: a log reaches more
            # accounts than a live credential should. The reason and the value it read are
            # what an operator needs to fix a misconfigured proxy.
            self._log.warning(
                "trusted proxy assertion refused: reason={} detail={}",
                refusal.reason.value,
                refusal.detail,
            )
            return AdmittedIdentity("")

    async def _verified_identity(self, assertion: str) -> AdmittedIdentity:
        if self._key_source is None:
            # A verifier with no key cannot verify. Refusing is the only safe answer.
            raise AssertionRefusedError(
                AssertionRefusal.NO_KEYS_AVAILABLE,
                "no JWKS source is configured for this gateway",
            )
        key_id = read_key_id(assertion)
        key = await self._key_source.key_for(key_id)
        if key is None:
            raise AssertionRefusedError(
                AssertionRefusal.UNKNOWN_KEY_ID,
                f"no signing key for kid {_short(key_id)}",
            )
        verified = verify_assertion(
            assertion,
            public_key=key,
            issuer=self._config.issuer,
            audience=self._config.audience,
            now=self._clock(),
        )
        name = admit_identity(
            verified.claims,
            identity_claim=self._config.identity_claim,
            allowed_identities=self._config.allowed_identities,
            required_claims=self._config.required_claims,
            allow_any_verified_identity=self._config.allow_any_verified_identity,
        )
        # The key is read after admission, so a token this gateway refuses never
        # creates a directory. A missing claim is not a refusal: the person is in,
        # and they share the workspace everyone else shares.
        key_claim = str(getattr(self._config, "workspace_key_claim", "sub") or "")
        subject = cast(object, verified.claims.get(key_claim)) if key_claim else None
        issuer = cast(object, verified.claims.get("iss"))
        key = identity_workspace_key(
            issuer if isinstance(issuer, str) else "",
            subject if isinstance(subject, str) else "",
        )
        return AdmittedIdentity(name, key)


def build_trusted_proxy_authenticator(
    config: Any,
    *,
    log: Any = _default_log,
) -> TrustedProxyAuthenticator | None:
    """Build the authenticator for a channel config, or None when no proxy is configured.

    The key source follows the config: a fetch with a TTL cache for ``jwksUrl``, and the keys
    an operator wrote for ``jwks``. A ``plain`` block needs no key at all.
    """
    from nanoinfra.webui.assertion_jwks import HttpJwksSource, StaticJwksSource

    proxy = cast(Any, getattr(config, "trusted_proxy_auth", None))
    if proxy is None:
        return None
    # Every read below fails closed. A block with no ``assertionFormat`` is read as ``jwt``,
    # because reading it as ``plain`` would trust an unverified header. A ``jwt`` block with no
    # key source gets none, and the authenticator then refuses every assertion. The schema
    # makes both cases impossible, so this is the second lock rather than the first.
    assertion_format = str(cast(object, getattr(proxy, "assertion_format", "jwt")))
    key_source: JwksSource | None = None
    if assertion_format == "jwt":
        static_keys = cast(object, getattr(proxy, "jwks", None))
        url = str(cast(object, getattr(proxy, "jwks_url", "")) or "")
        if static_keys is not None:
            key_source = StaticJwksSource(static_keys)
        elif url:
            key_source = HttpJwksSource(url, log=log)
    return TrustedProxyAuthenticator(
        cast("TrustedProxyAuthConfig", proxy),
        key_source=key_source,
        log=log,
    )


def trusted_proxy_posture_kind(config: TrustedProxyAuthConfig | None) -> PostureKind:
    """Name the posture this config puts the gateway in.

    This is the one place that decides. The startup line and the panel payload both read it, so
    a screen cannot claim a posture the log denies.

    The order of the questions is the rule:

    * a missing block is answered first, because every question below reads a field;
    * ``plain`` comes before every ``jwt`` question, because a block that verifies nothing
      answers no question about verification;
    * ``allowAnyVerifiedIdentity`` comes before the named lists, because it admits every
      identity the provider signs for, whatever those lists hold.
    """
    if config is None:
        return POSTURE_NO_PROXY
    if config.assertion_format != "jwt":
        return POSTURE_PLAIN
    if config.allow_any_verified_identity:
        return POSTURE_ANY_VERIFIED
    return POSTURE_VERIFIED


def describe_trusted_proxy_posture(config: TrustedProxyAuthConfig | None) -> AssertionPosture:
    """Say what this gateway trusts about an identity, in one line, at every start.

    The line names counts rather than identities. A log is shipped elsewhere often enough that
    an address list in it is a leak nobody chose.
    """
    kind = trusted_proxy_posture_kind(config)
    # A missing block and the ``no_proxy`` kind are one case. Both are named here, so every
    # read below needs no guard of its own.
    if config is None or kind == POSTURE_NO_PROXY:
        return AssertionPosture(
            warn=False,
            message=(
                "identity: no trusted proxy is configured, so the WebUI actor stays the path "
                'name "webui" and an approver entry must name that path'
            ),
        )
    if kind == POSTURE_PLAIN:
        return AssertionPosture(
            warn=True,
            message=(
                'identity: trustedProxyAuth assertionFormat is "plain", so the assertion '
                f"header {config.assertion_header} is read and never verified, and the proxy "
                "alone decides who reaches the agent"
            ),
        )
    if kind == POSTURE_ANY_VERIFIED:
        return AssertionPosture(
            warn=True,
            message=(
                "identity: trustedProxyAuth has allowAnyVerifiedIdentity set, so every "
                f"identity that {config.issuer} signs for audience {config.audience} may "
                "reach the agent"
            ),
        )
    return AssertionPosture(
        warn=False,
        message=(
            f"identity: trustedProxyAuth verifies a jwt from {config.issuer}, reads the "
            f"{config.identity_claim} claim, and admits "
            f"{len(config.allowed_identities)} named identities and "
            f"{len(config.required_claims)} required claims"
        ),
    )


def identity_panel_payload(
    config: TrustedProxyAuthConfig | None,
    request: Any,
    *,
    workspace: str = "",
    workspace_personal: bool = False,
) -> dict[str, Any]:
    """Answer the posture and the actor for the gate panel, as facts and never as a sentence.

    **The payload holds the posture, three facts, the actor and one flag. It holds no config
    block.** A client that held ``trustedPeerCidrs`` and a JWKS URL would describe the shape of
    the deployment to every reader of the page, and the panel needs none of it. ``issuer`` and
    ``identityClaim`` belong to a ``jwt`` posture, and the header name belongs to ``plain``. A
    posture that carries neither sends empty strings, so the key set never changes and the panel
    needs no test for a missing field.

    **The actor comes from ``operator_actor``**, which is the function the gate reads. The panel
    tells the operator to write that value in an approver row, and ``gates.approvers`` compares
    the whole string (#66). A second rule for the prefix here would drift, and the drift would
    read as an approval that does not count.

    **The workspace rides here because it is a fact about this caller**, not about the
    deployment: with a verified identity it is the person's own directory, and a panel that
    showed the deployment's would answer a question nobody asked. ``signOutPath`` is the
    proxy's route and not a URL, because the cookie belongs to the proxy in front and the
    gateway can only send the browser there.

    **``assertionMissing`` is the warning this payload exists for.** A configured proxy plus an
    actor of the bare path name means the assertion did not arrive or did not verify. The
    gateway then authenticated the shared token, so every approval on this path names nobody
    while the deployment believes it names somebody. A deployment with no proxy reaches the same
    actor and raises no warning, because there the path name is the true and whole answer.
    """
    kind = trusted_proxy_posture_kind(config)
    verified = kind in (POSTURE_VERIFIED, POSTURE_ANY_VERIFIED)
    actor = operator_actor(request)
    return {
        "posture": kind,
        "issuer": config.issuer if config is not None and verified else "",
        "identityClaim": config.identity_claim if config is not None and verified else "",
        "assertionHeader": (
            config.assertion_header if config is not None and kind == POSTURE_PLAIN else ""
        ),
        "workspaceKeyClaim": (
            config.workspace_key_claim if config is not None and verified else ""
        ),
        "actor": actor,
        "workspace": workspace,
        "workspacePersonal": workspace_personal,
        "signOutPath": str(getattr(config, "sign_out_path", "") or "") if config is not None else "",
        "assertionMissing": kind != POSTURE_NO_PROXY and actor == PATH_ACTOR,
    }


__all__ = [
    "POSTURE_ANY_VERIFIED",
    "POSTURE_NO_PROXY",
    "POSTURE_PLAIN",
    "POSTURE_VERIFIED",
    "AssertionPosture",
    "PostureKind",
    "TrustedProxyAuthenticator",
    "admit_identity",
    "build_trusted_proxy_authenticator",
    "describe_trusted_proxy_posture",
    "identity_panel_payload",
    "named_identity",
    "trusted_proxy_posture_kind",
]
