"""The confined process that makes a marketplace connector's request (#195, part 4).

Not because the package format executes third-party code -- it does not, `connector.json` is data
and `nanoinfra/connectors/package.py` refuses an archive holding anything importable. For the two
reasons that survive that:

1. **A first-party manifest is reviewed in this repository and a marketplace one is not.** The
   executor holds the credential store, the plaintext keys and the audit log. Performing a
   stranger's HTTPS request from that process is a larger blast radius than the request needs, and
   moving it costs one socket.
2. **It is the process a runtime hook would need.** Signing, cursor pagination and non-standard auth
   are the reasons a package will eventually want to ship code, and building the boundary now makes
   that a new request kind rather than a migration of the credential store.

Worth stating plainly, because the issue's own framing implies otherwise: this process is *not* what
stops a hostile package. A manifest declares its `baseUrl`, and Landlock does not stop an outbound
HTTPS call -- which is the one thing this host exists to do. What refuses a package that would send
a live token to `evil.example` is `ConnectorCredentialSpec.allowed_hosts`, checked at activation.
"""

from __future__ import annotations

from nanoinfra.gates.connector_host.protocol import (
    ConnectorHostRequest,
    ConnectorHostResponse,
    ProtocolError,
)

__all__ = ["ConnectorHostRequest", "ConnectorHostResponse", "ProtocolError"]
