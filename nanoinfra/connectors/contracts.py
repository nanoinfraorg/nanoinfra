"""Typed metadata for self-contained connector packages.

A connector integrates one data source — Calendar, Gmail, Docs — and it is shaped
like a channel on purpose: a package with a manifest, typed setup fields with its
own validator, dependencies installed when it is enabled, and its own WebUI
contribution. `nanoinfra/channels/plugin.py` is the sibling to read.

The one field a channel does not need is ``operations``, and it is the reason this
exists at all. Each operation names its **capability class**, so the gate answers
about `calendar_list_events` and `calendar_create_event` separately: a read may be
allowed while a write still asks. An MCP server cannot say that — its tools
declare nothing, `capability_class_of()` resolves them to the fail-closed
`mutate.remote`, and one standing grant that unblocks a read unblocks every write
that server exposes.

The field system is the channels' own (`ChannelFieldSpec`, `SetupRequirement`).
Two field systems would mean two renderers in the WebUI and two validators to keep
in agreement, so this imports rather than repeats. The names say "channel" because
that is where they were written; they describe a typed setting and nothing about
messages.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal, cast

from nanoinfra.agent.tools.capabilities import CAPABILITY_CLASSES
from nanoinfra.channels.contracts import ChannelFieldSpec, SetupRequirement

# The verbs an operation may use. A connector describes requests; the engine makes
# them, so the method is data and not code.
HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]

# Methods that may carry a read class. A `POST` declared as a read is the failure
# this whole design exists to prevent, so it is refused at load rather than
# reviewed by hope.
_READ_METHODS = frozenset({"GET"})

_NAME = re.compile(r"[a-z][a-z0-9_]*")
_CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9-]*")


@dataclass(frozen=True, slots=True)
class ConnectorOperation:
    """One call a connector may make, and the class the gate answers about.

    ``returns`` is the projection. An API response carries conference data,
    reminders, response status and a dozen link fields; handing that to a model
    burns the context window and teaches it nothing. Naming the fields also caps
    what a call that *is* allowed can carry out.
    """

    name: str
    capability_class: str
    method: HttpMethod
    path: str
    returns: tuple[str, ...] = ()
    # The response key that holds the list, for an operation that returns many things.
    # Naming it beats guessing: the projection applies to each element and the paging key
    # survives, so a model that needs the next page can ask for it.
    collection: str = ""
    description: str = ""
    parameters: dict[str, Any] = dataclass_field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.name) is None:
            raise ValueError(
                f"operation name {self.name!r} must start with a letter and hold only "
                "lowercase letters, digits or underscores"
            )
        if self.capability_class not in CAPABILITY_CLASSES:
            raise ValueError(
                f"operation {self.name!r} declares capability class "
                f"{self.capability_class!r}, which is not one the gate knows"
            )
        if not self.path.startswith("/"):
            raise ValueError(f"operation {self.name!r} path must start with '/'")
        reads = self.capability_class.startswith("read")
        if reads and self.method not in _READ_METHODS:
            raise ValueError(
                f"operation {self.name!r} is classified {self.capability_class!r} and uses "
                f"{self.method}. A read class on a writing method is the mismatch this "
                "refuses: classify it as a mutate, or use GET."
            )

    @property
    def is_read(self) -> bool:
        return self.capability_class.startswith("read")


def operation(
    name: str,
    capability_class: str,
    method: HttpMethod,
    path: str,
    *,
    returns: Iterable[str] = (),
    collection: str = "",
    description: str = "",
    parameters: dict[str, Any] | None = None,
) -> ConnectorOperation:
    """Declare one operation. Used by a connector's ``manifest.py``."""
    return ConnectorOperation(
        name=name,
        capability_class=capability_class,
        method=method,
        path=path,
        returns=tuple(returns),
        collection=collection,
        description=description,
        parameters=dict(parameters or {}),
    )


@dataclass(frozen=True, slots=True)
class ConnectorSetupSpec:
    """The settings one connector owns, validated by its own package.

    Independent per connector: `connectors.gmail` holds Gmail's fields and
    `connectors.google-docs` holds its own. Two Google connectors are two
    configurations and two enable states, not one "Google" blob.
    """

    fields: dict[str, ChannelFieldSpec] = dataclass_field(
        default_factory=dict[str, ChannelFieldSpec]
    )
    required: tuple[SetupRequirement, ...] = ()
    official_url: str = ""
    validator: Callable[..., Any] | None = None


