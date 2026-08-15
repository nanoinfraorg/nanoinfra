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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from nanoinfra.gates.audit import AuditStore
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
    """The gate half. It can deny, refuse, and record. It cannot clear a latch."""

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
        self.audit.record(
            decision=_DECISION_NAMES[outcome],
            capability_class=capability_class,
            execution_context=execution_context,
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
