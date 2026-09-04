"""The gate runtime the gateway builds once at boot -- nanoinfraorg/nanoinfra#33.

Five parts shipped as libraries and the gate called none of them, so three properties the
closed issues describe were not true in a running gateway: a denial was not terminal, no latch
formed, and no audit record landed. This module joins them.

The gateway builds one runtime and keeps the ``LatchController`` on the operator side. Only the
runtime travels toward the tool path. A module-level singleton would let any import reach the
controller, and the split in #15 exists to stop exactly that. #18 moves this whole object into
the executor process without a redesign.

Two orderings carry the security properties:

- A latched class refuses before policy runs. Re-asking policy would produce a fresh prompt,
  and a fresh prompt is the brute-force oracle #15 removes.
- A refusal records before it returns. #16 raises on a write failure, so an action that nothing
  recorded refuses rather than runs.

**Who is acting is read here, never passed (#251).** A refusal this runtime records is the record
an operator reads first, and for a delegated turn it has to name the peer that acted, the agent
that asked, and the human behind them -- the same three names the executor writes on the far side
of the socket. So this module reads them off the bound request rather than take them as keywords:
a parameter would let one call site name a different agent from the one that ran, and the two
records of one action would then disagree. The execution context is normalised the same way and
for the same reason, so a delegated turn that the executor decided as attended is not recorded
here as unattended.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nanoinfra.gates.audit import AuditStore
from nanoinfra.gates.delegation import Delegation, agent_name
from nanoinfra.gates.latch import DenialLatch, LatchController, LatchEvent, new_denial_latch
from nanoinfra.gates.latch_restore import restore_latches
from nanoinfra.gates.policy import Outcome
from nanoinfra.gates.tokens import ApprovalTokenStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nanoinfra.config.gates import GatesConfig

# What each outcome is called in the record. The audit vocabulary stays the operator's
# vocabulary, so `denied` matches the latch event kind rather than the enum name.
_DECISION_NAMES = {
    Outcome.ALLOW: "allow",
    Outcome.APPROVE: "approve",
    Outcome.DENY: "denied",
}


@dataclass(frozen=True, slots=True)
class GateRuntime:
    """The gate half. It can deny, refuse, and record. It cannot clear a latch.

    ``tokens`` belongs to the process that decides. After #18 that process is the executor, and
    after #38 the executor builds its own store beside its pending approvals. So this field is
    the store of whichever process built this runtime, and the agent's copy issues nothing. A
    caller on the agent side must not read it as the authority for an approval.
    """

    audit: AuditStore
    tokens: ApprovalTokenStore
    _latch: DenialLatch

    def latched_refusal(
        self, *, session_id: str, capability_class: str, tool: str, turn_id: str | None = None
    ):
        """Return a refusal when this class is latched, or None to let policy decide.

        Callers ask this **before** policy. A latched class must not reach a policy question,
        because a question can produce a prompt, and a fresh prompt is the oracle.
        """
        return self._latch.refuse(
            session_id=session_id,
            capability_class=capability_class,
            tool=tool,
            turn_id=turn_id,
        )

    def latched_classes(self, session_id: str) -> frozenset[str]:
        """Which capability classes this session is already blocked for.

        A read, so it stays on this half: commissioning (#184) has to tell an operator that a
        correct grant changes nothing while the session is blocked, and asking `refuse` instead
        would count a refusal that never happened.
        """
        return self._latch.latched_classes(session_id)

    def refuse_action(
        self,
        *,
        session_id: str,
        capability_class: str,
        tool: str,
        reason: str,
        execution_context: str,
        turn_id: str | None = None,
        scope: str | None = None,
        hosts: Sequence[str] | None = None,
        command: str | None = None,
    ):
        """Deny one action: end it, latch the class, and record it. One call, on purpose.

        ``deny`` in #15 latches as it denies, so a caller cannot deny without latching.
        """
        self.record_decision(
            outcome=Outcome.DENY,
            capability_class=capability_class,
            execution_context=execution_context,
            session_id=session_id,
            tool=tool,
            scope=scope,
            hosts=hosts,
            command=command,
            reason=reason,
        )
        return self._latch.deny(
            session_id=session_id,
            capability_class=capability_class,
            tool=tool,
            reason=reason,
            turn_id=turn_id,
        )

    def record_decision(
        self,
        *,
        outcome: Outcome,
        capability_class: str,
        execution_context: str,
        session_id: str | None = None,
        tool: str | None = None,
        scope: str | None = None,
        hosts: Sequence[str] | None = None,
        command: str | None = None,
        reason: str | None = None,
        grant_id: str | None = None,
        actor: str | None = None,
        origin_path: str | None = None,
        approval_path: str | None = None,
        token_nonce: str | None = None,
        exit_code: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Write one audit record. Raises OSError when the write fails.

        The failure is not caught here. #16 raises so the caller can fail closed, and an
        action that nothing recorded must not run.
        """
        acting = _acting_now()
        self.audit.record(
            decision=_DECISION_NAMES[outcome],
            capability_class=capability_class,
            execution_context=acting.effective_execution_context(execution_context),
            acting_agent=acting.acting_agent or None,
            delegated_by=acting.delegated_by or None,
            session_id=session_id,
            tool=tool,
            scope=scope,
            hosts=list(hosts) if hosts else None,
            command=command,
            reason=reason,
            grant_id=grant_id,
            actor=actor,
            origin_path=origin_path,
            approval_path=approval_path,
            token_nonce=token_nonce,
            exit_code=exit_code,
            duration_ms=duration_ms,
        )


def _acting_now() -> Delegation:
    """Who is acting on the turn this call belongs to, read from the bound request.

    Empty outside a request, and empty on every turn that names no agent -- which is every turn
    of a deployment with one agent, so the record it writes there is byte for byte the record it
    wrote before this existed. The names are normalised, because everything downstream of here
    renders them.
    """
    from nanoinfra.agent.tools.context import current_request_context

    context = current_request_context()
    if context is None:
        return Delegation()
    return Delegation(
        acting_agent=agent_name(context.agent),
        delegated_by=agent_name(context.delegated_by),
        # The human the spawning turn authenticated. A delegated turn carries the originating
        # person here, and carries nothing when there was none.
        origin_actor=(context.sender_id or "").strip(),
    )


def build_gate_runtime(
    gates: GatesConfig, *, root: Path | str
) -> tuple[GateRuntime, LatchController]:
    """Build the runtime and hand the operator half back separately.

    The caller keeps the ``LatchController`` behind an operator surface and passes the
    ``GateRuntime`` toward the tools. Nothing on the runtime reaches the controller.

    Latch state comes from the audit log (#32), so a restart does not clear a denial. The agent
    can cause a restart, and deny-restart-retry would otherwise be a loop no human rate-limits.
    """
    audit = AuditStore(root, config=gates.audit)
    restored = restore_latches(audit)

    def record(event: LatchEvent) -> None:
        # Every latch event becomes an audit record, so a latched session that keeps trying is
        # visible. A write failure here must not turn a refusal into a pass, and #15's emit()
        # already swallows a recorder failure for that reason.
        audit.record(
            decision=str(event.kind),
            capability_class=event.capability_class,
            execution_context="automation",
            session_id=event.session_id,
            tool=event.tool,
            actor=event.actor,
            reason=event.reason,
            command_digest=event.action_digest,
        )

    latch, controller = new_denial_latch(record=record, restored=restored)
    return GateRuntime(audit=audit, tokens=ApprovalTokenStore(), _latch=latch), controller


__all__ = ["GateRuntime", "build_gate_runtime"]
