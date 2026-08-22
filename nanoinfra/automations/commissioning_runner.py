"""Run one automation as a rehearsal, and write down what it would meet -- #183, #184.

The rehearsal is the automation's own turn, built by the same code that builds a scheduled run
(``build_bound_turn``), with every gated tool forced to preview. So it rehearses the automation
that exists rather than a paraphrase of it: the same references, the same declared skills, the
same delivery routing, the same prompt.

Three things come back, and only the first is the grant:

1. what each previewed action would meet on the schedule, and the grant that would permit it;
2. the credential class, because a permitted command still dies at the secret;
3. the latch, because a latched session refuses before the gate is consulted at all, so a correct
   grant changes nothing until an operator clears it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from loguru import logger

from nanoinfra.automations.commissioning import (
    CommissioningCollector,
    PreviewedAction,
    commissioning_turn,
)
from nanoinfra.automations.commissioning_state import (
    ERROR,
    MAX_FINDING_CHARS,
    MAX_PROPOSED_GRANTS,
    OK,
    REFUSED,
    UNCHECKED,
    CommissioningState,
    commissioning_fingerprint,
)
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.cron.bound_runner import build_bound_turn
from nanoinfra.cron.service import CronJobTerminalError
from nanoinfra.cron.types import CronJob


class CommissioningAgent(Protocol):
    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        ...


class LatchView(Protocol):
    """Whether a session is already blocked for a capability class."""

    def latched_classes(self, session_id: str) -> frozenset[str]:
        ...


@dataclass(frozen=True, slots=True)
class CommissioningReport:
    """What one rehearsal found, for the operator and for the record."""

    state: CommissioningState
    actions: tuple[PreviewedAction, ...]
    response: str = ""
    latched_classes: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return self.state.blocks_the_schedule


def _grant_id(name: str, index: int) -> str:
    slug = "".join(char if char.isalnum() or char in "-_" else "-" for char in name).strip("-")
    base = (slug or "automation").lower()[:48]
    return base if index == 0 else f"{base}-{index + 1}"


def compose_finding(
    *,
    name: str,
    collector: CommissioningCollector,
    latched_classes: tuple[str, ...] = (),
) -> tuple[str, list[dict[str, Any]]]:
    """Turn previewed actions into the sentences an operator acts on, plus the grants to write."""
    lines: list[str] = []
    grants: list[dict[str, Any]] = []

    if latched_classes:
        # First, because it outranks every grant below: the gate is not even consulted.
        lines.append(
            "This automation's session is blocked for "
            + ", ".join(sorted(latched_classes))
            + ". A refusal there happens before the gate is consulted, so the grants below "
            "change nothing until an operator clears it."
        )

    if not collector.actions:
        lines.append(
            "The rehearsal took no gated action, so this automation needs no standing grant."
        )
        return "\n".join(lines)[:MAX_FINDING_CHARS], grants

    for action in collector.actions:
        if action.permitted:
            covered = f" (standing grant {action.grant_id})" if action.grant_id else ""
            lines.append(f"{action.tool}: permitted{covered}. {action.reason}")
            continue
        lines.append(f"{action.tool}: would be refused. {action.reason}")
        if action.credential_outcome not in (None, "allow"):
            lines.append(
                f"{action.tool}: the credential it needs would be "
                f"{action.credential_outcome}. {action.credential_reason}"
            )
        if not action.grantable:
            # Naming the reason matters more than the refusal: an operator who is told "no grant"
            # without being told that no grant could exist writes one and waits for it to work.
            lines.append(
                f"{action.tool}: a standing grant cannot permit this, so the policy itself has "
                "to change."
            )
            continue
        if len(grants) < MAX_PROPOSED_GRANTS:
            grants.append(action.as_grant(grant_id=_grant_id(name, len(grants))))

    return "\n".join(lines)[:MAX_FINDING_CHARS], grants


async def commission_cron_job(
    job: CronJob,
    *,
    agent: CommissioningAgent,
    workspace_path: Path | None = None,
    latches: LatchView | None = None,
) -> CommissioningReport:
    """Rehearse one cron job. Nothing this function does can reach a host.

    The rehearsal runs as the job's own turn, so its answer reaches the operator through the
    channel the automation already delivers on. The verdict is returned rather than written here:
    the caller owns the store, and a rehearsal must not be able to disable an automation without
    the caller that asked for it knowing.
    """
    session_key = job.payload.session_key or ""
    if not session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    fingerprint = commissioning_fingerprint(
        message=job.payload.message,
        references=job.references,
        skills=job.skills,
    )
    latched = tuple(sorted(latches.latched_classes(session_key))) if latches else ()

    try:
        with commissioning_turn() as (turn_id, collector):
            turn = build_bound_turn(
                job, workspace_path=workspace_path, commissioning_id=turn_id
            )
            resp = await agent.submit_cron_turn(turn.message(session_key=session_key))
            finding, grants = compose_finding(
                name=job.name, collector=collector, latched_classes=latched
            )
            actions = tuple(collector.actions)
            refused = bool(collector.refused) or bool(latched)
    except CronJobTerminalError as exc:
        # A reference that no longer resolves is a finding about this automation and not a fault
        # of the rehearsal: every scheduled run will fail identically, so the verdict is refused
        # and the automation is the thing to fix.
        return CommissioningReport(
            state=CommissioningState(
                status=REFUSED,
                checked_at_ms=int(time.time() * 1000),
                finding=(
                    f"This automation cannot run as written: {exc} No standing grant changes "
                    "that -- the reference has to be repointed or removed."
                ),
                fingerprint=fingerprint,
            ),
            actions=(),
            latched_classes=latched,
        )
    except Exception as exc:  # noqa: BLE001 - the reason belongs on the record, not in a traceback
        # A rehearsal that could not complete learned nothing, which is not the same as refused.
        # Reporting it as refused would disable an automation over an unrelated fault.
        logger.warning("Commissioning of cron job '{}' failed: {}", job.name, exc)
        return CommissioningReport(
            state=CommissioningState(
                status=ERROR,
                checked_at_ms=int(time.time() * 1000),
                finding=f"The commissioning run did not complete: {exc}",
                fingerprint=fingerprint,
            ),
            actions=(),
            latched_classes=latched,
        )

    return CommissioningReport(
        state=CommissioningState(
            status=REFUSED if refused else OK,
            checked_at_ms=int(time.time() * 1000),
            finding=finding,
            fingerprint=fingerprint,
            proposed_grants=tuple(grants),
        ),
        actions=actions,
        response=(resp.content if resp else ""),
        latched_classes=latched,
    )


#: What an operator is told about a trigger at creation. A trigger's content arrives from whoever
#: fires it, so no rehearsal can learn which commands it will run: rehearsing a made-up message
#: would propose a grant for a command the real firing may never use, which is worse than saying
#: nothing. So this states the limit instead of guessing, and the trigger is not disabled over it.
TRIGGER_COMMISSIONING_NOTE = (
    "A trigger carries no stored message: its content arrives from whoever fires it, so a "
    "rehearsal cannot learn which commands it will run. What it may do unattended is bounded by "
    "gates.unattended and by the standing grants that already exist, and an action outside those "
    "is refused when it fires. Commission the work by firing it once while you are watching."
)


def trigger_commissioning_state(*, name: str = "") -> CommissioningState:
    """The verdict a newly created trigger carries.

    ``unchecked`` on purpose, and not ``ok``: nothing was learned about what it will run, and a
    verdict that reads as clean would be a claim this cannot make.
    """
    _ = name
    return CommissioningState(
        status=UNCHECKED,
        checked_at_ms=int(time.time() * 1000),
        finding=TRIGGER_COMMISSIONING_NOTE,
    )


__all__ = [
    "TRIGGER_COMMISSIONING_NOTE",
    "CommissioningAgent",
    "CommissioningReport",
    "commission_cron_job",
    "compose_finding",
    "trigger_commissioning_state",
]
