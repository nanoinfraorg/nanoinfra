"""Policy config for the capability gates -- nanoinfraorg/nanoinfra#7.

Config is the only source of authority in this design. This module therefore reads no
reachability list. ``allowFrom`` exists so teammates can reach the bot, and the pairing
store writes approved senders at runtime from chat. An injected instruction attacks a
runtime-mutable list first. The approver set, the standing grants, and the scope denials
live here, and git review covers them.

Two schema decisions carry weight:

``extra="forbid"`` on every model. ``Base`` sets no ``extra`` policy, so pydantic ignores
an unknown key by default. Under that default a mistyped ``allowCommnads`` becomes an
empty allowlist, and a mistyped nested block becomes absent policy. The root cannot use
this rule, because ``Config`` accepts extras today and a change there breaks live configs.
A mistyped ``gates`` key therefore still falls back to these defaults, which is why #8
also echoes the effective policy at start.

Restrictive defaults. An absent block denies every unattended remote action and every
unattended secret resolution. An absent block must widen nothing.
"""

from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, ConfigDict, Field

from nanoinfra.config_base import Base

# What the gate does with one class at one scope. `grant` means "allow only when a
# standing grant matches", which is the sole unattended allow path.
Decision = Literal["allow", "approve", "grant", "deny"]

_FORBID_EXTRA = ConfigDict(extra="forbid", populate_by_name=True)


class ScopePolicy(Base):
    """One decision per blast-radius tier. See #4 for how scope is resolved.

    ``all`` accepts only ``deny``. This design has no runtime path to unbounded scope, so
    the schema refuses to express one rather than negotiate at evaluation time.
    """

    model_config = _FORBID_EXTRA

    host: Decision = "deny"
    group: Decision = "deny"
    all: Literal["deny"] = "deny"


class ContextPolicy(Base):
    """Policy for one execution context. #5 decides which context a turn runs in.

    Field names carry explicit aliases because the JSON keys are the capability class
    strings from nanoinfra/agent/tools/capabilities.py. A dot is not legal in a Python
    identifier, and ``Base``'s camelCase generator would produce ``mutateRemote``, which
    would not match the class vocabulary.
    """

    model_config = _FORBID_EXTRA

    mutate_remote: ScopePolicy = Field(
        default_factory=ScopePolicy,
        validation_alias=AliasChoices("mutate.remote", "mutate_remote"),
        serialization_alias="mutate.remote",
    )
    mutate_inventory: Literal["allow", "deny"] = Field(
        default="deny",
        validation_alias=AliasChoices("mutate.inventory", "mutate_inventory"),
        serialization_alias="mutate.inventory",
    )
    credential_access: Literal["approve", "deny"] = Field(
        default="deny",
        validation_alias=AliasChoices("credential.access", "credential_access"),
        serialization_alias="credential.access",
    )


def _default_interactive() -> ContextPolicy:
    """An operator is present, so an unusual action can ask. Unbounded scope still cannot."""
    return ContextPolicy(
        mutate_remote=ScopePolicy(host="approve", group="approve"),
        mutate_inventory="allow",
        credential_access="approve",
    )


def _default_unattended() -> ContextPolicy:
    """Nobody is present. A gate that prompts here becomes a hang or a rubber stamp.

    Both outcomes are worse than an explicit narrow grant, so the default refuses and the
    operator declares what an automation may do.
    """
    return ContextPolicy()


class StandingGrant(Base):
    """One pre-declared permission for a recurring action.

    A grant carries no capability class. It permits ``mutate.remote`` and nothing else, so
    a grant cannot permit an inventory write and cannot widen itself (#23).

    ``commands`` holds exact resolved command strings. It is not a pattern language.
    Patterns reintroduce the weakness that makes the existing dangerous-pattern detection
    useless as a boundary. A grant matches, or a grant does not match.

    ``hosts`` names inventory records. #24 compares the *resolved* target instead of the
    label, because an inventory write can repoint a label at another address.
    """

    model_config = _FORBID_EXTRA

    id: str | None = None  # named in the audit record (#16) when this grant matches
    contexts: list[Literal["interactive", "unattended"]] = Field(default_factory=lambda: ["unattended"])
    hosts: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)


class Approver(Base):
    """One identity that may approve an action at runtime.

    Membership in a channel's ``allowFrom``, or in the pairing store, grants nothing. This
    list is the only source of approval authority, and no chat message may add to it.
    """

    model_config = _FORBID_EXTRA

    channel: str
    sender: str


class AuditConfig(Base):
    """Retention and detail for the append-only record (#16).

    ``record_command_text`` defaults to false. Resolved commands routinely embed secrets,
    so an audit log that captures them becomes a second secret store. Full text is an
    explicit opt-in.
    """

    model_config = _FORBID_EXTRA

    retention_days: int = 90
    record_command_text: bool = False


class GatesConfig(Base):
    """The whole policy surface. #8 enforces the unattended half first."""

    model_config = _FORBID_EXTRA

    approvers: list[Approver] = Field(default_factory=list)
    interactive: ContextPolicy = Field(default_factory=_default_interactive)
    unattended: ContextPolicy = Field(default_factory=_default_unattended)
    standing_grants: list[StandingGrant] = Field(default_factory=list)
    audit: AuditConfig = Field(default_factory=AuditConfig)


__all__ = [
    "Approver",
    "AuditConfig",
    "ContextPolicy",
    "Decision",
    "GatesConfig",
    "ScopePolicy",
    "StandingGrant",
]
