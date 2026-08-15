"""The chat commands that answer one suspended approval -- nanoinfraorg/nanoinfra#43.

#38 built the wait and the executor's operator socket. #27 built the inbox, and the inbox
answers over HTTP from the WebUI. So ``webui`` was the only path that could answer an approval.

#13 asks the answer to arrive on an authenticated path other than the origin of the request. A
deployment that runs the WebUI alone has a **nominal** second path only: the chat arrives on
``websocket``, and the answer arrives from the same browser with the same token. This module
makes a real second path exist, and Telegram is the first channel, because a homelab operator
already holds an authenticated account there.

**The answer is a slash command, and it runs in the priority tier.** ``AgentLoop.run`` answers a
priority command before every other branch, so the text never reaches a model turn and never
reaches the transcript. The model therefore cannot read an answer and cannot write one. A tool
would be the opposite of this: a tool is a thing the model calls.

**The actor comes from the channel, and from the half of it the channel authenticated.** No
argument of the command names an actor, and no argument names a path. ``sender_id`` is a routing
label, and it is proof of a person only on a channel that authenticates it: Telegram does, and the
WebSocket channel reads that value from a query parameter the browser chooses (#81). So a channel
that verifies an identity sets ``InboundMessage.authenticated_sender``, this module prefers it, and a
channel that proves nobody answers no approval at all. The executor matches that identity against ``gates.approvers`` on the
channel that carried the answer, so a sender the config does not name answers nothing.

A channel ``allowFrom`` list and the pairing store grant nothing here. Both carry reachability,
and the pairing store is mutable at runtime from chat. An approver who is unreachable also
cannot answer, because the channel drops the message before the bus, so a deployment needs the
approver on both lists for two different reasons.

**A denial costs one socket call, and an approval costs two.** The approval reads the pending
view first, because the executor binds an approval to the digest of the payload it rendered. A
denial carries no digest, because a denial stops an action and authorizes no bytes. So a refusal
can never cost more steps than an approval, which is the rule #27 applies to its two routes.

**What the digest echo proves here.** The operator types a request id, and this module reads the
digest from the executor's own pending view. A pending record is frozen and its id is a uuid, so
the re-read proves the executor still holds those bytes in that state. It does not prove that the
operator read them, which is the property the WebUI's echo of a rendered card carries. The
request id in the delivered message is the link between the two, and the delivery carries the
payload verbatim (``nanoinfra/gates/approval_delivery.py``).

**Residual risk, stated rather than hidden.** These commands run inside the gateway process,
which runs as the agent's account, so the file mode on the operator socket protects nothing on
this path. Three facts carry the rest. The answer crosses a process boundary into the executor,
which owns the decision. The executor matches the actor against ``gates.approvers`` from
git-reviewed config. An AST import closure asserts that no module under ``nanoinfra/agent/tools/``
reaches this module, and ``tests/command/test_approval_commands.py`` walks that graph. A tool that
runs arbitrary code inside the gateway defeats the closure, and the approver match is then the
last rule that holds.

**A second channel is a small addition.** Add the two commands to the channel's forwarded-command
set, add an entry to ``_SENDER_RULES`` when the channel decorates its sender id, and add the
channel name to ``gates.approvalPaths``. This module needs no other change.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Callable

from loguru import logger

from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.gates.executor.operator_socket import (
    OperatorClient,
    OperatorUnavailableError,
)

if TYPE_CHECKING:
    from nanoinfra.command.router import CommandContext, CommandRouter

APPROVE_COMMAND = "/approve"
DENY_COMMAND = "/deny"

# The prefix tiers carry the trailing space, the way every other argument command does.
APPROVE_PREFIX = f"{APPROVE_COMMAND} "
DENY_PREFIX = f"{DENY_COMMAND} "

# The reason reaches the audit log, and a record is one line an operator has to read.
_MAX_REASON_CHARS = 500

# A request id is a uuid hex today. The cap bounds the size and not the format, because the
# executor owns the set of ids that exist and this module must not guess at it. A token above
# the cap refuses, and it never truncates.
_MAX_REQUEST_ID_CHARS = 128

# The two forms, for an operator who sent a bare command.
_USAGE = (
    f"Send {APPROVE_COMMAND} <request-id> to approve one action. "
    f"Send {DENY_COMMAND} <request-id> <reason> to refuse it."
)

# An instruction per refusal name, beside the executor's own sentence. #27 maps the same names
# for the same reason: an operator who reads a rule can act, and an operator who reads "denied"
# files a bug. A name that is absent here leaves the executor's sentence on its own.
_REFUSAL_INSTRUCTIONS: dict[str, str] = {
    "same_path": "Answer this request from another authenticated path.",
    "not_an_approver": "An operator adds an approver to gates.approvers in git-reviewed config.",
    "unauthenticated_path": "An operator adds this path to gates.approvalPaths.",
    "no_second_path": "An operator adds a second path, or declares a standing grant.",
    "digest_mismatch": "Read the request again, and answer the payload the executor rendered.",
    "already_answered": "One action takes one answer.",
    "expired": "Ask for the action again while an approver is present.",
}


def _telegram_actor(sender_id: str) -> str:
    """The Telegram account id, without the username the channel appends.

    ``TelegramChannel._sender_id`` builds ``<account-id>|<username>`` so its own allowlist can
    match either half. A username is user-controlled and it changes at will, so an approver
    identity must not rest on one. The account id is the half Telegram authenticates.
    """
    account, separator, _username = sender_id.partition("|")
    return account if separator else sender_id


# Which channels decorate their sender id, and how to read the authenticated half back out.
# A channel that is absent here carries its sender id unchanged.
_SENDER_RULES: dict[str, Callable[[str], str]] = {"telegram": _telegram_actor}


#: Which channels prove no identity through ``sender_id`` (#81). The WebSocket channel reads
#: that value from a query parameter the browser chooses, and it falls back to ``anon-<uuid>``,
#: so it is a routing label and never proof of a person. Such a channel answers an approval
#: only through ``authenticated_sender``, which the channel sets from a value it verified.
_SENDER_ID_AUTHENTICATES_NOBODY = frozenset({"websocket"})


def approval_actor(message: InboundMessage) -> str:
    """The identity ``gates.approvers`` names for one inbound message.

    The value is what the channel **authenticated**, and a deployment lists that exact string,
    because ``check_approval`` compares it exactly (#13).

    Two sources, in one order. ``authenticated_sender`` wins whenever a channel sets it, because
    a channel that sets it is telling us which of its two values it verified. Otherwise
    ``sender_id`` answers, because Telegram authenticates its numeric account id and most
    channels are that shape.

    A channel in ``_SENDER_ID_AUTHENTICATES_NOBODY`` with no authenticated sender answers the
    empty string, which matches no approver. The empty answer is the refusal: a caller reads it
    and sends the operator to a surface that does work, rather than comparing a label the client
    chose against an authority list.
    """
    channel = message.channel.strip()
    verified = (message.authenticated_sender or "").strip()
    if verified:
        rule = _SENDER_RULES.get(channel)
        return rule(verified) if rule is not None else verified
    if channel in _SENDER_ID_AUTHENTICATES_NOBODY:
        return ""
    rule = _SENDER_RULES.get(channel)
    sender = message.sender_id.strip()
    return rule(sender) if rule is not None else sender


def unauthenticated_channel_refusal(channel: str) -> str:
    """What an operator reads when their channel proves no identity (#81).

    A refusal that said only "denied" would send an operator to file a bug about a rule that is
    protecting them. This one names the surface that works: the WebUI reads the identity its own
    handshake verified, and #27 answers there.
    """
    return (
        f"The {channel!r} channel authenticates no person for an approval, so this command "
        "answers nothing. Answer from the Approvals inbox in the WebUI, which reads the identity "
        "the gateway verified, or from a channel that authenticates its sender."
    )


class ApprovalAnswerSurface:
    """The answer half of one suspended action, behind two methods and nothing else.

    A router holds this object. It must not be able to hand the client on, so the client stays
    private and no accessor returns it.
    """

    def __init__(self, *, client: object) -> None:
        """Take the real operator client, and refuse anything else.

        The parameter is ``object`` for the reason ``ApprovalsOperatorSurface`` takes one: a
        chat message carries strings, and this check makes those values fail at the door.
        """
        if not isinstance(client, OperatorClient):
            raise TypeError("an approval answer surface needs the OperatorClient from the gateway")
        self._client: OperatorClient = client

    async def approve(self, *, message: InboundMessage, request_id: str) -> str:
        """Approve one action, and return the sentence the operator reads.

        The digest comes from the executor's own pending view, and never from the message. An
        operator who had to retype a 64-character digest on a phone would deny by default, and
        a denial that costs less than an approval is the wrong incentive.
        """
        channel = message.channel.strip()
        actor = approval_actor(message)
        if not actor:
            # The channel proved no identity (#81). Refuse here rather than send a label the
            # client chose to the executor, which would compare it against the approver list.
            return unauthenticated_channel_refusal(channel)
        try:
            views = await asyncio.to_thread(self._client.pending)
        except OperatorUnavailableError as exc:
            return _unreachable(request_id, exc)

        match = next((view for view in views if view["request_id"] == request_id), None)
        if match is None:
            # ``pending`` lists the actions an operator can still answer. An id that is absent
            # from it never existed, or it already ended.
            return (
                f"No action waits under {request_id!r}. It expired, or an operator already "
                "answered it."
            )

        try:
            response = await asyncio.to_thread(
                self._client.approve,
                request_id=request_id,
                actor=actor,
                approval_path=channel,
                target_digest=match["target_digest"],
            )
        except OperatorUnavailableError as exc:
            return _unreachable(request_id, exc)

        if response.ok:
            logger.info("gates: {} approved {} on {}", actor, request_id, channel)
            return f"Approved {request_id!r}. The executor runs the action now."
        return _refused(response.refusal, response.error)

    async def deny(
        self, *, message: InboundMessage, request_id: str, reason: str = ""
    ) -> str:
        """Deny one action, and return the sentence the operator reads.

        One socket call, and one field fewer than an approval. A denial is terminal (#15), so
        the executor applies the same identity check and the same path check.
        """
        channel = message.channel.strip()
        actor = approval_actor(message)
        if not actor:
            # The channel proved no identity (#81). Refuse here rather than send a label the
            # client chose to the executor, which would compare it against the approver list.
            return unauthenticated_channel_refusal(channel)
        try:
            response = await asyncio.to_thread(
                self._client.deny,
                request_id=request_id,
                actor=actor,
                approval_path=channel,
                reason=reason[:_MAX_REASON_CHARS],
            )
        except OperatorUnavailableError as exc:
            return _unreachable(request_id, exc)

        if response.ok:
            logger.info("gates: {} denied {} on {}", actor, request_id, channel)
            return f"Denied {request_id!r}. The executor refuses the action."
        return _refused(response.refusal, response.error)


def register_approval_commands(router: CommandRouter, *, surface: ApprovalAnswerSurface) -> None:
    """Put both commands in the priority tiers of *router*.

    The bare form goes in the exact tier beside the prefix form. A bare ``/approve`` that fell
    through to the dispatch tier would become a model turn, and the model would then read the
    word.
    """

    async def handle_approve(ctx: CommandContext) -> OutboundMessage | None:
        request_id, extra = _split_id(ctx.args)
        if not _usable_id(request_id) or extra:
            # Extra text on an approval refuses rather than silently drops. A field this
            # module ignored would be a field an operator believes they sent.
            return _reply(ctx, _USAGE)
        return _reply(
            ctx,
            await surface.approve(message=ctx.msg, request_id=request_id),
        )

    async def handle_deny(ctx: CommandContext) -> OutboundMessage | None:
        request_id, reason = _split_id(ctx.args)
        if not _usable_id(request_id):
            return _reply(ctx, _USAGE)
        return _reply(
            ctx,
            await surface.deny(message=ctx.msg, request_id=request_id, reason=reason),
        )

    router.priority(APPROVE_COMMAND, handle_approve)
    router.priority(DENY_COMMAND, handle_deny)
    router.priority_prefix(APPROVE_PREFIX, handle_approve)
    router.priority_prefix(DENY_PREFIX, handle_deny)


def _split_id(args: str) -> tuple[str, str]:
    """Read the request id and the rest of the text out of one argument string."""
    request_id, _separator, rest = args.strip().partition(" ")
    return request_id, rest.strip()


def _usable_id(request_id: str) -> bool:
    """Say whether one token can name a request at all.

    The cap refuses rather than truncates. A truncated id would name another request, or no
    request, and an operator would read an answer about bytes they never sent.
    """
    return bool(request_id) and len(request_id) <= _MAX_REQUEST_ID_CHARS


def _reply(ctx: CommandContext, content: str) -> OutboundMessage:
    """Answer on the path the message arrived on, and never in the transcript."""
    return OutboundMessage(channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=content)


def _unreachable(request_id: str, exc: OperatorUnavailableError) -> str:
    """The sentence for an answer that never arrived.

    An answer the executor never saw must not read as a refusal the executor issued. The
    action still waits in that case, and the operator can answer it again.
    """
    logger.warning("gates: a chat approval answer could not reach the executor: {}", exc)
    return (
        f"The executor is not reachable, so nothing answered {request_id!r}. "
        "Send the command again, or answer from the WebUI."
    )


def _refused(refusal: str | None, error: str | None) -> str:
    """The sentence for an answer the executor refused.

    The executor's own words come first, because they name the identity and the path that
    failed. The instruction follows, and an unknown refusal name leaves the words on their own.
    """
    sentence = error or "This answer does not count."
    instruction = _REFUSAL_INSTRUCTIONS.get(refusal or "")
    return f"{sentence} {instruction}" if instruction else sentence


__all__ = [
    "APPROVE_COMMAND",
    "APPROVE_PREFIX",
    "DENY_COMMAND",
    "DENY_PREFIX",
    "ApprovalAnswerSurface",
    "approval_actor",
    "unauthenticated_channel_refusal",
    "register_approval_commands",
]
