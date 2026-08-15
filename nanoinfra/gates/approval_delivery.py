"""Delivery of one suspended action to an approver on a chat channel -- nanoinfraorg/nanoinfra#43.

#38 named this as item 3, "a delivery path", and called it per-channel plumbing. Nothing was
built, so ``webui`` stayed the only path that could answer an approval, and a deployment that
runs the WebUI alone had a **nominal** second path only. The chat arrives on ``websocket``, and
the answer arrives from the same browser with the same token. This module makes a real second
path exist.

**Why the gateway delivers, and not the executor.** The executor holds no transport. It dials a
host through a connection backend, and it holds no chat client, no bot token, and no bus. The
gateway holds both the channels and an ``OperatorClient``, so the delivery belongs there.

**Why a poll, and not a push.** The wait is at most ``gates.approvalTimeoutS``, which defaults to
120 seconds and stops at 300. A poll of a few seconds therefore spends a negligible part of the
window, and it needs no new wire between the two processes. #38 refused a poll on the *execute*
socket for four reasons that all rest on the blocked call there. None of them applies to a read
of ``pending`` on the operator socket, which is a separate connection that returns at once.

**What the approver reads.** The executor's rendering, verbatim, with the digest. Never a model
summary. #14 rendered those bytes and ``target_digest`` binds them, and a summary inside the
security path is the unfaithful-summarization problem: the human authorizes a sentence, the
executor runs a command, and nothing compares the two.

The payload travels inside a fenced block. A chat channel renders markdown, and the Telegram
renderer turns a line that holds two pipes into a drawn table. A command such as
``ps aux | grep nginx`` produces one, so an unfenced payload would reach an operator restyled.

**Who receives one request.** Every approver in ``gates.approvers`` whose path authenticates an
approver, and whose path is not the origin path of the request. A request that arrived on
Telegram therefore reaches no Telegram approver, because an answer from there cannot count
(#13 condition 3). ``allowFrom`` grants nobody a delivery and nobody an answer. It carries
reachability, and this module reads it never.

**What the channel carries.** The rendered payload and a request id. No credential plaintext, and
no token nonce. The executor resolves a ``secretRef`` after the answer, so the decrypted value
never reaches this module. The nonce never leaves the executor (#12), and the digest inside the
payload binds the bytes.

One honest limit, and it is the limit the #27 inbox already carries. The payload holds the
resolved command, and a resolved command holds a secret when an operator wrote one into it. #16
records a digest instead of the text for that reason. An approval needs the text, because a human
who cannot read the command approves nothing real, so the chat message carries it.

**The config contract.** ``gates.approvers[].sender`` is the identity the channel authenticates,
and it is also the chat this module delivers to. On Telegram that is the numeric account id: a
direct-message chat id equals the account id, and a username is user-controlled. A deployment
that listed a username would therefore reach no chat and would also answer nothing, because
``nanoinfra/command/approvals.py`` derives the same numeric id from the inbound message.

An approver must also be reachable on the channel. ``allowFrom`` or the pairing store admits the
inbound answer, and this module cannot admit it. So a deployment lists the approver twice, for
two different reasons, and the reasons must not be confused.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from loguru import logger

from nanoinfra.bus.events import OutboundMessage
from nanoinfra.gates.executor.operator_socket import (
    OperatorClient,
    OperatorUnavailableError,
    PendingView,
)
from nanoinfra.gates.policy import load_policy

if TYPE_CHECKING:
    from nanoinfra.config.gates import GatesConfig

# How long the watcher waits between two reads of the pending list. A few seconds costs a
# negligible part of a 120-second wait, and it keeps a fresh request in front of an operator
# almost at once. A shorter interval would add socket traffic for no operator benefit.
DEFAULT_POLL_INTERVAL_S = 3.0

# The path the #27 inbox answers on. The summary names it, because a deployment whose only
# approver sits there has no second path in practice.
_WEBUI_PATH = "webui"

# The fence around the payload. Three backticks are the marker every chat channel that renders
# markdown understands as "show these bytes".
_FENCE = "```"

_LEAD = "One action waits for your answer. The executor rendered the text below."


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """One approver, and the chat that reaches them.

    ``channel`` matches ``gates.approvalPaths`` and ``Approver.channel``. ``chat_id`` is the
    approver's own sender id, so the request arrives in a direct message.
    """

    channel: str
    chat_id: str


def delivery_targets(*, gates: GatesConfig, origin_path: str) -> tuple[DeliveryTarget, ...]:
    """Which approvers may answer a request that arrived on *origin_path*.

    The three conditions of #13 decide, minus the identity that has not answered yet. The
    approver comes from ``gates.approvers``, the path must be in ``gates.approvalPaths``, and
    the path must differ from the origin. Config order survives, because it tells an operator
    which path the deployment prefers.
    """
    origin = origin_path.strip()
    authenticated = {entry.strip() for entry in gates.approval_paths if entry.strip()}
    targets: list[DeliveryTarget] = []
    seen: set[tuple[str, str]] = set()
    for approver in gates.approvers:
        channel = approver.channel.strip()
        sender = approver.sender.strip()
        if not channel or not sender:
            continue
        if channel == origin or channel not in authenticated:
            continue
        if (channel, sender) in seen:
            continue
        seen.add((channel, sender))
        targets.append(DeliveryTarget(channel=channel, chat_id=sender))
    return tuple(targets)


def render_delivery(view: PendingView) -> str:
    """The message one approver reads, with the executor's payload inside a fenced block.

    The instructions follow the payload, so the first line an operator sees in a notification
    is the executor's own header. The request id sits outside the fence, because an operator
    copies it into the answer.
    """
    payload = view["payload"]
    body = payload if payload.endswith("\n") else f"{payload}\n"
    request_id = view["request_id"]
    return (
        f"{_LEAD}\n"
        f"{_FENCE}\n{body}{_FENCE}\n"
        f"Request id: {request_id}\n"
        f"This request arrived on {view['origin_path']}.\n"
        f"Time left: {int(view['expires_in_s'])}s\n"
        f"To approve, send: /approve {request_id}\n"
        f"To deny, send: /deny {request_id} <reason>\n"
    )


class ApprovalDeliveryWatcher:
    """The gateway-side poll that puts one suspended action in front of an approver.

    The watcher holds the client, so a caller cannot hand it on. It records the pairs it
    delivered, and it drops a record when the request leaves the pending list, so a long-lived
    gateway holds one entry per live request and no more.
    """

    def __init__(
        self,
        *,
        client: object,
        publish: Callable[[OutboundMessage], Awaitable[None]],
        is_channel_enabled: Callable[[str], bool],
        gates_loader: Callable[[], GatesConfig] = load_policy,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        """Take the real operator client, and refuse anything else.

        The parameter is ``object`` for the reason the two WebUI operator surfaces take one:
        the gateway that builds this is dynamically typed, so a static annotation guards
        nothing there.
        """
        if not isinstance(client, OperatorClient):
            raise TypeError("an approval delivery watcher needs the OperatorClient from the gateway")
        self._client: OperatorClient = client
        self._publish = publish
        self._is_channel_enabled = is_channel_enabled
        self._gates_loader = gates_loader
        self._interval_s = interval_s
        self._delivered: set[tuple[str, str, str]] = set()
        self._warned_channels: set[str] = set()

    async def run(self) -> None:
        """Poll until the task is cancelled.

        One failed poll must not end the loop. A gateway that lost this task would suspend
        every unusual action and deliver none of them, and the operator would read nothing.
        """
        logger.info(self.summary())
        while True:
            try:
                await self.deliver_pending()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- one bad poll must not end the loop
                logger.warning("gates: an approval delivery poll failed: {}", exc)
            await asyncio.sleep(self._interval_s)

    async def deliver_pending(self) -> int:
        """Deliver every request that no approver has read yet, and return the count.

        A read failure delivers nothing and raises nothing. The executor may be down, and the
        gateway must keep its channels up. The next poll tries again.
        """
        try:
            views = await asyncio.to_thread(self._client.pending)
        except OperatorUnavailableError as exc:
            logger.debug("gates: the approval delivery watcher could not read pending: {}", exc)
            return 0

        self._forget_answered(views)
        if not views:
            return 0

        gates = self._gates_loader()
        delivered = 0
        for view in views:
            delivered += await self._deliver_one(view, gates=gates)
        return delivered

    def summary(self) -> str:
        """One line an operator reads at start, about the second path this build gives them."""
        gates = self._gates_loader()
        authenticated = [entry.strip() for entry in gates.approval_paths if entry.strip()]
        paths = sorted(
            {
                approver.channel.strip()
                for approver in gates.approvers
                if approver.sender.strip() and approver.channel.strip() in authenticated
            }
        )
        chat_paths = [path for path in paths if path != _WEBUI_PATH]
        if not chat_paths:
            return (
                "gates: no approver sits on a chat channel. gates.approvalPaths lists "
                f"{authenticated!r}, so a suspended action reaches an operator through the "
                "WebUI inbox only."
            )
        absent = [path for path in chat_paths if not self._is_channel_enabled(path)]
        line = (
            f"gates: approval delivery polls the executor every {self._interval_s:g}s. It "
            f"delivers a suspended action to these paths: {paths!r}."
        )
        if absent:
            line += f" These channels are not enabled, so they receive nothing: {absent!r}."
        return line

    async def _deliver_one(self, view: PendingView, *, gates: GatesConfig) -> int:
        """Deliver one request to every approver who may answer it."""
        request_id = view["request_id"]
        content = render_delivery(view)
        delivered = 0
        for target in delivery_targets(gates=gates, origin_path=view["origin_path"]):
            key = (request_id, target.channel, target.chat_id)
            if key in self._delivered:
                continue
            if not self._is_channel_enabled(target.channel):
                self._warn_absent_channel(target.channel)
                continue
            try:
                await self._publish(
                    OutboundMessage(
                        channel=target.channel, chat_id=target.chat_id, content=content
                    )
                )
            except Exception as exc:  # noqa: BLE001 -- one channel must not stop another
                # The pair stays unrecorded, so the next poll tries again. A request that
                # nobody read is worse than a duplicate message.
                logger.warning(
                    "gates: could not deliver approval {} to {}: {}", request_id, target.channel, exc
                )
                continue
            self._delivered.add(key)
            delivered += 1
            logger.info(
                "gates: approval {} reached an approver on {}", request_id, target.channel
            )
        return delivered

    def _forget_answered(self, views: tuple[PendingView, ...]) -> None:
        """Drop the record of every request that left the pending list.

        A request id is a uuid, so a dropped id never returns. The set therefore holds one
        entry per live request and per target, and it cannot grow without bound.
        """
        live = {view["request_id"] for view in views}
        self._delivered = {key for key in self._delivered if key[0] in live}

    def _warn_absent_channel(self, channel: str) -> None:
        """Say once that an approver sits on a channel this gateway does not run."""
        if channel in self._warned_channels:
            return
        self._warned_channels.add(channel)
        logger.warning(
            "gates: an approver sits on {}, and that channel is not enabled. A suspended "
            "action reaches nobody there.",
            channel,
        )


__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "ApprovalDeliveryWatcher",
    "DeliveryTarget",
    "delivery_targets",
    "render_delivery",
]
