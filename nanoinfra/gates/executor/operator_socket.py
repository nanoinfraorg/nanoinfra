"""The operator surface for one pending approval -- nanoinfraorg/nanoinfra#38.

The operator answers over a **second Unix socket**, and never over the execute socket. The
agent holds the execute socket. An answer accepted there would let a compromised agent approve
its own action, and the whole approval would then be a formality inside one address space.

So the executor owns two sockets. It accepts requests on one and answers on the other.

**The honest limit.** The operator socket carries mode 0660 and an operator group, so an
account outside that group cannot connect. A single-uid install has no such separation: the
agent runs as the same account, so the file mode protects nothing there. The split then rests
on two weaker facts. The first is the process boundary, because the answer arrives through a
listener the agent does not own. The second is the asserted operator identity, which the
executor matches against ``gates.approvers`` from git-reviewed config. State this plainly in a
deployment review. An operator who reads "two sockets" as "two accounts" is wrong.

``entrypoint.sh`` closes the gap on a root start. It creates the operator group, hands this
socket's directory to the executor with that group, and keeps the agent's group off it. The
executor cannot do that itself, because it holds no privilege to create a group.

**The wire.** Three verbs, one frame each, length prefixed like the execute wire.

- ``pending`` lists the actions that wait. The reply carries the rendered payload of each one.
- ``approve`` answers one action. It carries the actor, the path, and the digest of the bytes
  the operator read.
- ``deny`` answers one action. It carries the actor, the path, and a reason.

The digest on ``approve`` is not decoration. It proves the answer describes the payload the
executor rendered. Without it an operator surface could show one action and approve another.

The decoder is strict, for the reason the execute wire gives. An unknown field is an ambiguity
between two peers, and an ambiguity in this position is a hole.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypedDict, cast

from loguru import logger

from nanoinfra.agent.tools.capabilities import command_digest
from nanoinfra.gates.approvals import check_approval
from nanoinfra.gates.executor.protocol import (
    ProtocolError,
    read_frame,
    write_frame,
)
from nanoinfra.gates.pending import AnswerRefusal, PendingApproval, PendingApprovalStore
from nanoinfra.gates.policy import load_policy
from nanoinfra.gates.tokens import ApprovalTokenStore

if TYPE_CHECKING:
    from nanoinfra.config.gates import GatesConfig

# Version 2 carries ``origin_actor`` on the pending view (#47, item 11). The field set changed,
# so the version rose. The rule is the one the execute wire sets: a peer that shares the version
# shares the field set. A tolerated difference between two peers is an ambiguity, and an ambiguity
# in this position is a hole. The two peers ship in one package, so no rolling deploy needs the
# older frame.
OPERATOR_PROTOCOL_VERSION = 2

OP_PENDING = "pending"
OP_APPROVE = "approve"
OP_DENY = "deny"

# Where the executor listens for answers when nothing names a path. The name sits in a
# directory of its own, one level under the execute socket's directory. A two-uid deployment
# gives that directory the operator group, and the agent's group reaches the execute socket
# only. A sibling name in one shared directory would inherit the agent's group through the
# setgid bit, and the agent could then answer.
#
# The file name carries the execute socket's own stem. Two executors can share one run
# directory, because the SDK names each execute socket after its process (#21). A fixed name
# would let the second executor unlink the first one's operator socket, and the first one would
# then hold a socket that no operator surface can reach.
DEFAULT_OPERATOR_DIR_NAME = "operator"
OPERATOR_SOCKET_SUFFIX = ".op.sock"

# The environment variable a deployment uses to place this socket somewhere else. entrypoint.sh
# sets it before it starts the executor.
OPERATOR_SOCKET_ENV = "NANOINFRA_OPERATOR_SOCKET"

# Owner and group read and write, and nothing for anybody else. A connect() needs write rights
# on the socket file, so 0640 would refuse the operator surface.
_SOCKET_MODE = 0o660
# The executor creates its own directory private, and a root start opens it to the operator
# group afterwards. Private first is the fail-closed order.
_SOCKET_DIR_MODE = 0o700

# Which fields each verb needs, and which it may add. A missing field and an extra field both
# refuse, so the two peers always agree about one frame.
_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    OP_PENDING: frozenset(),
    OP_APPROVE: frozenset({"request_id", "actor", "approval_path", "target_digest"}),
    OP_DENY: frozenset({"request_id", "actor", "approval_path"}),
}
_OPTIONAL_FIELDS: dict[str, frozenset[str]] = {
    OP_PENDING: frozenset(),
    OP_APPROVE: frozenset(),
    OP_DENY: frozenset({"reason"}),
}

# What the audit log calls an answer that did not count. It is not ``denied``, on purpose. #32
# rebuilds a latch from a ``denied`` record, and a mistyped actor must not latch a session that
# a real approver can still answer.
REFUSED_ANSWER_DECISION = "approval_refused"


class OperatorUnavailableError(RuntimeError):
    """The operator socket could not be reached, or the peer dropped the connection."""


class PendingView(TypedDict):
    """One suspended action, as the wire carries it.

    ``payload`` holds the bytes #14 rendered, and ``target_digest`` binds them. An operator
    surface renders the payload and echoes the digest. It never summarises either one.

    ``origin_actor`` names the person the origin path authenticated, and it is blank when the
    channel authenticated nobody. The delivery watcher of #43 reads it to choose targets, because
    ``gates.identityIndependence`` can admit an approver on the origin path. The value is the
    agent's own assertion, so no operator surface presents it as a verified fact.
    """

    request_id: str
    session_id: str
    origin_path: str
    origin_actor: str
    execution_context: str
    capability_class: str
    scope: str
    host_count: int
    hosts: list[str]
    payload: str
    target_digest: str
    expires_in_s: float


@dataclass(frozen=True, slots=True)
class OperatorRequest:
    """One frame from an operator surface. ``op`` decides which fields carry meaning."""

    op: str
    request_id: str = ""
    actor: str = ""
    approval_path: str = ""
    target_digest: str = ""
    reason: str = ""


@dataclass(frozen=True, slots=True)
class OperatorResponse:
    """The executor's answer. ``refusal`` names the rule that refused, for a caller to branch on."""

    ok: bool
    error: str | None = None
    refusal: str | None = None
    pending: tuple[PendingView, ...] = ()


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """The outcome of one answer, before it becomes a frame."""

    ok: bool
    error: str | None = None
    refusal: str | None = None


