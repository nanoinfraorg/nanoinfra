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
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_INVENTORY,
    MUTATE_REMOTE,
)
from nanoinfra.agent.tools.context import EXECUTION_CONTEXT_INTERACTIVE
from nanoinfra.servers.scope import ALL, GROUP, HOST

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from nanoinfra.config.gates import ContextPolicy, GatesConfig

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
    # With an inventory, compare resolved targets (#24). Without one, compare labels: an
    # inventory write reaches no host, so #23's caller has nothing to resolve, and every
    # caller before #24 already compared labels.
    allowed = (
        _resolved_grant_hosts(servers, grant_hosts, cache)
        if servers is not None
        else set(grant_hosts)
    )
    return all(host in allowed for host in hosts)


def _matching_grant_id(
    gates: GatesConfig,
    *,
    context_key: str,
    hosts: Sequence[str],
    command: str,
    servers: Any = None,
    cache: dict[str, set[str]] | None = None,
) -> str | None:
    for index, grant in enumerate(gates.standing_grants):
        if context_key not in grant.contexts:
            continue
        if _grant_matches(
            grant.hosts,
            grant.commands,
            hosts=hosts,
            command=command,
            servers=servers,
            cache=cache,
        ):
            return grant.id or f"grant[{index}]"
    return None


def evaluate(
    gates: GatesConfig,
    *,
    capability_class: str,
    scope: str,
    execution_context: str,
    hosts: Iterable[str],
    command: str,
    servers: Any = None,
) -> Decision:
    """Decide one action. Safe to call before any credential is resolved.

    ``servers`` is the inventory store. Pass it so a grant host resolves through the same
    resolver the action used (#24). A caller that passes nothing gets label comparison, which
    is correct for #23's inventory writes, since an inventory write reaches no host.
    """
    host_tuple = tuple(hosts)
    # One resolve per grant host for this decision, and nothing survives the call (#35).
    resolve_cache: dict[str, set[str]] = {}
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

    policy = _context_policy(gates, unattended=unattended)

    if capability_class == CREDENTIAL_ACCESS:
        return Decision(
            _decision_for(policy.credential_access),
            f"{capability_class} in a {context_key} context is {policy.credential_access}.",
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
    if configured in ("grant", "approve"):
        grant_id = _matching_grant_id(
            gates,
            context_key=context_key,
            hosts=host_tuple,
            command=command,
            servers=servers,
            cache=resolve_cache,
        )
        if grant_id is not None:
            return Decision(
                Outcome.ALLOW,
                f"Standing grant {grant_id} covers this action, so nobody was asked.",
                grant_id,
                resolved_targets=host_tuple,
            )
        if configured == "grant":
            return Decision(
                Outcome.DENY,
                _missing_grant_reason(capability_class, scope, context_key, host_tuple),
            )

    outcome = _decision_for(configured)
    if outcome is Outcome.DENY:
        # A grant that matches everything except the matrix is the most confusing state an
        # operator can reach: they wrote the grant, and nothing happened. Name the key.
        shadowed = _matching_grant_id(
            gates,
            context_key=context_key,
            hosts=host_tuple,
            command=command,
            servers=servers,
            cache=resolve_cache,
        )
        if shadowed is not None:
            return Decision(
                Outcome.DENY,
                f"Standing grant {shadowed} covers this action, but "
                f"gates.{context_key}.mutate.remote.{scope} is {configured!r}. "
                "Set it to 'grant' for the grant to apply.",
            )
        return Decision(
            Outcome.DENY,
            _missing_grant_reason(capability_class, scope, context_key, host_tuple)
            if unattended
            else f"{capability_class} at {scope} scope is deny for a {context_key} context.",
        )
    return Decision(outcome, f"{capability_class} at {scope} scope is {configured}.")


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


__all__ = ["Decision", "Outcome", "evaluate", "load_policy"]