@dataclass(frozen=True, slots=True)
class ConnectorCredentialSpec:
    """What kind of credential this connector resolves, and the scopes it needs.

    The connector names a credential; it does not hold one. `Server.secret_ref` is
    the same shape: the executor resolves the reference per action, and the gate's
    refusal text can quote it. Which credential a connector may resolve is decided
    in config, because who may use a token is an authority decision and a package
    naming its own peers would be self-certification.
    """

    kind: Literal["oauth2", "api_key", "none"] = "none"
    # The scopes each capability class needs. The executor mints an access token for
    # the intersection of what the credential was granted and what the connector
    # declared, so a connector cannot use a scope it never asked for.
    scopes: dict[str, tuple[str, ...]] = dataclass_field(
        default_factory=dict[str, tuple[str, ...]]
    )
    token_url: str = ""

    def scopes_for(self, capability_class: str) -> tuple[str, ...]:
        return self.scopes.get(capability_class, ())

    def declared_scopes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for scopes in self.scopes.values():
            for scope in scopes:
                if scope not in seen:
                    seen.append(scope)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class ConnectorMentionSpec:
    """One kind of object of this connector that a person may pin with a mention.

    A mention is not for attaching content. It **pins identity**, so a task does not begin with
    a search -- and the value is in an automation, where a fuzzy match on a name is re-done on
    every run, so a rename or a second calendar silently changes what the 03:00 run touches.

    ``argument`` is the field that makes a pinned id useful rather than decorative: it names the
    operation argument the id fills, so the runtime context can say *pass ``calendarId=<id>``*
    instead of leaving the model to guess which parameter an id belongs to.
    """

    #: The mention prefix a person types: ``@calendar:``.
    kind: str
    #: The read operation that lists these objects. It must be one of this connector's own.
    operation: str
    #: Where the id and the label live in the projected result.
    id_field: str = "id"
    label_field: str = "summary"
    #: Extra projected fields worth showing in the picker and the context block.
    detail_fields: tuple[str, ...] = ()
    #: The operation argument this id fills.
    argument: str = ""

    def __post_init__(self) -> None:
        if _NAME.fullmatch(self.kind) is None:
            raise ValueError(
                f"mention kind {self.kind!r} must start with a letter and hold only lowercase "
                "letters, digits or underscores"
            )
        if not self.operation:
            raise ValueError(f"mention kind {self.kind!r} names no listing operation")


@dataclass(frozen=True, slots=True)
class ConnectorPlugin:
    """Dependency-free manifest for one connector package.

    ``base_url`` plus each operation's ``path`` is the whole request surface, which
    is why no SDK appears here: the engine is `httpx`, already a base dependency,
    and an explicit method and path are what make a declared capability class
    checkable. A discovery-based client would hide both.
    """

    name: str
    display_name: str
    base_url: str
    operations: tuple[ConnectorOperation, ...]
    credential: ConnectorCredentialSpec = ConnectorCredentialSpec()
    setup: ConnectorSetupSpec | None = None
    dependencies: tuple[str, ...] = ()
    webui: str | None = None
    skill: str | None = None
    description: str = ""
    #: The objects of this connector a person may pin with a mention. Empty means none, and a
    #: connector with none is complete -- pinning is worth having where an id is stable and a
    #: name is not.
    mentions: tuple[ConnectorMentionSpec, ...] = ()

    def __post_init__(self) -> None:
        if _CONNECTOR_NAME.fullmatch(self.name) is None:
            raise ValueError(
                "connector name must start with a letter and hold only lowercase letters, "
                "digits or hyphens"
            )
        if not self.base_url.startswith("https://"):
            raise ValueError(
                f"connector {self.name!r} base_url must be https: a token travels on it"
            )
        if not self.operations:
            raise ValueError(f"connector {self.name!r} declares no operations")
        names = [op.name for op in self.operations]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise ValueError(f"connector {self.name!r} declares {sorted(duplicates)} twice")
        if self.setup is not None and not isinstance(
            cast(object, self.setup), ConnectorSetupSpec
        ):
            raise TypeError("connector setup must be a ConnectorSetupSpec or None")
        for mention in self.mentions:
            listing = self.operation(mention.operation)
            if listing is None:
                raise ValueError(
                    f"connector {self.name!r} declares mention kind {mention.kind!r} listed by "
                    f"{mention.operation!r}, which it does not offer"
                )
            if not listing.is_read:
                # Listing what may be pinned must not be able to change anything: the picker
                # calls this operation, and a picker that wrote would be a write nobody asked
                # for.
                raise ValueError(
                    f"connector {self.name!r} lists mention kind {mention.kind!r} with "
                    f"{mention.operation!r}, which is {listing.capability_class!r} rather than a "
                    "read"
                )

    @property
    def classes(self) -> tuple[str, ...]:
        """The capability classes this connector's operations fall under, in order."""
        seen: list[str] = []
        for op in self.operations:
            if op.capability_class not in seen:
                seen.append(op.capability_class)
        return tuple(seen)

    def operation(self, name: str) -> ConnectorOperation | None:
        for op in self.operations:
            if op.name == name:
                return op
        return None

    def tool_name(self, op: ConnectorOperation) -> str:
        """The name the model calls: ``<connector>_<operation>``, hyphens flattened."""
        return f"{self.name.replace('-', '_')}_{op.name}"


__all__ = [
    "ConnectorCredentialSpec",
    "ConnectorMentionSpec",
    "ConnectorOperation",
    "ConnectorPlugin",
    "ConnectorSetupSpec",
    "HttpMethod",
    "operation",
]