def pending_view(approval: PendingApproval, *, now: float | None = None) -> PendingView:
    """Render one suspended action for the wire.

    ``expires_in_s`` is a remaining time and not a deadline. The two processes share no clock
    origin, because the store reads a monotonic clock.
    """
    moment = time.monotonic() if now is None else now
    return PendingView(
        request_id=approval.request_id,
        session_id=approval.session_id,
        origin_path=approval.origin_path,
        origin_actor=approval.origin_actor,
        execution_context=approval.execution_context,
        capability_class=approval.capability_class,
        scope=approval.scope,
        host_count=approval.host_count,
        hosts=list(approval.hosts),
        payload=approval.payload,
        target_digest=approval.target_digest,
        expires_in_s=max(0.0, approval.expires_at - moment),
    )


@dataclass(slots=True)
class ApprovalService:
    """The authority behind the operator socket.

    It reads the pending store, judges an answer with #13, issues a token with #12, and records
    the refused answers. It never runs a command. The executor thread that owns the suspended
    action does that, and it records the state changes of that action.

    The division matters. One writer per action keeps the record and the execution in one order,
    so an action that nothing recorded cannot run.
    """

    pending: PendingApprovalStore
    tokens: ApprovalTokenStore
    gates_loader: Callable[[], GatesConfig] = load_policy
    audit: Any = None

    def list_pending(self) -> tuple[PendingView, ...]:
        """Return every action an operator can still answer."""
        return tuple(pending_view(approval) for approval in self.pending.pending())

    def approve(
        self, *, request_id: str, actor: str, approval_path: str, target_digest: str
    ) -> AnswerResult:
        """Accept one approval, or name the rule that refused it.

        The order is fixed. The path check comes first, because an answer from the wrong path or
        the wrong identity must not learn whether its digest matched. The digest check comes
        next, so a mismatch mints no token. Issuance comes last, and the execution spends the
        token (#12 rule 1).
        """
        approval = self.pending.get(request_id)
        if approval is None:
            return AnswerResult(
                ok=False,
                refusal=AnswerRefusal.UNKNOWN_REQUEST.value,
                error=f"No pending approval matches {request_id!r}.",
            )

        check = self._checked(approval, approval_path=approval_path, actor=actor)
        if check is not None:
            return check

        if approval.target_digest != target_digest:
            self._record_refused_answer(
                approval,
                actor=actor,
                approval_path=approval_path,
                reason=(
                    "the answer carried another digest, so it describes bytes this executor "
                    "did not render."
                ),
            )
            return AnswerResult(
                ok=False,
                refusal=AnswerRefusal.DIGEST_MISMATCH.value,
                error=(
                    "This answer carries a digest of other bytes. An approval covers the "
                    "payload the executor rendered, so it authorizes nothing here."
                ),
            )

        token = self.tokens.issue(
            session_id=approval.session_id,
            actor=actor,
            origin_path=approval.origin_path,
            approval_path=approval_path,
            target_digest=approval.target_digest,
            capability_class=approval.capability_class,
            scope=approval.scope,
        )
        refusal = self.pending.approve(
            request_id=request_id,
            actor=actor,
            approval_path=approval_path,
            token_nonce=token.nonce,
            target_digest=target_digest,
        )
        if refusal is not None:
            # The action ended between the read above and this write. The token stays unspent
            # and it expires on its own, because nothing hands a nonce to a caller.
            return AnswerResult(
                ok=False, refusal=refusal.value, error=_answer_refusal_text(refusal)
            )
        return AnswerResult(ok=True)

    def deny(
        self, *, request_id: str, actor: str, approval_path: str, reason: str = ""
    ) -> AnswerResult:
        """Accept one denial, or name the rule that refused it.

        A denial needs the same identity check as an approval. A denial is terminal (#15) and it
        latches the class, so an unchecked denial would let one peer stop every action of a
        session.
        """
        approval = self.pending.get(request_id)
        if approval is None:
            return AnswerResult(
                ok=False,
                refusal=AnswerRefusal.UNKNOWN_REQUEST.value,
                error=f"No pending approval matches {request_id!r}.",
            )

        check = self._checked(approval, approval_path=approval_path, actor=actor)
        if check is not None:
            return check

        refusal = self.pending.deny(
            request_id=request_id, actor=actor, approval_path=approval_path, reason=reason
        )
        if refusal is not None:
            return AnswerResult(
                ok=False, refusal=refusal.value, error=_answer_refusal_text(refusal)
            )
        return AnswerResult(ok=True)

    def _checked(
        self, approval: PendingApproval, *, approval_path: str, actor: str
    ) -> AnswerResult | None:
        """Apply #13 to one answer. Returns None when the answer counts."""
        check = check_approval(
            gates=self.gates_loader(),
            origin_path=approval.origin_path,
            origin_actor=approval.origin_actor,
            approval_path=approval_path,
            sender=actor,
        )
        if check.ok:
            return None
        self._record_refused_answer(
            approval, actor=actor, approval_path=approval_path, reason=check.reason
        )
        return AnswerResult(
            ok=False,
            refusal=check.refusal.value if check.refusal is not None else None,
            error=f"This answer does not count: {check.reason}",
        )

    def _record_refused_answer(
        self, approval: PendingApproval, *, actor: str, approval_path: str, reason: str
    ) -> None:
        """Record an answer that did not count, and leave the action pending.

        A write failure here must not swallow the refusal. The answer already failed, and the
        action already waits, so the log line is the only thing at risk.

        The record names both people (#79). ``actor`` is the person whose answer did not count,
        and ``origin_actor`` is the person the suspended request came from. The pending record
        holds the second one, so a record that left it out would say the origin identity is
        unknown where this process knows it.
        """
        if self.audit is None:
            return
        try:
            self.audit.record(
                decision=REFUSED_ANSWER_DECISION,
                capability_class=approval.capability_class,
                execution_context=approval.execution_context,
                session_id=approval.session_id,
                tool="execute_on_server",
                origin_path=approval.origin_path,
                origin_actor=approval.origin_actor,
                approval_path=approval_path,
                actor=actor,
                scope=approval.scope,
                hosts=list(approval.hosts),
                command_digest=command_digest(approval.command),
                reason=reason,
            )
        except OSError as exc:
            logger.error("gates: could not record a refused approval answer: {}", exc)


