"""Write the standing grant one approval implies -- nanoinfraorg/nanoinfra#219.

Every approval is a fresh decision, including the fifth identical one. ``gates.standingGrants``
already exists for exactly that, and it is the only unattended allow path, but writing one means
leaving the inbox, opening ``config.json``, and typing the resolved command by hand while the
action waits. This module is the other half of one click.

**One click, two effects, in two processes.** The decision crosses into the executor exactly as
before, digest and all, because the executor owns the decision. The grant is *config*, and the
confined executor must not write config: ``nanoinfra/gates/confinement.py`` gives it
``FS_READ_FILE`` on ``config.json`` and nothing more, because config is the operator's authority
and the executor is the thing being constrained by it. So the write happens here, in the gateway
process that already owns config writes, and ``tests/gates/test_derived_grants.py`` asserts that
no executor module reaches this file.

**The grant is derived from the executor-rendered action.** ``action_from_rendered_prompt`` reads
the command and the host list back out of the payload bytes the executor rendered, and re-renders
them to prove the text is one that renderer produced. Nothing the browser supplied reaches the
grant. A grant built from a request field would be a way to widen authority by editing a request,
and the exact-string data model would stop protecting anything.

**A derived grant cannot be wider than the action it came from.** The command is the exact string
the executor rendered, the hosts are the addresses the resolver produced, and the context is the
one the action ran in. There is no scope to choose, so there is no scope to get wrong. The cost is
that a different flag is a different command, so an operator presses *add* more than once for what
feels like one action. That is the model working: the alternative is a pattern language, which the
gate refuses on purpose.

**The write must never block the approved action.** Every failure here returns a
:class:`GrantWriteResult` with ``ok=False`` and a sentence, and this module raises nothing at all.
An approval that failed because a convenience feature failed would be the worst possible trade, so
a read-only config costs the grant and never the action.

**A grant expires by default.** ``never`` is a real choice an operator may want, and it is the one
option a click makes permanent, so it needs an explicit acknowledgement (#220 asks once more in the
UI). This module refuses a permanent grant that does not carry one, rather than infer consent from
a duration string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanoinfra.agent.tools.capabilities import MUTATE_REMOTE
from nanoinfra.agent.tools.context import EXECUTION_CONTEXT_INTERACTIVE
from nanoinfra.config.gates import StandingGrant
from nanoinfra.gates.audit import DECISION_GRANT_WRITTEN
from nanoinfra.gates.prompt import PromptRenderError, action_from_rendered_prompt

if TYPE_CHECKING:
    from nanoinfra.gates.executor.operator_socket import PendingView

# How long a derived grant lives, by the name the inbox sends. ``never`` is the absent expiry the
# schema already means by an absent ``expiresAt``, so it is ``None`` rather than a far-off date.
#
# The set is closed on purpose. A free-form duration on this path would let one request ask for a
# century, and the three values are the three the operator surface offers.
GRANT_EXPIRY_CHOICES: dict[str, timedelta | None] = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "never": None,
}

# The value that needs the second click. It is named rather than inferred from ``None``, so the
# rule reads the same here and in the surface.
PERMANENT_CHOICE = "never"

# What each choice is called in the note the writer leaves. An operator reads the duration they
# chose beside the date it produced, which is what a file read six months later needs.
_EXPIRY_WORDS = {"24h": "24 hours", "7d": "7 days", PERMANENT_CHOICE: "never"}

# The id slug takes the first token of the command and nothing else, and this is a secret rule
# rather than a brevity rule. The grant id travels into the audit log, where
# ``gates.audit.recordCommandText`` is false by default precisely because a resolved command
# routinely embeds a secret. A slug of three tokens would carry `myapp deploy <token>` into that
# log and defeat the opt-in. The first token is the program name, and a program name is not a
# credential.
_SLUG_ALLOWED = re.compile(r"[^a-z0-9]+")
_SLUG_MAX_CHARS = 24

# Enough of the approval id to keep two grants apart, and not the whole hex. Two approvals of the
# same command on the same day would otherwise share an id, and #16 names the grant that matched:
# one id for two lines makes that record ambiguous about which line applied.
_APPROVAL_ID_CHARS = 6


class GrantRequestError(ValueError):
    """The answer asked for a grant this module cannot build from the request itself.

    A client fault, and not a write failure: an unknown duration, or a permanent grant with no
    acknowledgement. The route answers 400 and nothing happens at all -- no decision and no
    grant -- because dropping the operator's intent silently is worse than making them click
    again. The write failures are the other case, and those never touch the approval.
    """


@dataclass(frozen=True, slots=True)
class GrantWriteResult:
    """What became of the grant, as the inbox reports it.

    ``ok=False`` with a ``reason`` is a normal outcome and never an error. The action was still
    approved, and the screen says the grant was not saved.
    """

    ok: bool
    grant_id: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None

    def as_payload(self) -> dict[str, Any]:
        """The JSON the answer route carries back."""
        return {
            "ok": self.ok,
            "id": self.grant_id,
            "expiresAt": None if self.expires_at is None else self.expires_at.isoformat(),
            "reason": self.reason,
        }


def grant_from_approved_action(
    view: PendingView,
    *,
    expires: str,
    permanent_acknowledged: bool = False,
    actor: str,
    now: datetime | None = None,
) -> StandingGrant:
    """Build the grant one approved action implies, from the bytes the executor rendered.

    Raises :class:`GrantRequestError` for a duration this module does not offer, and for a
    permanent grant with no acknowledgement. Raises :class:`PromptRenderError` when the payload
    is not a text the approval renderer produced, which means there is no action to derive from.
    """
    if expires not in GRANT_EXPIRY_CHOICES:
        raise GrantRequestError(
            f"a derived grant expires in one of {sorted(GRANT_EXPIRY_CHOICES)}, not {expires!r}"
        )
    if expires == PERMANENT_CHOICE and not permanent_acknowledged:
        raise GrantRequestError(
            "a grant that never expires needs an explicit acknowledgement, so the record says "
            "the operator chose permanent rather than inheriting it from a default"
        )

    action = action_from_rendered_prompt(view["payload"])
    if action.target_digest != view["target_digest"]:
        # The executor's own two fields disagree, so this process cannot tell which one describes
        # the action. It approves nothing here -- the approval already went through -- and it
        # writes no grant either.
        raise PromptRenderError(
            "The payload and the binding digest of this suspended action describe different "
            "bytes, so no grant can be derived from either one."
        )

    moment = now or datetime.now(UTC)
    delta = GRANT_EXPIRY_CHOICES[expires]
    expires_at = None if delta is None else moment + delta
    context = (
        "interactive"
        if view["execution_context"] == EXECUTION_CONTEXT_INTERACTIVE
        else "unattended"
    )
    return StandingGrant(
        id=_grant_id(command=action.command, request_id=view["request_id"], now=moment),
        # The context the action ran in, and not both. A grant cannot be wider than the approval
        # it came from, and an interactive approval says nothing about an unattended turn.
        contexts=[context],
        hosts=list(action.hosts),
        commands=[action.command],
        expires_at=expires_at,
        note=_note(
            expires=expires,
            expires_at=expires_at,
            actor=actor,
            request_id=view["request_id"],
            now=moment,
        ),
    )


def write_derived_grant(
    view: PendingView,
    *,
    expires: str,
    permanent_acknowledged: bool = False,
    actor: str,
    approval_path: str,
    now: datetime | None = None,
    audit: Any = None,
) -> GrantWriteResult:
    """Append the derived grant to config, and record which approval wrote it.

    The caller sends the decision to the executor **first** and calls this afterwards, so the
    action is already released when the write runs. Nothing raised here reaches the approval:
    every failure becomes a result the inbox renders.

    ``audit`` takes an :class:`~nanoinfra.gates.audit.AuditStore` for a test. Production passes
    nothing and this function opens the store the same way ``nanoinfra/diagrams/write_gate.py``
    does, because the gateway builds its runtime before this surface exists and no operator
    surface should have to be handed one to record what it did.
    """
    try:
        grant = grant_from_approved_action(
            view,
            expires=expires,
            permanent_acknowledged=permanent_acknowledged,
            actor=actor,
            now=now,
        )
    except PromptRenderError as exc:
        logger.warning("gates: no grant derived from approval {}: {}", view["request_id"], exc)
        return GrantWriteResult(
            ok=False,
            reason=(
                "The executor's payload is not a text the approval renderer produced, so no "
                "grant was derived from it."
            ),
        )
    except GrantRequestError as exc:
        # The operator surface refuses these before it sends the decision, so reaching this is a
        # caller that skipped that check. It still may not raise: the approval already landed.
        logger.warning("gates: the grant request for {} was refused: {}", view["request_id"], exc)
        return GrantWriteResult(ok=False, reason=f"The grant was not saved: {exc}")

    covered = _already_covered(grant)
    if covered is not None:
        # Nothing written, and not a failure: the action is approved and a grant that covers it
        # already exists. Reported so the inbox says which one, because "added" for a row that
        # was not added is the kind of message that makes an operator click again.
        logger.info("gates: {} already covers this action; no grant added", covered)
        return GrantWriteResult(
            ok=True,
            grant_id=covered,
            reason=f"{covered} already covers this action, so no second grant was added.",
        )

    try:
        _append_to_config(grant)
    except Exception as exc:  # noqa: BLE001 -- a convenience feature must not fail an approval
        # A read-only config is the expected case, and it is a deployment choice rather than a
        # fault. Every other failure lands here too, because the one thing this function may not
        # do is turn a config problem into a failed approval.
        logger.warning("gates: the derived grant for {} was not saved: {}", grant.id, exc)
        return GrantWriteResult(
            ok=False,
            grant_id=grant.id,
            expires_at=grant.expires_at,
            reason=f"The grant was not saved: {exc}",
        )

    _record_grant(
        view,
        grant=grant,
        actor=actor,
        approval_path=approval_path,
        expires=expires,
        audit=audit,
    )
    return GrantWriteResult(ok=True, grant_id=grant.id, expires_at=grant.expires_at)


def _already_covered(grant: StandingGrant) -> str | None:
    """The id of an existing grant that covers this one exactly, or ``None``.

    **Not** a merge and not an edit: nothing an operator wrote is touched. This only declines to
    append a row whose meaning is already on file, which is a different act from rewriting one --
    and the difference matters, because without it every approval of a slightly different command
    leaves another permanent row and the list grows without bound.

    Coverage has to be *at least as broad*, not merely equal, in one respect: an operator who held
    a grant until tomorrow and has now chosen "never" is asking for something the old row does not
    give, so that one is still written. The reverse -- an unexpiring row already on file -- covers
    a narrower request completely.
    """
    from nanoinfra.config.loader import load_config

    try:
        grants = load_config().gates.standing_grants
    except Exception as exc:  # noqa: BLE001 -- a convenience check may not fail an approval
        # An unreadable config is reported by the append below, which is the path that owns that
        # failure. Answering "not covered" here keeps this check out of that story entirely.
        logger.warning("gates: could not check existing grants: {}", exc)
        return None

    for existing in grants:
        if sorted(existing.commands) != sorted(grant.commands):
            continue
        if sorted(existing.hosts) != sorted(grant.hosts):
            continue
        if sorted(existing.contexts) != sorted(grant.contexts):
            continue
        if existing.expires_at is None:
            return existing.id
        if grant.expires_at is not None and existing.expires_at >= grant.expires_at:
            return existing.id
    return None


def _append_to_config(grant: StandingGrant) -> None:
    """Add one grant to ``gates.standingGrants`` and save.

    An append, and never an edit of an existing line. Two approvals of the same action leave two
    lines, and both mean the same thing; the alternative is an application that rewrites the
    operator's rows, and a file that edits itself is not the authority this design says it is.

    The read-modify-write is not locked. One gateway process owns config writes and answers this
    route, so two clicks are two sequential requests. A second writer outside this process is the
    same race ``Settings -> Security`` already has, and it is the operator's own two windows.
    """
    from nanoinfra.config.loader import load_config, save_config

    config = load_config()
    config.gates.standing_grants.append(grant)
    save_config(config)


def _record_grant(
    view: PendingView,
    *,
    grant: StandingGrant,
    actor: str,
    approval_path: str,
    expires: str,
    audit: Any,
) -> None:
    """Record which approval wrote which grant, and swallow a write failure.

    The grant is already in config, so a failed record must not read as a failed write. It is
    logged instead: the record is the thing at risk here, and the caller has already reported
    success to the operator.

    ``approval_id`` is the request id of the approval, which is how a reader pairs this row with
    the decision record the executor wrote for the same action. ``command`` reaches the store as
    text, and the store digests it: the full text lands only under
    ``gates.audit.recordCommandText``, so this row carries no more than the decision row does.
    """
    store = audit if audit is not None else _default_audit_store()
    if store is None:
        return
    try:
        store.record(
            decision=DECISION_GRANT_WRITTEN,
            capability_class=MUTATE_REMOTE,
            execution_context=view["execution_context"],
            session_id=view["session_id"],
            tool="execute_on_server",
            origin_path=view["origin_path"],
            origin_actor=view["origin_actor"],
            # Which agent's action produced this grant (#251). A grant an operator wrote for a
            # peer's command is a grant that peer's next run will match, so the row names it.
            acting_agent=view["acting_agent"],
            delegated_by=view["delegated_by"],
            approval_path=approval_path,
            actor=actor,
            scope=view["scope"],
            hosts=list(view["hosts"]),
            command=grant.commands[0] if grant.commands else None,
            grant_id=grant.id,
            approval_id=view["request_id"],
            reason=grant.note,
        )
    except OSError as exc:
        logger.error("gates: could not record the derived grant {}: {}", grant.id, exc)


def _default_audit_store() -> Any:
    """Open the audit store this deployment writes to, or ``None`` when that fails."""
    try:
        from nanoinfra.config.paths import get_data_dir
        from nanoinfra.gates.audit import AuditStore
        from nanoinfra.gates.policy import load_policy

        return AuditStore(get_data_dir() / "gates", config=load_policy().audit)
    except Exception as exc:  # noqa: BLE001 -- no store must not undo a written grant
        logger.error("gates: no audit store for the derived grant record: {}", exc)
        return None


def _grant_id(*, command: str, request_id: str, now: datetime) -> str:
    """Name the grant after the approval that created it.

    "Why does this grant exist?" then answers with an incident instead of a shrug: the date, the
    program the operator approved, and the approval's own id.
    """
    first_token = command.split("\n", 1)[0].strip().split(" ", 1)[0]
    slug = _SLUG_ALLOWED.sub("-", first_token.lower()).strip("-")[:_SLUG_MAX_CHARS]
    day = now.astimezone(UTC).strftime("%Y-%m-%d")
    suffix = request_id[:_APPROVAL_ID_CHARS]
    return f"approval-{day}-{slug}-{suffix}" if slug else f"approval-{day}-{suffix}"


def _note(
    *,
    expires: str,
    expires_at: datetime | None,
    actor: str,
    request_id: str,
    now: datetime,
) -> str:
    """The sentence a human reads in config.json.

    ``expiresAt`` is absolute, because a reader six months later needs a date rather than a
    subtraction. This note is where the duration the operator chose survives beside it, and it
    is a field rather than a comment because config.json is JSON.
    """
    chosen = _EXPIRY_WORDS.get(expires, expires)
    when = "never expires" if expires_at is None else f"expires {expires_at.isoformat()}"
    return (
        f"Added by approve and add on {now.astimezone(UTC).strftime('%Y-%m-%d')}: "
        f"{actor} approved {request_id} and chose {chosen}, so this grant {when}."
    )


__all__ = [
    "GRANT_EXPIRY_CHOICES",
    "PERMANENT_CHOICE",
    "GrantRequestError",
    "GrantWriteResult",
    "grant_from_approved_action",
    "write_derived_grant",
]
