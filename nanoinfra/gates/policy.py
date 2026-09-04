"""Evaluate gate policy -- nanoinfraorg/nanoinfra#8.

One function decides. It reads the operator's policy (#7), the capability class (#3), the
resolved scope (#4), and the execution context (#5). It opens no transport and resolves no
secret, so the caller stays in charge of ordering. That matters: ``ExecuteOnServerTool`` must
ask before it decrypts a credential and before it writes a job record.

``evaluate`` reads the local inventory when a caller passes ``servers``, because #24 compares
resolved targets rather than mutable labels. It reads no other file, and it never dials a
host.

Three fail-closed rules run before any policy lookup:

- An unknown capability class refuses. A class the policy does not model must not fall
  through to allow.
- An unknown execution context counts as unattended. Only the exact literal ``interactive``
  earns attended trust, the same rule ``is_unattended_execution_context`` applies.
- An unresolved scope refuses. #4 returns ``unresolved`` when it cannot expand a pattern,
  and an unexpandable pattern is not an empty one.

An unattended context has exactly one allow path: a standing grant that covers every
resolved host and holds the resolved command exactly. There is no interactive fallback,
because a prompt with nobody present becomes a hang or a rubber stamp.

An expired grant is treated as absent, and it is never reported as absent (#218). The reason
text names it and gives the date, because the state this feature has to explain is a turn that
ran unattended yesterday and waits for a human today. "No standing grant names this action"
would send an operator to write the grant that is already in their config.

**A delegated action is decided here too (#251).** ``delegation`` carries the peer that acts, the
agent that asked, and the human behind the chain, and this function applies both rules that
follow from it: the ceiling, which refuses a capability class the spawning turn did not hold, and
the actor rule, which reads a delegated turn as attended exactly when a human is on its chain.
Both live in ``nanoinfra/gates/delegation.py`` and are applied *here* rather than in the tool
that asks, because a tool that checks its own ceiling is a ceiling the model can talk past. A
caller that passes nothing gets the behaviour of every deployment with one agent.

``credential.access`` is a decision about one decryption, and never about the action (#39).
``evaluate_credential_access`` answers it, and that answer is ``allow`` or ``deny`` alone. A
second ``approve`` outcome would prompt a human twice for one action, and #13 says a human who
reads forty prompts a week stops reading them. So ``approve`` and ``grant`` read the
authorization the action already carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_INVENTORY,
    MUTATE_REMOTE,
    READ,
)
from nanoinfra.agent.tools.context import EXECUTION_CONTEXT_INTERACTIVE
from nanoinfra.gates.delegation import Delegation
from nanoinfra.servers.scope import ALL, GROUP, HOST

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from nanoinfra.config.gates import ContextPolicy, GatesConfig

# What a caller that names no delegation gets: no agent, no chain, no ceiling. Shared because it
# is frozen, so no evaluation can mutate the one every other evaluation reads.
_NO_DELEGATION = Delegation()

# Classes this policy models. A class outside this set refuses, so a new capability cannot
# reach a host until someone gives it a policy.
_GATED_CLASSES = frozenset({MUTATE_REMOTE, MUTATE_INVENTORY, CREDENTIAL_ACCESS})

# Scopes this policy models. #4's `unresolved` is deliberately absent.
_KNOWN_SCOPES = frozenset({HOST, GROUP, ALL})


class Outcome(Enum):
    """What the caller must do next."""

    ALLOW = "allow"
    APPROVE = "approve"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Decision:
    """One policy answer, plus the text an operator reads when it refuses."""

    outcome: Outcome
    reason: str
    grant_id: str | None = None
    # The addresses a matched grant permitted, after resolution (#24). #16 records these
    # beside the grant id, so a reviewer sees which addresses ran rather than which labels
    # the operator typed.
    resolved_targets: tuple[str, ...] = ()
    # The suspended action a human answered, when that answer satisfied this decision (#39).
    # It is the request id of the pending approval, and it is not the token nonce. The audit
    # log has more readers than the approval path has, so the record names the approval and
    # omits the means to spend it (#12).
    approval_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionAuthorization:
    """What already authorized the action that needs a credential -- #39.

    The executor builds this from the ``mutate.remote`` decision it just took. A standing grant
    covered the action, or a human approved it, or neither did. ``credential.access`` reads this
    instead of a second prompt, because one action must cost one human decision.

    ``actor`` and ``approval_path`` describe the human, and both stay empty for a grant. A grant
    asks nobody, so a name there would invent an approver.
    """

    grant_id: str | None = None
    approval_id: str | None = None
    actor: str | None = None
    approval_path: str | None = None
    # The decision the matrix took for the action itself, and the scope it took it at. An operator
    # who set `allow` at that scope said the agent may run the action, and the action needs the
    # credential of the server it names. Without this field the class refused every allowed action
    # against a server that holds a secretRef, and `allow` then meant nothing.
    policy_decision: str | None = None
    scope: str | None = None


def resolve_scope_for_grant_host(server: Any) -> Any:
    """Resolve one grant host. A seam, so a caller can count the resolves (#35).

    A real resolve runs `ansible-inventory` and costs about a quarter of a second, so the number
    of calls per decision is a property worth testing rather than guessing.
    """
    from nanoinfra.servers.scope import resolve_scope

    return resolve_scope(server)


def _resolved_grant_hosts(
    servers: Any, grant_hosts: Sequence[str], cache: dict[str, set[str]] | None = None
) -> set[str]:
    """Resolve each grant host through the resolver the action used (#24).

    A grant lists inventory names, and a name is mutable. #23 stops an unattended context
    from editing the inventory, but an interactive operator can still repoint a record. A
    later automation must not inherit a redirected grant from that edit, so the comparison
    uses resolved targets.

    An entry that names no record stays in the set as its literal self. Two reasons. A grant
    written before #24 lists addresses rather than names, and those configs must keep working.
    A name that resolves to nothing must also match nothing, which the literal achieves,
    because a resolved action never carries a bare inventory label.
    """
    from nanoinfra.servers.lookup import resolve_server
    from nanoinfra.servers.scope import ScopeResolutionError

    resolved: set[str] = set()
    for entry in grant_hosts:
        # One resolve per host per decision (#35). Every grant is checked against the same
        # action, so several grants naming one host would otherwise each pay for it. The cache
        # lives for this call and no longer: #24 re-resolves on purpose, so an inventory write
        # between two actions must still invalidate a match.
        if cache is not None and entry in cache:
            resolved.update(cache[entry])
            continue
        server = resolve_server(servers, entry)
        if server is None:
            resolved.add(entry)
            if cache is not None:
                cache[entry] = {entry}
            continue
        try:
            hosts = set(resolve_scope_for_grant_host(server).hosts)
            resolved.update(hosts)
            if cache is not None:
                cache[entry] = hosts
        except ScopeResolutionError:
            # A grant host that will not resolve cannot be checked, so it grants nothing.
            # Dropping it is the fail-closed direction: the action's own hosts stay
            # uncovered and the grant does not match.
            continue
    return resolved


def _context_policy(gates: GatesConfig, *, unattended: bool) -> ContextPolicy:
    return gates.unattended if unattended else gates.interactive


def _decision_for(value: str) -> Outcome:
    """Map a config decision string onto an outcome.

    ``grant`` is not an outcome. It means "allow only when a standing grant matches", and
    the caller already checked that before it reaches here, so an unmatched ``grant``
    refuses.
    """
    if value == "allow":
        return Outcome.ALLOW
    if value == "approve":
        return Outcome.APPROVE
    return Outcome.DENY


def _grant_matches(
    grant_hosts: Sequence[str],
    grant_commands: Sequence[str],
    *,
    hosts: Sequence[str],
    command: str,
    servers: Any = None,
    cache: dict[str, set[str]] | None = None,
    connector_grant: bool = False,
) -> bool:
    """True when this grant covers the whole action.

    Every resolved host must appear in the grant. Partial coverage is no coverage: a group
    must never execute on a host nobody granted. The command must match exactly, because
    ``commands`` is an allowlist and not a pattern language.
    """
    if command not in grant_commands:
        return False
    if not hosts:
        return False
    if connector_grant:
        # A connector grant permits a connector call and nothing else. Matching it here would
        # let a grant written for `send_message` cover a shell command that happened to share
        # the name.
        return False
    # With an inventory, compare resolved targets (#24). Without one, compare labels: an
    # inventory write reaches no host, so #23's caller has nothing to resolve, and every
    # caller before #24 already compared labels.
    allowed = (
        _resolved_grant_hosts(servers, grant_hosts, cache)
        if servers is not None
        else set(grant_hosts)
    )
    return all(host in allowed for host in hosts)


@dataclass(frozen=True, slots=True)
class _GrantHits:
    """The grants that cover one action: the live one, and the expired one behind it (#218).

    Both halves travel because an expired grant is not a missing grant. A turn that ran
    unattended yesterday and waits for a human today needs the reason in the record, and "no
    standing grant names this action" sends an operator to write the grant they already wrote.
    """

    live: str | None = None
    expired: str | None = None
    expired_at: datetime | None = None


def _grant_hits(
    gates: GatesConfig,
    *,
    context_key: str,
    hosts: Sequence[str],
    command: str,
    servers: Any = None,
    cache: dict[str, set[str]] | None = None,
) -> _GrantHits:
    """Find the first live grant that covers the action, and the first expired one.

    The scan continues past an expired match, because a second grant may still be live. It
    stops at the first live one, which is the value every caller before #218 read.
    """
    now = datetime.now(UTC)
    expired: str | None = None
    expired_at: datetime | None = None
    for index, grant in enumerate(gates.standing_grants):
        if context_key not in grant.contexts:
            continue
        if not _grant_matches(
            grant.hosts,
            grant.commands,
            hosts=hosts,
            command=command,
            servers=servers,
            cache=cache,
            connector_grant=bool(grant.connectors or grant.operations),
        ):
            continue
        named = grant.id or f"grant[{index}]"
        if not grant.is_expired(now):
            return _GrantHits(live=named, expired=expired, expired_at=expired_at)
        if expired is None:
            expired, expired_at = named, grant.expires_at
    return _GrantHits(expired=expired, expired_at=expired_at)


def _expiry_sentence(hits: _GrantHits) -> str:
    """The clause that names an expired grant, or nothing at all.

    It reads as one sentence appended to a refusal or to an approve reason, so the audit line
    says *expired* where it used to say nothing.
    """
    if hits.expired is None:
        return ""
    when = hits.expired_at.isoformat() if hits.expired_at is not None else "an earlier date"
    return (
        f" Standing grant {hits.expired} covers this action, and it expired at {when}. "
        "Nothing removed it: set a later expiresAt on that grant, or approve the action once "
        "and add a fresh grant."
    )


def evaluate(
    gates: GatesConfig,
    *,
    capability_class: str,
    scope: str,
    execution_context: str,
    hosts: Iterable[str],
    command: str,
    servers: Any = None,
    delegation: Delegation | None = None,
) -> Decision:
    """Decide one action. Safe to call before any credential is resolved.

    ``servers`` is the inventory store. Pass it so a grant host resolves through the same
    resolver the action used (#24). A caller that passes nothing gets label comparison, which
    is correct for #23's inventory writes, since an inventory write reaches no host.

    ``delegation`` names the peer that acts, the agent that asked, and the human behind the chain
    (#251). It does two things and only two: it refuses a class outside the ceiling the spawning
    turn declared, and it decides whether a delegated turn reads as attended. ``None`` is every
    turn of a deployment with one agent.
    """
    host_tuple = tuple(hosts)
    # One resolve per grant host for this decision, and nothing survives the call (#35).
    resolve_cache: dict[str, set[str]] = {}
    delegated = delegation or _NO_DELEGATION
    # The actor rule, before the context is read for anything. A delegated turn with a human on
    # its chain is attended, because an approval can reach that person; one without stays
    # unattended and a standing grant is its only allow path. Idempotent, so it does not matter
    # whether the caller already applied it.
    execution_context = delegated.effective_execution_context(execution_context)
    unattended = execution_context != EXECUTION_CONTEXT_INTERACTIVE
    context_key = "unattended" if unattended else "interactive"

    if capability_class not in _GATED_CLASSES:
        return Decision(
            Outcome.DENY,
            f"Refusing {capability_class!r}: this class has no policy, so it cannot reach a host.",
        )
    if scope not in _KNOWN_SCOPES:
        return Decision(
            Outcome.DENY,
            f"Refusing {capability_class!r} at scope {scope!r}: the target did not resolve, "
            "so the host set is unknown.",
        )
    # The ceiling, before any policy lookup. A peer must not reach a class the turn that spawned
    # it could not, whatever the matrix says for this context, so no grant and no approval can
    # answer this refusal.
    ceiling = delegated.ceiling_refusal(capability_class)
    if ceiling is not None:
        return Decision(Outcome.DENY, ceiling)

    policy = _context_policy(gates, unattended=unattended)

    if capability_class == CREDENTIAL_ACCESS:
        # One implementation, and no second wording (#39). A caller that reaches the class
        # through this function names no authorization, so `approve` and `grant` refuse here.
        # The executor calls `evaluate_credential_access` directly and passes what it holds.
        return evaluate_credential_access(
            gates, execution_context=execution_context, delegation=delegated
        )

    if capability_class == MUTATE_INVENTORY:
        # A standing grant carries no class, so it can never satisfy this. A grant that
        # permitted an inventory write could repoint a host and widen itself (#23).
        return Decision(
            _decision_for(policy.mutate_inventory),
            f"{capability_class} in a {context_key} context is {policy.mutate_inventory}. "
            "A standing grant cannot permit an inventory write.",
        )

    configured = getattr(policy.mutate_remote, scope)

    # A grant answers both `grant` and `approve`, and it never answers `deny` (#11).
    #
    # `grant` means "allow only when a grant matches", so an unmatched grant refuses.
    # `approve` means "a human may permit this", and a standing grant is a permission the
    # operator declared in advance, so a match skips the prompt. That is the point of #11: a
    # human who reads forty prompts a week stops reading them, so runtime approval must stay
    # the exception.
    # `deny` means the action is not permitted at all. A grant that could overrule that would
    # make the matrix advisory, so a deny stays a deny and the refusal names the shadowed
    # grant instead.

    # The expired match survives this branch, because an `approve` decision falls through to the
    # bottom and its reason is where "why is a human being asked today" has to be legible (#218).
    hits = _GrantHits()

    if configured in ("grant", "approve"):
        hits = _grant_hits(
            gates,
            context_key=context_key,
            hosts=host_tuple,
            command=command,
            servers=servers,
            cache=resolve_cache,
        )
        if hits.live is not None:
            return Decision(
                Outcome.ALLOW,
                f"Standing grant {hits.live} covers this action, so nobody was asked.",
                hits.live,
                resolved_targets=host_tuple,
            )
        if configured == "grant":
            return Decision(
                Outcome.DENY,
                _missing_grant_reason(capability_class, scope, context_key, host_tuple)
                + _expiry_sentence(hits),
            )

    outcome = _decision_for(configured)
    if outcome is Outcome.DENY:
        # A grant that matches everything except the matrix is the most confusing state an
        # operator can reach: they wrote the grant, and nothing happened. Name the key.
        hits = _grant_hits(
            gates,
            context_key=context_key,
            hosts=host_tuple,
            command=command,
            servers=servers,
            cache=resolve_cache,
        )
        if hits.live is not None:
            return Decision(
                Outcome.DENY,
                f"Standing grant {hits.live} covers this action, but "
                f"gates.{context_key}.mutate.remote.{scope} is {configured!r}. "
                "Set it to 'grant' for the grant to apply.",
            )
        return Decision(
            Outcome.DENY,
            (
                _missing_grant_reason(capability_class, scope, context_key, host_tuple)
                if unattended
                else f"{capability_class} at {scope} scope is deny for a {context_key} context."
            )
            + _expiry_sentence(hits),
        )
    return Decision(
        outcome, f"{capability_class} at {scope} scope is {configured}." + _expiry_sentence(hits)
    )


def _connector_grant_matches(
    grant: Any, *, connector: str, operation: str
) -> bool:
    """True when this grant was written for this connector call.

    A grant that names hosts or commands is not a connector grant, and the schema already
    refuses one that names both. This still checks, because a config written before that
    validator existed must not match here by having empty connector lists.
    """
    if not grant.connectors or not grant.operations:
        return False
    if grant.hosts or grant.commands:
        return False
    return connector in grant.connectors and operation in grant.operations


def _connector_grant_hits(
    gates: GatesConfig, *, context_key: str, connector: str, operation: str
) -> _GrantHits:
    """The same two answers for a connector call. Expiry is on the grant, not on the kind."""
    now = datetime.now(UTC)
    expired: str | None = None
    expired_at: datetime | None = None
    for index, grant in enumerate(gates.standing_grants):
        if context_key not in grant.contexts:
            continue
        if not _connector_grant_matches(grant, connector=connector, operation=operation):
            continue
        named = grant.id or f"grant[{index}]"
        if not grant.is_expired(now):
            return _GrantHits(live=named, expired=expired, expired_at=expired_at)
        if expired is None:
            expired, expired_at = named, grant.expires_at
    return _GrantHits(expired=expired, expired_at=expired_at)


def evaluate_connector(
    gates: GatesConfig,
    *,
    capability_class: str,
    execution_context: str,
    connector: str,
    operation: str,
    delegation: Delegation | None = None,
) -> Decision:
    """Decide one connector operation. The class comes from the connector's manifest.

    Scope is ``host``, and the reason is the same one inventory writes give: a connector call
    reaches one remote service under one credential, so blast radius does not vary with the
    operation. Widening it per operation would invent a tier the matrix does not model.

    ``read`` is not a gated class, here or anywhere, so a read is allowed and the record of it
    is the audit log rather than a decision. That asymmetry **is** the design: an MCP server's
    tools all resolve to ``mutate.remote``, and a connector that declares ``read`` on its GETs
    is what buys the difference.

    ``delegation`` applies the same two rules a command gets (#251). A peer can call a connector,
    and the ceiling and the actor rule cannot depend on which surface the peer reached for.
    """
    delegated = delegation or _NO_DELEGATION
    execution_context = delegated.effective_execution_context(execution_context)
    unattended = execution_context != EXECUTION_CONTEXT_INTERACTIVE
    context_key = "unattended" if unattended else "interactive"

    if capability_class == READ:
        return Decision(Outcome.ALLOW, f"{connector}.{operation} is a read.")

    # Before the class check below, because a class outside the ceiling is refused for being
    # outside the ceiling rather than for being unmodelled here.
    ceiling = delegated.ceiling_refusal(capability_class)
    if ceiling is not None:
        return Decision(Outcome.DENY, ceiling)

    if capability_class != MUTATE_REMOTE:
        # A connector operation is a remote call or a read of one. Anything else means the
        # manifest declared a class this path does not model, and inventing a policy for it
        # here would be the fail-open direction.
        return Decision(
            Outcome.DENY,
            f"Refusing {connector}.{operation}: a connector operation cannot be "
            f"{capability_class!r}.",
        )

    policy = _context_policy(gates, unattended=unattended)
    configured = policy.mutate_remote.host

    hits = _GrantHits()

    if configured in ("grant", "approve"):
        hits = _connector_grant_hits(
            gates, context_key=context_key, connector=connector, operation=operation
        )
        if hits.live is not None:
            return Decision(
                Outcome.ALLOW,
                f"Standing grant {hits.live} covers {connector}.{operation}, so nobody was asked.",
                hits.live,
            )
        if configured == "grant":
            return Decision(
                Outcome.DENY,
                f"Refusing {connector}.{operation}: gates.{context_key}.mutate.remote.host is "
                "'grant' and no standing grant names it. Add "
                f'{{"connectors": ["{connector}"], "operations": ["{operation}"]}} to '
                "gates.standingGrants to permit it." + _expiry_sentence(hits),
            )

    outcome = _decision_for(configured)
    if outcome is Outcome.DENY:
        hits = _connector_grant_hits(
            gates, context_key=context_key, connector=connector, operation=operation
        )
        if hits.live is not None:
            return Decision(
                Outcome.DENY,
                f"Standing grant {hits.live} covers {connector}.{operation}, but "
                f"gates.{context_key}.mutate.remote.host is {configured!r}. "
                "Set it to 'grant' for the grant to apply.",
            )
        return Decision(
            Outcome.DENY,
            f"Refusing {connector}.{operation}: gates.{context_key}.mutate.remote.host is "
            f"{configured!r}." + _expiry_sentence(hits),
        )
    return Decision(
        outcome,
        f"{connector}.{operation} is {capability_class}, which is {configured}."
        + _expiry_sentence(hits),
    )


def evaluate_credential_access(
    gates: GatesConfig,
    *,
    execution_context: str,
    authorization: ActionAuthorization | None = None,
    delegation: Delegation | None = None,
) -> Decision:
    """Decide one decryption -- #39. Call this before ``resolve_plaintext`` reads the store.

    The answer is ``allow`` or ``deny``, and never ``approve``. A second prompt would ask a
    human twice for one action, and #13 spends that attention elsewhere.

    ``authorization`` holds the action's own decision. A standing grant satisfies ``grant`` and
    ``approve``, because the operator declared that permission in advance (#11). A human approval
    satisfies ``approve``. A matrix ``allow`` satisfies both as well: an operator who allowed the
    action at that scope authorized the credential the action needs, and a class that refused it
    would make ``allow`` mean nothing. That combination reached a real operator, with the shipped
    default on this key.

    ``deny`` still refuses each of those, and that is where this class keeps its teeth: an
    unattended context can permit a granted command and refuse the decryption it would need.

    A value this function does not model refuses. A hand-edited key must cost the decryption
    rather than buy it.

    ``delegation`` applies the ceiling and the actor rule here as well (#251). A peer that could
    not have run the action must not be able to open the credential the action needs, and the
    order matters: this class is decided against the same context the action was.
    """
    delegated = delegation or _NO_DELEGATION
    execution_context = delegated.effective_execution_context(execution_context)
    ceiling = delegated.ceiling_refusal(CREDENTIAL_ACCESS)
    if ceiling is not None:
        return Decision(Outcome.DENY, ceiling)
    unattended = execution_context != EXECUTION_CONTEXT_INTERACTIVE
    context_key = "unattended" if unattended else "interactive"
    # ``str`` on purpose, and not a cast. #7 names four decision values, and the schema spells
    # two of them for this key today. This function models all four, so a widened schema needs
    # no edit here, and a value nothing models still refuses below.
    configured = str(_context_policy(gates, unattended=unattended).credential_access)
    carried = authorization or ActionAuthorization()
    key = f"gates.{context_key}.credential.access"

    if configured == "allow":
        return Decision(Outcome.ALLOW, f"{key} is 'allow', so the credential resolves.")

    if configured in ("approve", "grant"):
        # A grant answers both values, and an approval answers `approve` alone. The order
        # matches #11: this code reads the pre-declared permission first, because it asks nobody.
        if carried.grant_id is not None:
            return Decision(
                Outcome.ALLOW,
                f"{key} is {configured!r}, and standing grant {carried.grant_id} covers this "
                "action. That grant authorizes the credential the action needs.",
                grant_id=carried.grant_id,
            )
        if configured == "approve" and carried.approval_id is not None:
            return Decision(
                Outcome.ALLOW,
                f"{key} is 'approve', and {carried.actor!r} approved this action as "
                f"{carried.approval_id}. That approval authorizes the credential it needs.",
                approval_id=carried.approval_id,
            )
        if carried.policy_decision == "allow":
            # The general authorization, read last, so a grant or an approval names itself in the
            # record. An operator who allowed the action at this scope authorized the credential it
            # needs, and a refusal here would make that `allow` mean nothing.
            return Decision(
                Outcome.ALLOW,
                f"{key} is {configured!r}, and the matrix allows this action at "
                f"{carried.scope or 'this'} scope. That decision authorizes the credential the "
                "action needs, so no human answers twice for one action.",
            )
        return Decision(
            Outcome.DENY,
            f"The gate refuses {CREDENTIAL_ACCESS}: {key} is {configured!r}, and nothing "
            "authorized this action. A human approval, a standing grant, or an `allow` on the "
            f"matrix carries that authorization. Set {key} to 'allow', widen the matrix at this "
            "scope, or declare a grant in gates.standingGrants.",
        )

    return Decision(
        Outcome.DENY,
        f"The gate refuses {CREDENTIAL_ACCESS}: {key} is {configured!r}. The credential stays "
        "encrypted, so the action reaches no host.",
    )


def _missing_grant_reason(
    capability_class: str, scope: str, context_key: str, hosts: tuple[str, ...]
) -> str:
    """Name the class, the scope, and what a grant would have to say.

    An operator who debugs a broken automation at 03:00 needs to know which grant to write.
    A bare "denied" sends them to the issue tracker instead of to their config.
    """
    named = ", ".join(hosts) if hosts else "no resolved host"
    return (
        f"Refusing {capability_class} at {scope} scope in a {context_key} context. "
        f"A standing grant must list every resolved host ({named}) and the exact command. "
        "See gates.standingGrants."
    )


def load_policy() -> GatesConfig:
    """Read the operator's policy for one decision.

    This is the only function here that touches I/O, and it reads on every call on purpose.
    A gate must apply the policy in force now, not a snapshot from process start, so an
    operator who tightens policy does not have to restart the gateway to be protected.

    Any failure returns the restrictive defaults. Unparseable policy must fail closed. A
    malformed config file must not become a reason to skip the gate. The failure logs loudly,
    because a silent fallback to defaults would look like a working policy.
    """
    from nanoinfra.config.gates import GatesConfig as _GatesConfig
    from nanoinfra.config.loader import load_config

    try:
        return load_config().gates
    except Exception as exc:  # noqa: BLE001 -- fail closed on any config failure
        logger.warning("gates: policy unreadable, falling back to deny-by-default: {}", exc)
        return _GatesConfig()


__all__ = [
    "ActionAuthorization",
    "Decision",
    "Delegation",
    "Outcome",
    "evaluate",
    "evaluate_connector",
    "evaluate_credential_access",
    "load_policy",
]
