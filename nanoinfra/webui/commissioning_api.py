"""The operator surface that rehearses an automation and promotes its grant -- #186, #187, #188.

Two routes, and the asymmetry between them is the design.

**Rehearsing writes nothing.** It previews every gated action, evaluates the policy, and answers.
So it is available to any authenticated operator, and a caller cannot use it to act.

**Promoting writes a permission**, and it only writes what a rehearsal already found. The route
never takes a grant from the request: a grant supplied by the caller would let whatever composed
the request choose its own authority, which is the one thing standing grants exist to prevent. It
reads the verdict off the automation record, refuses the cases a grant may never cover, writes
through the same settings path the gates editor uses, and records who did it.

What a promotion writes is narrow and not guessable, so the caller is told in words: the grant
covers that command on those hosts in **any** unattended turn, not this automation. `StandingGrant`
carries no caller and matching ignores its id.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from loguru import logger

from nanoinfra.automations.commissioning_state import REFUSED
from nanoinfra.config.gates import StandingGrant

#: Contexts a promotion may name. An interactive grant would skip the prompt a person is there to
#: answer, and commissioning is about the unattended schedule.
_PROMOTABLE_CONTEXTS = ("unattended",)

#: The one class a standing grant can permit. `mutate.inventory` is excluded by #23 -- a grant
#: that permitted an inventory write could repoint a host and widen itself -- and
#: `credential.access` follows the action rather than carrying its own grant.
_PROMOTABLE_CLASS = "mutate.remote"


class CommissionRunner(Protocol):
    async def __call__(self, automation_id: str) -> dict[str, Any]:
        ...


class PromotionRefusedError(RuntimeError):
    """A promotion this route will not perform, with the reason an operator reads."""


def _text_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    values: list[Any] = list(cast("list[Any]", raw))
    return [str(value).strip() for value in values if str(value).strip()]


def _grant_payload(raw: object) -> dict[str, Any] | None:
    """Read one stored proposed grant, and refuse anything that is not exactly that shape."""
    if not isinstance(raw, Mapping):
        return None
    entry: dict[str, Any] = {
        str(key): value for key, value in cast("Mapping[Any, Any]", raw).items()
    }
    host_list = _text_list(entry.get("hosts"))
    command_list = _text_list(entry.get("commands"))
    if not host_list or not command_list:
        # An empty half would match nothing, and a grant that matches nothing reads to an
        # operator as one that works.
        return None
    context_list = _text_list(entry.get("contexts")) or list(_PROMOTABLE_CONTEXTS)
    if any(value not in _PROMOTABLE_CONTEXTS for value in context_list):
        return None
    return {
        "id": str(entry.get("id") or "").strip() or None,
        "contexts": context_list,
        "hosts": host_list,
        "commands": command_list,
    }


class CommissioningOperatorSurface:
    """Rehearse an automation, and promote what a rehearsal found."""

    def __init__(
        self,
        *,
        cron_service: Any = None,
        local_trigger_store: Any = None,
        commission: CommissionRunner | None = None,
        audit: Any = None,
    ) -> None:
        self._cron = cron_service
        self._triggers = local_trigger_store
        self._commission = commission
        self._audit = audit

    # -- rehearse ---------------------------------------------------------------------------

    @property
    def can_commission(self) -> bool:
        return self._commission is not None

    async def commission(self, automation_id: str) -> dict[str, Any]:
        """Run one rehearsal now. Nothing it does can reach a host."""
        if self._commission is None:
            raise PromotionRefusedError("this deployment cannot run a commissioning turn")
        return await self._commission(automation_id)

    # -- promote ----------------------------------------------------------------------------

    def promote(self, automation_id: str, *, actor: str, origin_path: str | None = None) -> dict[str, Any]:
        """Write the grants a rehearsal proposed for *automation_id*, and record who asked.

        Returns what was written, so the caller can state it back rather than describe it from
        the request it sent.
        """
        job = self._cron.get_job(automation_id) if self._cron else None
        if job is None:
            raise PromotionRefusedError("automation not found")
        state = job.commissioning
        if state.status != REFUSED:
            # Nothing to promote is not the same as a refusal to promote, but the route answers
            # the same way: an operator must not be able to write a grant for an automation no
            # rehearsal found a gap in.
            raise PromotionRefusedError(
                "this automation has no refused commissioning finding, so there is no grant to "
                "promote. Rehearse it first."
            )
        if not state.proposed_grants:
            raise PromotionRefusedError(
                "the finding proposes no grant. A standing grant cannot permit what it found -- "
                "an inventory write, or an unbounded host set -- so the policy has to change "
                "instead."
            )

        grants: list[dict[str, Any]] = []
        for raw in state.proposed_grants:
            payload = _grant_payload(raw)
            if payload is None:
                raise PromotionRefusedError(
                    "the stored finding is not a grant this route will write. Rehearse the "
                    "automation again."
                )
            grants.append(payload)

        written = self._write_grants(grants, name=job.name)
        self._record_promotion(job, written, actor=actor, origin_path=origin_path)
        return {
            "granted": written,
            "note": (
                "A standing grant covers that command on those hosts in any unattended turn, "
                "not this automation alone."
            ),
            "requires_restart": True,
        }

    def _write_grants(self, grants: Sequence[dict[str, Any]], *, name: str) -> list[dict[str, Any]]:
        """Append the grants to config, through the path the gates editor already uses."""
        from nanoinfra.config.loader import load_config, save_config

        config = load_config()
        existing = list(config.gates.standing_grants)
        taken = {grant.id for grant in existing if grant.id}
        written: list[dict[str, Any]] = []
        for payload in grants:
            grant_id = payload["id"] or name
            candidate = grant_id
            suffix = 2
            while candidate in taken:
                candidate = f"{grant_id}-{suffix}"
                suffix += 1
            taken.add(candidate)
            grant = StandingGrant(
                id=candidate,
                contexts=list(payload["contexts"]),  # pyright: ignore[reportArgumentType]
                hosts=list(payload["hosts"]),
                commands=list(payload["commands"]),
            )
            if any(
                other.contexts == grant.contexts
                and other.hosts == grant.hosts
                and other.commands == grant.commands
                for other in existing
            ):
                # Already permitted. Writing a duplicate would leave two grants an operator has
                # to revoke separately to take the permission away.
                continue
            existing.append(grant)
            written.append({
                "id": candidate,
                "contexts": list(grant.contexts),
                "hosts": list(grant.hosts),
                "commands": list(grant.commands),
            })
        config.gates.standing_grants = existing
        save_config(config)
        return written

    def _record_promotion(
        self,
        job: Any,
        written: Sequence[dict[str, Any]],
        *,
        actor: str,
        origin_path: str | None,
    ) -> None:
        """Record which finding became which grant (#188).

        A grant that appears in config with no record of who promoted it is worse than a
        hand-written one, because a hand-written one has a git history. A failed write is logged
        and not raised: the grant is already in config, and reporting a failure here would make
        an operator promote it twice.
        """
        if self._audit is None or not written:
            return
        for grant in written:
            try:
                self._audit.record(
                    decision="grant_promoted",
                    capability_class=_PROMOTABLE_CLASS,
                    execution_context="interactive",
                    tool="commissioning",
                    session_id=job.payload.session_key,
                    actor=actor,
                    approval_path=origin_path,
                    scope="host",
                    hosts=list(grant["hosts"]),
                    command=grant["commands"][0] if grant["commands"] else None,
                    grant_id=grant["id"],
                    reason=(
                        f"promoted from the commissioning finding of automation "
                        f"{job.name!r} ({job.id})"
                    ),
                )
            except OSError as exc:
                logger.error("commissioning: the promotion record failed to write: {}", exc)


__all__ = [
    "CommissioningOperatorSurface",
    "PromotionRefusedError",
]
