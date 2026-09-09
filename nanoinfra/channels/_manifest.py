"""Small constructors shared by declarative channel manifests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nanoinfra.channels.contracts import ChannelFieldSpec, FieldKind, SetupRequirement

GROUP_POLICIES = frozenset({"mention", "open", "allowlist"})
DIRECT_GROUP_POLICIES = frozenset({"mention", "open"})


def field(
    kind: FieldKind = "string",
    *,
    choices: Iterable[str] = (),
    default: Any = None,
    writable: bool = True,
    snapshot: bool = True,
) -> ChannelFieldSpec:
    return ChannelFieldSpec(
        kind=kind,
        choices=frozenset(choices),
        default=default,
        writable=writable,
        snapshot=snapshot,
    )


def agent_field() -> ChannelFieldSpec:
    """The channel's agent binding (``channels.<name>.agent``).

    Declared per manifest rather than added centrally, because a channel package owns its own
    setup contract -- `tests/channels/test_channel_setup.py` asserts that
    `channel_setup_spec(name) is plugin.setup`, and refuses a central spec registry outright. This
    constructor is the shared part: the kind, and the reason it is not an enum.

    ``snapshot`` stays on, so the browser receives the value that is set and the picker shows it.
    """
    return field("agent")


def required(name: str) -> SetupRequirement:
    return SetupRequirement.field(name)


def required_fields(*names: str) -> tuple[SetupRequirement, ...]:
    return tuple(required(name) for name in names)


def one_of(*alternatives: tuple[str, ...]) -> SetupRequirement:
    return SetupRequirement.one_of(*alternatives)
