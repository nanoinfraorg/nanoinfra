"""What a credential is, and which scopes each capability class may ask for.

Declarations only. The exchange that turns a refresh token into an access token lives in
``nanoinfra/gates/executor/connector_credentials.py``, because it calls ``resolve_plaintext``
and ``tests/agent/test_redaction_isolation.py`` refuses that name in every module the agent
process can load. That split is the point: a refresh token in the gateway would be a standing
key to somebody's account sitting in the process the model steers.

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

from dataclasses import dataclass
from urllib.parse import urlsplit

from nanoinfra.connectors.contracts import ConnectorPlugin


class CredentialError(Exception):
    """The credential cannot serve this connector, and a person has to act.

    Separate from a call failure on purpose: "the calendar said no" and "this connector is no
    longer authorised" need different words, because only the second one has a fix an operator
    can perform.
    """


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
    #: The hosts a package holding this credential may address (#195, part 0). Empty means the
    #: manifest decides, which is what a reviewed first-party package gets.
    allowed_hosts: tuple[str, ...] = ()

    def granted(self) -> frozenset[str]:
        return frozenset(self.scopes)

    def permits_host(self, host: str) -> bool:
        """Exact match, or unconstrained when no hosts are named."""
        if not self.allowed_hosts:
            return True
        return host.lower() in {allowed.lower() for allowed in self.allowed_hosts}


def check_connector_hosts(plugin: ConnectorPlugin, credential: ConnectorCredential) -> None:
    """Refuse a package that would send this credential's token somewhere it may not go.

    Checked at activation rather than per call, because the manifest's `base_url` is fixed at load
    and an operator reading a refusal wants it before the first request rather than during one.

    Both sets are named in the refusal, the same way the `maxClass` mismatch names both halves: a
    message that says only "refused" makes the operator guess which side to change, and the answer
    is almost always the config -- because the package declaring its own reach is the thing this
    check exists not to trust.
    """
    declared_hosts = {urlsplit(plugin.base_url).hostname or ""}
    for operation in plugin.operations:
        # An operation path is relative by contract, but a manifest that got one absolute would
        # otherwise route a token past this check entirely.
        if "://" in operation.path:
            declared_hosts.add(urlsplit(operation.path).hostname or "")
    for host in sorted(declared_hosts):
        if not host:
            raise CredentialError(
                f"connector {plugin.name!r} declares a URL with no host, which cannot be checked "
                f"against credentials.{credential.name}.allowedHosts."
            )
        if not credential.permits_host(host):
            raise CredentialError(
                f"connector {plugin.name!r} would address {host!r}, and "
                f"credentials.{credential.name}.allowedHosts holds "
                f"{sorted(credential.allowed_hosts)}. A package declares where it sends a token "
                f"and config decides whether it may: add the host there, or install a package "
                f"that stays within it."
            )
    manifest_hosts = {host.lower() for host in plugin.credential.allowed_hosts}
    if manifest_hosts and not manifest_hosts >= {host.lower() for host in declared_hosts}:
        raise CredentialError(
            f"connector {plugin.name!r} declares allowedHosts {sorted(manifest_hosts)} and then "
            f"addresses {sorted(declared_hosts)}. A package that contradicts its own declaration "
            f"is refused rather than reconciled."
        )


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


__all__ = [
    "ConnectorCredential",
    "CredentialError",
    "check_connector_scopes",
    "scope_subset",
]