# ------------------------------------------------------------------------------- the wire


def encode_operator_request(request: OperatorRequest) -> bytes:
    """Serialise one frame with the fields its verb uses, and no others."""
    fields = _REQUIRED_FIELDS.get(request.op)
    if fields is None:
        raise ProtocolError(f"unknown operator verb: {request.op!r}")
    payload: dict[str, Any] = {"v": OPERATOR_PROTOCOL_VERSION, "op": request.op}
    for name in sorted(fields | _OPTIONAL_FIELDS[request.op]):
        payload[name] = getattr(request, name)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_operator_request(payload: bytes) -> OperatorRequest:
    """Parse one frame, and refuse anything that does not match one verb exactly."""
    data = _frame_object(payload)
    op = data.pop("op", None)
    if not isinstance(op, str) or op not in _REQUIRED_FIELDS:
        raise ProtocolError(f"frame carries no known operator verb: {op!r}")

    required = _REQUIRED_FIELDS[op]
    allowed = required | _OPTIONAL_FIELDS[op]
    unknown = set(data) - allowed
    if unknown:
        raise ProtocolError(f"frame carries unknown field(s): {sorted(unknown)}")
    missing = required - set(data)
    if missing:
        raise ProtocolError(f"frame is missing field(s): {sorted(missing)}")
    for name, value in data.items():
        if not isinstance(value, str):
            raise ProtocolError(f"field {name!r} is not a string")
    return OperatorRequest(op=op, **cast("dict[str, str]", data))


