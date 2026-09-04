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

from datetime import UTC, datetime
from typing import Literal

from pydantic import AliasChoices, ConfigDict, Field, model_validator

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
    # Every decision value, and not two of them. The refusal for this class advises `allow`, and a
    # schema that refused that value made the advice a dead end. `grant` is also a real choice: a
    # deployment may permit a decryption for granted work alone. #39 models all four already.
    credential_access: Decision = Field(
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

    ``connectors`` and ``operations`` are the same idea for a data connector, which has
    neither a host nor a command string. Without them a connector's write is ``mutate.remote``
    in an unattended turn with no grant that could ever match it -- permanently denied, so
    "reply to the on-call thread overnight" is not expressible. They carry exact names for the
    same reason ``commands`` does: a grant matches, or it does not.

    A grant names **one kind of action**. A grant that named both a host and a connector would
    have to mean either "and" or "or", and both readings are wrong: the first makes a grant
    that never matches, the second silently widens one grant into two. The validator refuses it
    instead of choosing.

    ``expires_at`` is an absolute timestamp, and absent means never (#218). Absent has to keep
    that meaning, because every grant written before this field existed omits it. The value is
    absolute rather than a duration for the reader six months later: a file has to say the date
    it stops, not a subtraction from a start nobody recorded. ``note`` is where the writer echoes
    the duration the operator chose, because config.json is JSON and JSON has no comments.

    Nothing prunes an expired grant. The gate stops matching it and the line stays in the file.
    An application that deleted lines from the operator's config would make the file something
    other than the authority, and a stale line an operator can read beats a silent edit.
    """

    model_config = _FORBID_EXTRA

    id: str | None = None  # named in the audit record (#16) when this grant matches
    contexts: list[Literal["interactive", "unattended"]] = Field(default_factory=lambda: ["unattended"])
    hosts: list[str] = Field(default_factory=list)
    commands: list[str] = Field(default_factory=list)
    connectors: list[str] = Field(default_factory=list)
    operations: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None
    # Free text a human reads, and nothing matches on it (#218). "Approve and add" writes the
    # duration and the approval here, so an operator who opens config.json in six months reads
    # why the line exists beside the date it dies.
    note: str | None = None

    @model_validator(mode="after")
    def _utc_expiry(self) -> StandingGrant:
        """Read a timestamp with no offset as UTC.

        Pydantic accepts ``2026-12-01`` and ``2026-12-01T09:00:00`` and leaves both naive, and a
        naive value cannot be compared with an aware ``now`` at all -- the gate raised
        ``TypeError`` instead of deciding. UTC is also the fail-closed reading: an operator who
        meant a local time east of UTC gets a grant that dies earlier than they wrote, never
        later.
        """
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=UTC)
        return self

    def is_expired(self, now: datetime) -> bool:
        """True when this grant no longer covers anything.

        The comparison is wall clock, and the approval wait in ``nanoinfra/gates/pending.py`` is
        monotonic. The two are not inconsistent: that wait is a deadline of seconds inside one
        process, and this one is a date that has to survive a restart and be legible in a file.
        """
        return self.expires_at is not None and now >= self.expires_at

    @model_validator(mode="after")
    def _one_kind_of_action(self) -> StandingGrant:
        names_command = bool(self.hosts or self.commands)
        names_connector = bool(self.connectors or self.operations)
        if names_command and names_connector:
            raise ValueError(
                "a standing grant names hosts and commands, or connectors and operations, "
                "never both: split it into two grants"
            )
        # Half a grant matches nothing, and an operator who wrote one will be waiting for it to
        # work. Say so at load rather than at 03:00.
        if bool(self.connectors) != bool(self.operations):
            raise ValueError(
                "a connector grant needs both 'connectors' and 'operations'; one without the "
                "other matches nothing"
            )
        return self


class Approver(Base):
    """One identity that may approve an action at runtime.

    Membership in a channel's ``allowFrom``, or in the pairing store, grants nothing. This
    list is the only source of approval authority, and no chat message may add to it.

    ``sender`` is one opaque token, and the gate compares the whole string (#47, item 9). Three
    shapes arrive in practice, and the list takes all three with no new syntax:

    - a chat sender id, such as a Telegram numeric account id;
    - ``webui``, which is the actor of a deployment that authenticates a shared token and no
      person;
    - ``webui:<claim>``, which is the person a trusted proxy asserted.

    The prefix on the third shape is not decoration. It keeps the path inside the identity, so
    an audit record says which path authenticated the person, and a chat sender of the same
    text cannot read as the same person. The gate never splits the value, so a list that names
    ``webui`` grants nothing to ``webui:alberto@example.com`` and the reverse holds too.

    A blank value names nobody, and the gate matches nobody against it.
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

    # A floor of one day, because AuditStore.prune keeps every segment at zero or less. That is
    # the correct fail-safe for a store that must never empty itself by accident, and it also
    # means a hand-edited zero would turn retention off with no message. The WebUI already
    # refuses a value below one day, and this covers the file the WebUI never sees.
    retention_days: int = Field(default=90, ge=1)
    record_command_text: bool = False


class GatesConfig(Base):
    """The whole policy surface. #8 enforces the unattended half first."""

    model_config = _FORBID_EXTRA

    approvers: list[Approver] = Field(default_factory=list)
    # Which paths authenticate an approver (#13). An authenticated-path list is authority, so
    # it belongs here beside the approvers rather than hardcoded in the gate. A hardcoded set
    # could not express a deployment that hardened another channel on purpose, and it could
    # not name a specific missing path in the "configure a second path" error.
    #
    # The default names ``webui`` alone. The WebUI has a real session concept, and it supports
    # trusted-proxy bootstrap auth for Cloudflare Access and similar products (7413ae89). One
    # path also means no runtime approval path for an unusual action, which #13 reports as a
    # named refusal. Entries use the same vocabulary as ``Approver.channel``.
    approval_paths: list[str] = Field(default_factory=lambda: ["webui"])
    # Whether a second person on one path may replace a second path (#47, item 11). #13 compares
    # path strings, so two people behind one proxy are one path and neither can answer the
    # other's request. With real identities that is the wrong answer, and this flag changes it.
    #
    # The default is false, and the reason is not caution. The origin identity is an assertion of
    # the agent: nanoinfra/gates/executor/protocol.py already says that about the origin path,
    # and origin_actor inherits it exactly. So a compromised agent can claim that the request
    # came from another person, and one genuine human approval then substitutes for two
    # authenticated paths. The human still reads the command and the host list that the executor
    # rendered, which stays the real protection. But the deployment gives up the property that
    # one compromised account cannot hold both halves.
    #
    # An operator who accepts that trade turns the flag on, and #8 echoes the posture at start.
    # nanoinfra/gates/approvals.py holds the rule itself.
    identity_independence: bool = False
    # How long one suspended action waits for a human (#38). The executor holds the action on
    # its socket for this long, and then it refuses.
    #
    # The default is short for the reason #12 gives: a human needs time to read a rendered host
    # list, and no longer. Ambient authority must not develop out of an approval nobody spent.
    # The ceiling equals MAX_TTL_S in nanoinfra/gates/tokens.py. A wait longer than that ceiling
    # would produce an approval whose token cannot cover the action it approved. The value is
    # repeated rather than imported, because tokens.py imports the agent tree and this module
    # must stay a leaf. tests/gates/test_approval_gate.py asserts the two agree.
    approval_timeout_s: int = Field(default=120, ge=1, le=300)
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
