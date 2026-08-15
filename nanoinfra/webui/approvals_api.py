"""The operator inbox that answers one suspended action -- nanoinfraorg/nanoinfra#27.

#38 built the wait. An ``approve`` decision suspends the action inside the executor, a pending
store holds it, and an operator answers on a **second Unix socket**. Nothing answered on that
socket, so every approve decision waited for ``gates.approvalTimeoutS`` and then refused. This
module is the half that answers.

**What the surface may render.** ``payload`` holds the bytes #14 rendered from resolver output,
and ``target_digest`` binds them. This module passes both through unchanged, and it builds no
summary of its own. A model-written summary inside the security path is the unfaithful
summarization problem: the human authorizes a sentence, the executor runs a command, and nothing
compares the two. The digest travels back on the approve, so a mismatch refuses and leaves the
action pending.

**Where the actor comes from.** ``operator_actor`` in ``nanoinfra/webui/latch_api.py`` names the
operator from the server-side session. A browser that names itself in the request body gains
nothing, because ``answer`` takes the actor as a keyword and ignores every identity field in the
values. The value is ``webui`` for a bare-token deployment, and ``webui:<identity>`` when a
trusted proxy asserts a name. ``gates.approvers`` matches that exact string, so a deployment
behind Cloudflare Access lists ``webui:ops@example.com`` rather than ``ops@example.com``.

**Residual risk, and it is deliberate.** The WebUI answers on the operator socket from inside the
gateway process. #38 gives that socket mode 0660 and an operator group, and the agent runs as the
same account here, so the file mode protects nothing against the agent. Two facts remain. The
answer still crosses a process boundary into the executor, which owns the decision. The executor
still matches the asserted actor against ``gates.approvers`` from git-reviewed config. Inside the
gateway process the import closure is the whole protection, and
``tests/webui/test_approvals_api.py`` walks that graph and refuses a tool that reaches this
module. A tool that runs arbitrary code in this process defeats that closure, and the approver
match is then the last rule that holds.

**Why a failed read reports degraded rather than an empty queue.** An empty list must not read as
"no action waits". The socket may be down instead, and an operator who reads an empty inbox during
an outage learns the wrong fact. So the payload carries ``degraded`` and the caller renders the
difference. #32 applies the same rule to the latch banner.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

from loguru import logger

from nanoinfra.gates.executor.operator_socket import (
    OperatorClient,
    OperatorUnavailableError,
)
from nanoinfra.webui.http_utils import case_insensitive_header

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nanoinfra.gates.executor.operator_socket import PendingView

# The two routes. ``ws_http.py`` imports these names, so one file owns the paths.
APPROVALS_READ_PATH = "/api/webui/gates/approvals"
APPROVALS_ANSWER_PATH = "/api/webui/gates/approvals/answer"

# The answer body travels in a header, the way every other WebUI mutation body travels. The
# ``websockets`` request object carries no body at all.
APPROVAL_VALUES_HEADER = "X-Nanoinfra-Approval-Values"

# The path this surface answers on. It matches ``gates.approvalPaths`` and ``Approver.channel``,
# and #13 refuses an answer whose path equals the origin path of the request.
APPROVAL_PATH = "webui"

DECISION_APPROVE = "approve"
DECISION_DENY = "deny"

# The reason a free-text field reaches the audit log. A record is one line, and an operator has
# to be able to read it.
_MAX_REASON_CHARS = 500


class ApprovalAnswerError(ValueError):
    """The answer named no request, no decision, or no digest.

    A separate type, because the route answers 400 for this case and 200 for an answer the
    executor refused. A client fault and an operator refusal are two different events.
    """


class ApprovalsOperatorSurface:
    """The operator half of one suspended action, behind two methods and nothing else.

    A route holds this object. It must not be able to hand the client on, so the client stays
    private and no accessor returns it.
    """

    def __init__(self, *, client: object) -> None:
        """Take the real operator client, and refuse anything else.

        The parameter is ``object`` for the reason ``LatchOperatorSurface`` takes one: the
        gateway that calls this is dynamically typed, so a static annotation guards nothing
        there. A request carries strings and JSON, and this check makes those values fail at the
        door.
        """
        if not isinstance(client, OperatorClient):
            raise TypeError(
                "an approvals operator surface needs the OperatorClient from the gateway"
            )
        self._client: OperatorClient = client

    def pending(self) -> dict[str, Any]:
        """Which actions wait for an answer, and how long each one has left.

        ``count`` is the unread count the navigation shows. ``degraded`` states that the read
        failed, so an empty list never reads as "no action waits".
        """
        try:
            views = self._client.pending()
        except OperatorUnavailableError as exc:
            logger.warning("gates: the approvals inbox could not reach the executor: {}", exc)
            return {
                "degraded": True,
                "approvalPath": APPROVAL_PATH,
                "count": 0,
                "pending": [],
            }
        return {
            "degraded": False,
            "approvalPath": APPROVAL_PATH,
            "count": len(views),
            "pending": [_for_operator(view) for view in views],
        }

    def answer(self, values: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        """Approve or deny one action, and report the rule that refused a bad answer.

        ``actor`` and the approval path come from the caller and from this module. Nothing in
        ``values`` names either one, so a browser that sends an ``actor`` field gains nothing.

        An approval carries the digest of the payload the operator read. A denial carries none,
        because a denial stops an action and authorizes no bytes. A deny therefore needs one
        field fewer than an approve, and it can never cost more steps.
        """
        request_id = _required_text(values, "requestId")
        decision = _required_text(values, "decision")
        if decision not in (DECISION_APPROVE, DECISION_DENY):
            raise ApprovalAnswerError(
                f"decision must be {DECISION_APPROVE!r} or {DECISION_DENY!r}"
            )
        target_digest = (
            _required_text(values, "targetDigest") if decision == DECISION_APPROVE else ""
        )

        try:
            if decision == DECISION_APPROVE:
                response = self._client.approve(
                    request_id=request_id,
                    actor=actor,
                    approval_path=APPROVAL_PATH,
                    target_digest=target_digest,
                )
            else:
                response = self._client.deny(
                    request_id=request_id,
                    actor=actor,
                    approval_path=APPROVAL_PATH,
                    reason=_optional_text(values.get("reason")),
                )
        except OperatorUnavailableError as exc:
            # The answer never arrived, so the action still waits. An operator must read that,
            # rather than read a refusal the executor never issued.
            logger.warning("gates: an approval answer could not reach the executor: {}", exc)
            return _answered(
                request_id=request_id,
                decision=decision,
                actor=actor,
                ok=False,
                refusal=None,
                error=str(exc),
                degraded=True,
            )

        return _answered(
            request_id=request_id,
            decision=decision,
            actor=actor,
            ok=response.ok,
            refusal=response.refusal,
            error=response.error,
            degraded=False,
        )


def _for_operator(view: PendingView) -> dict[str, Any]:
    """One suspended action in the WebUI's spelling.

    ``payload`` and ``targetDigest`` travel unchanged. ``samePath`` applies #13's third
    condition here, so the screen can state the refusal before the operator clicks.
    """
    return {
        "requestId": view["request_id"],
        "sessionId": view["session_id"],
        "originPath": view["origin_path"],
        "executionContext": view["execution_context"],
        "capabilityClass": view["capability_class"],
        "scope": view["scope"],
        "hostCount": view["host_count"],
        "hosts": list(view["hosts"]),
        "payload": view["payload"],
        "targetDigest": view["target_digest"],
        "expiresInS": view["expires_in_s"],
        "samePath": view["origin_path"] == APPROVAL_PATH,
    }


def _answered(
    *,
    request_id: str,
    decision: str,
    actor: str,
    ok: bool,
    refusal: str | None,
    error: str | None,
    degraded: bool,
) -> dict[str, Any]:
    """One answer, as the route reports it.

    ``refusal`` names the rule for the caller to render. ``error`` is the executor's own
    sentence, and it stays as the fallback for a refusal the caller does not know.
    """
    return {
        "ok": ok,
        "requestId": request_id,
        "decision": decision,
        "actor": actor,
        "refusal": refusal,
        "error": error,
        "degraded": degraded,
    }


def approval_values_from_request(request: Any) -> dict[str, Any] | None:
    """Read the answer body from the values header. ``None`` means an invalid payload."""
    raw = case_insensitive_header(request.headers, APPROVAL_VALUES_HEADER)
    if not raw:
        return None
    for candidate in (raw, unquote(raw)):
        try:
            values = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(values, dict):
            return cast("dict[str, Any]", values)
        return None
    return None


def _required_text(values: Mapping[str, Any], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApprovalAnswerError(f"{field} is required")
    return value.strip()


def _optional_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    return value.strip()[:_MAX_REASON_CHARS]


__all__ = [
    "APPROVALS_ANSWER_PATH",
    "APPROVALS_READ_PATH",
    "APPROVAL_PATH",
    "APPROVAL_VALUES_HEADER",
    "DECISION_APPROVE",
    "DECISION_DENY",
    "ApprovalAnswerError",
    "ApprovalsOperatorSurface",
    "approval_values_from_request",
]