def encode_operator_response(response: OperatorResponse) -> bytes:
    payload: dict[str, Any] = {
        "v": OPERATOR_PROTOCOL_VERSION,
        "ok": response.ok,
        "error": response.error,
        "refusal": response.refusal,
        "pending": [dict(view) for view in response.pending],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def decode_operator_response(payload: bytes) -> OperatorResponse:
    data = _frame_object(payload)
    expected = {"ok", "error", "refusal", "pending"}
    unknown = set(data) - expected
    if unknown:
        raise ProtocolError(f"frame carries unknown field(s): {sorted(unknown)}")
    missing = expected - set(data)
    if missing:
        raise ProtocolError(f"frame is missing field(s): {sorted(missing)}")
    if not isinstance(data["ok"], bool):
        raise ProtocolError("field 'ok' is not a boolean")
    raw_pending = data["pending"]
    if not isinstance(raw_pending, list):
        raise ProtocolError("field 'pending' is not a list")
    return OperatorResponse(
        ok=data["ok"],
        error=_optional_text(data["error"], "error"),
        refusal=_optional_text(data["refusal"], "refusal"),
        pending=tuple(_checked_view(entry) for entry in cast("list[object]", raw_pending)),
    )


def _frame_object(payload: bytes) -> dict[str, Any]:
    """Parse one frame into a dictionary, and check the version."""
    try:
        raw = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"frame is not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ProtocolError("frame is not an object")
    data: dict[str, Any] = cast("dict[str, Any]", raw)
    version = data.pop("v", None)
    if version is None:
        raise ProtocolError("frame carries no protocol version")
    if version != OPERATOR_PROTOCOL_VERSION:
        raise ProtocolError(
            f"protocol version {version!r} is not {OPERATOR_PROTOCOL_VERSION}"
        )
    return data


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ProtocolError(f"field {name!r} is not a string or null")


def _checked_view(entry: object) -> PendingView:
    """Validate one pending entry at the edge, so no caller receives a raw dictionary."""
    if not isinstance(entry, dict):
        raise ProtocolError("a pending entry is not an object")
    fields: dict[str, Any] = cast("dict[str, Any]", entry)
    expected = set(PendingView.__annotations__)
    if set(fields) != expected:
        raise ProtocolError(f"a pending entry carries the wrong fields: {sorted(fields)}")
    hosts = fields["hosts"]
    if not isinstance(hosts, list) or not all(
        isinstance(host, str) for host in cast("list[object]", hosts)
    ):
        raise ProtocolError("a pending entry carries a host list that is not a list of names")
    for name in ("request_id", "session_id", "origin_path", "origin_actor",
                 "execution_context", "capability_class", "scope", "payload", "target_digest"):
        if not isinstance(fields[name], str):
            raise ProtocolError(f"a pending entry carries a non-string {name!r}")
    if not isinstance(fields["host_count"], int):
        raise ProtocolError("a pending entry carries a non-integer host_count")
    if not isinstance(fields["expires_in_s"], int | float):
        raise ProtocolError("a pending entry carries a non-numeric expires_in_s")
    return cast("PendingView", fields)


# ----------------------------------------------------------------------------- the server


def default_operator_socket_path(execute_socket_path: Path | str) -> Path:
    """Where the operator socket lives beside one execute socket.

    ``NANOINFRA_OPERATOR_SOCKET`` wins when a deployment sets it. Otherwise the path is a private
    subdirectory of the execute socket's directory, and the file name carries the execute
    socket's stem.

    The default path is about twenty bytes longer than the execute path. The kernel copies a
    socket path into ``sun_path``, which holds 108 bytes on Linux, so a very deep run directory
    needs the variable above. A bind failure ends the executor and the supervisor quotes the
    reason, so that case is loud rather than silent.
    """
    override = os.environ.get(OPERATOR_SOCKET_ENV, "").strip()
    if override:
        return Path(override)
    execute = Path(execute_socket_path)
    return (
        execute.parent
        / DEFAULT_OPERATOR_DIR_NAME
        / f"{execute.name.removesuffix('.sock')}{OPERATOR_SOCKET_SUFFIX}"
    )


def bind_operator_socket(socket_path: Path | str) -> socket.socket:
    """Bind and listen, and raise when that fails.

    The caller binds in its own thread on purpose. A bind failure is a deployment fault, and it
    must reach the supervisor rather than leave an executor that suspends every action and
    answers none of them.

    The directory starts private to this account. A root start opens it to the operator group
    afterwards, so the fail-closed order holds even when that start never happens.
    """
    path = Path(socket_path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        # The mode goes on before the listen, so no peer connects through a wider mode.
        os.chmod(path, _SOCKET_MODE)
        listener.listen(8)
    except OSError:
        listener.close()
        raise
    logger.info("gates: executor operator socket listening on {} (mode {:o})", path, _SOCKET_MODE)
    return listener


def serve_operator_socket(
    listener: socket.socket, service: ApprovalService, *, max_requests: int | None = None
) -> None:
    """Answer on a bound operator socket until the listener closes.

    Each connection gets its own thread, the same as the execute socket. An operator surface
    that lists the queue must not wait behind another one.

    ``max_requests`` exists for tests. Production passes nothing.
    """
    served = 0
    while max_requests is None or served < max_requests:
        try:
            conn, _ = listener.accept()
        except OSError as exc:
            # The owner closed the listener, which is how a shutdown ends this loop.
            logger.debug("gates: operator socket stopped answering: {}", exc)
            return
        served += 1
        thread = threading.Thread(
            target=_answer_one_connection,
            args=(conn, service),
            name="nanoinfra-operator",
            daemon=True,
        )
        thread.start()


def serve_operator_forever(
    socket_path: Path | str, service: ApprovalService, *, max_requests: int | None = None
) -> None:
    """Bind the operator socket, answer, and remove the socket file on exit.

    A stale socket file blocks the next bind, and a supervisor that restarts the executor must
    not need a human to delete one.
    """
    path = Path(socket_path)
    listener = bind_operator_socket(path)
    try:
        serve_operator_socket(listener, service, max_requests=max_requests)
    finally:
        listener.close()
        with contextlib.suppress(OSError):
            path.unlink()


def _answer_one_connection(conn: socket.socket, service: ApprovalService) -> None:
    """Answer one connection. A bad frame gets a refusal, and never a crash."""
    with conn:
        try:
            request = decode_operator_request(read_frame(conn))
        except ProtocolError as exc:
            logger.warning("gates: operator socket refused a frame: {}", exc)
            with contextlib.suppress(OSError, ProtocolError):
                write_frame(
                    conn,
                    encode_operator_response(
                        OperatorResponse(ok=False, error=f"Malformed request: {exc}")
                    ),
                )
            return

        try:
            response = _dispatch(request, service)
        except Exception as exc:  # noqa: BLE001 -- one bad answer must not end the process
            logger.exception("gates: the operator socket failed a request")
            response = OperatorResponse(ok=False, error=f"The executor failed this answer: {exc}")

        with contextlib.suppress(OSError, ProtocolError):
            write_frame(conn, encode_operator_response(response))


def _dispatch(request: OperatorRequest, service: ApprovalService) -> OperatorResponse:
    """Run one verb against the service."""
    if request.op == OP_PENDING:
        return OperatorResponse(ok=True, pending=service.list_pending())
    if request.op == OP_APPROVE:
        result = service.approve(
            request_id=request.request_id,
            actor=request.actor,
            approval_path=request.approval_path,
            target_digest=request.target_digest,
        )
    else:
        result = service.deny(
            request_id=request.request_id,
            actor=request.actor,
            approval_path=request.approval_path,
            reason=request.reason,
        )
    return OperatorResponse(
        ok=result.ok,
        error=result.error,
        refusal=result.refusal,
        pending=service.list_pending(),
    )


# ----------------------------------------------------------------------------- the client


class OperatorClient:
    """One request per connection, for an operator surface such as #27's inbox."""

    def __init__(self, socket_path: Path | str, *, timeout_s: float = 10.0) -> None:
        self._socket_path = Path(socket_path)
        self._timeout_s = timeout_s

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def pending(self) -> tuple[PendingView, ...]:
        """List the actions that wait for an answer."""
        return self._send(OperatorRequest(op=OP_PENDING)).pending

    def approve(
        self, *, request_id: str, actor: str, approval_path: str, target_digest: str
    ) -> OperatorResponse:
        """Approve one action. ``target_digest`` is the digest of the payload the operator read."""
        return self._send(
            OperatorRequest(
                op=OP_APPROVE,
                request_id=request_id,
                actor=actor,
                approval_path=approval_path,
                target_digest=target_digest,
            )
        )

    def deny(
        self, *, request_id: str, actor: str, approval_path: str, reason: str = ""
    ) -> OperatorResponse:
        """Deny one action. The reason reaches the caller and the audit log."""
        return self._send(
            OperatorRequest(
                op=OP_DENY,
                request_id=request_id,
                actor=actor,
                approval_path=approval_path,
                reason=reason,
            )
        )

    def _send(self, request: OperatorRequest) -> OperatorResponse:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self._timeout_s)
                conn.connect(str(self._socket_path))
                write_frame(conn, encode_operator_request(request))
                return decode_operator_response(read_frame(conn))
        except (OSError, ProtocolError) as exc:
            raise OperatorUnavailableError(
                f"Could not reach the operator socket at {self._socket_path}: {exc}"
            ) from exc


def _answer_refusal_text(refusal: AnswerRefusal) -> str:
    """The sentence an operator reads for one store refusal."""
    if refusal is AnswerRefusal.ALREADY_ANSWERED:
        return "This action already has an answer. One action takes one answer."
    if refusal is AnswerRefusal.EXPIRED:
        return "This action expired before the answer arrived, so it already refused."
    if refusal is AnswerRefusal.DIGEST_MISMATCH:
        return "This answer carries a digest of other bytes, so it authorizes nothing."
    return "No pending approval matches this request id."


__all__ = [
    "DEFAULT_OPERATOR_DIR_NAME",
    "OPERATOR_SOCKET_SUFFIX",
    "OPERATOR_PROTOCOL_VERSION",
    "OPERATOR_SOCKET_ENV",
    "OP_APPROVE",
    "OP_DENY",
    "OP_PENDING",
    "REFUSED_ANSWER_DECISION",
    "AnswerResult",
    "ApprovalService",
    "OperatorClient",
    "OperatorRequest",
    "OperatorResponse",
    "OperatorUnavailableError",
    "PendingView",
    "bind_operator_socket",
    "decode_operator_request",
    "decode_operator_response",
    "default_operator_socket_path",
    "encode_operator_request",
    "encode_operator_response",
    "pending_view",
    "serve_operator_forever",
    "serve_operator_socket",
]
