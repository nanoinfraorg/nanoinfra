"""Config for data connectors -- the operator's half of the contract.

A connector package declares what it *can* do. This file is where a deployment says what it
*may* do, and the split matters: a package that shipped from a marketplace declaring "this
call is a read" is self-certification, so three of the four keys here are ceilings rather
than settings.

- ``credentials`` holds the OAuth credentials the deployment owns, each naming secrets by
  reference. A connector never holds one; it names one, exactly as ``Server.secret_ref``
  does, because who may resolve a credential is an authority decision.
- ``connectors.<name>.credential`` **is** the grant. There is no second allow-list to keep in
  agreement with it: a connector resolves the credential config names for it and nothing else,
  so one OAuth flow can serve four Google connectors without a package naming its own peers.
- ``connectors.<name>.maxClass`` caps the classes a package may offer, whatever its manifest
  says.
- ``connectors.<name>.enabledOperations`` caps which operations reach the model, the same
  choice ``mcpServers.<name>.enabledTools`` already offers, and for the same reason: a
  deployment that only reads a calendar should put two tools in the context window.

``extra="forbid"`` on every model, for the reason ``config/gates.py`` gives: under pydantic's
default a mistyped ``enabledOperatons`` becomes an absent restriction, and an absent
restriction here widens what the model can reach.
"""

from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from nanoinfra.config_base import Base

_FORBID_EXTRA = ConfigDict(extra="forbid", populate_by_name=True)

# The classes a connector operation may declare. `read` and `mutate.remote` are the two a
# remote data call can be; the other three describe local writes and decryptions, which a
# connector does not perform.
ConnectorClass = Literal["read", "mutate.remote"]


class ConnectorCredentialConfig(Base):
    """One OAuth credential the deployment holds.

    Only references live here. ``secretRef`` names the refresh token in the secret store and
    ``clientSecretRef`` names the OAuth client secret; neither value is ever written to
    config, because a config file has more readers than a secret store does.

    ``scopes`` is what the consent screen actually granted. It is not a wish: the refresh
    exchange asks for the intersection of this and what the connector declared, so a scope
    listed here and never granted fails at the exchange with the provider's own words.
    """

    model_config = _FORBID_EXTRA

    kind: Literal["oauth2"] = "oauth2"
    client_id: str = ""
    # The refresh token, by reference.
    secret_ref: str = ""
    # The OAuth client secret, by reference. Google requires it in the refresh exchange for a
    # web or desktop client.
    client_secret_ref: str = ""
    # Left empty, the connector's own manifest supplies it. An operator sets this only for a
    # provider whose endpoint differs from the package's default.
    token_url: str = ""
    scopes: list[str] = Field(default_factory=list)
    # The hosts a package holding this credential may address (#195, part 0). Empty means the
    # package's own manifest decides, which is what a first-party connector reviewed in this
    # repository gets. A marketplace package must be pinned here, because otherwise a manifest
    # declaring `baseUrl: https://evil.example` and Google scopes receives a live Google token --
    # and a confined host process would forward it just as obediently as the executor.
    allowed_hosts: list[str] = Field(default_factory=list)


class ConnectorConfig(Base):
    """What one connector may do in this deployment.

    ``settings`` holds the connector's own fields, validated by its own package against the
    ``ConnectorSetupSpec`` in its manifest -- the same shape a channel's settings take. A
    secret-typed field holds a reference here too, never a value.
    """

    model_config = _FORBID_EXTRA

    credential: str = ""
    # ``None`` means every operation the manifest declares. A list means exactly those.
    enabled_operations: list[str] | None = None
    # The ceiling. A declared class above it is refused when the connector is enabled, with
    # the mismatch named, so a third-party connector capped at `read` offers no writes
    # however its manifest is written.
    max_class: ConnectorClass | None = None
    # When this connector's tool schemas reach the prompt (#204). `always` is what every
    # deployment did before the field existed. `mention` sends one advertised line instead, and
    # the schemas only for a turn that names the connector -- either `@<name>` or a `@<kind>:<id>`
    # object of one of its kinds, because pinning a calendar names the calendar connector.
    #
    # Not the same as removing it from `active`: a `mention` connector keeps its credential, its
    # grants and its operation list, and is one word away.
    attach: Literal["always", "mention"] = "always"
    settings: dict[str, str] = Field(default_factory=dict)


class ConnectorRuntimeConfig(Base):
    """The two dicts a connector needs, together, so one object plumbs through.

    They are separate keys in ``config.json`` because they answer different questions -- what
    the deployment holds, and what each connector may do with it -- and they are one object
    here because every consumer needs both to answer either.
    """

    model_config = _FORBID_EXTRA

    credentials: dict[str, ConnectorCredentialConfig] = Field(default_factory=dict)
    connectors: dict[str, ConnectorConfig] = Field(default_factory=dict)
    # The connectors an operator activated, in the vocabulary of the package directory names.
    # Installing a connector writes a package; activating it gives that package a token and a
    # capability class, so activation is declared here and never toggled from a chat turn --
    # the same rule ``tools.agentPlugins`` states.
    active: list[str] = Field(default_factory=list)


__all__ = [
    "ConnectorClass",
    "ConnectorConfig",
    "ConnectorCredentialConfig",
    "ConnectorRuntimeConfig",
]
