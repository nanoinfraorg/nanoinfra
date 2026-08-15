"""The operator surface that reads and clears a denial latch -- nanoinfraorg/nanoinfra#28.

A denial latches the capability class for the session (#15). The gate then refuses that class
and asks nobody, because a fresh prompt is the brute-force oracle. Only an operator lifts the
latch. Time does not lift it, a new turn does not lift it, and no value the model supplies
reaches the clearing code.

That rule is why this control is not a chat command. A chat command would let the model ask
for its own latch to go away. So the control lives outside the transcript, on an authenticated
HTTP route, and this module is the whole of it.

**How the controller arrives.** ``nanoinfra/cli/gateway_runtime.py`` builds the gate runtime
once and keeps the ``LatchController`` on the operator side. It then hands the controller to
one ``GatewayHTTPHandler`` instance. There is no module-level global here on purpose: a global
would let any importer reach the controller, and the split in #15 exists to stop exactly that.
A tool would need this module in its import graph to reach the handler, and
``tests/webui/test_latch_api.py`` walks that graph and refuses the case.

**Why the read half comes from the audit log.** ``LatchController`` has no read member, and
``DenialLatch`` holds the read members and travels toward the tools. So the operator side asks
the audit log instead, through ``restore_latches`` from #32. Two properties follow for free.
The refusal count survives a restart, so the banner never resets to zero and hides a session
that keeps trying. An unreadable log degrades and reports that every session stays latched,
which is the answer an operator needs rather than a silent empty list.

The cost is one dependency: the gateway recorder writes every latch event to that log. A write
that fails leaves the live latch in place and drops it from this view. #15 swallows a recorder
failure on purpose, because a refusal must never become a pass, so the log is the operator's
view and the process holds the authority.

**Why the clear needs no audit call here.** ``LatchController.clear`` already emits a
``LatchEvent`` with the actor, and the recorder in ``nanoinfra/gates/runtime.py`` turns that
event into an audit record. A second write from this module would double the record.
``GateRuntime.record_decision`` is the other writer, and it is the wrong one twice over: it
belongs to the gate half that this surface must not hold, and its decision vocabulary carries
no ``cleared`` value.
"""

from __future__ import annotations

import json
from collections import deque
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote

from nanoinfra.gates.latch import LatchController
from nanoinfra.gates.latch_restore import restore_latches
from nanoinfra.webui.http_utils import (
    MAX_IDENTITY_CHARS,
    TRUSTED_PROXY_IDENTITY_ATTR,
    case_insensitive_header,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from nanoinfra.gates.audit import AuditStore

# The two routes. ``ws_http.py`` imports these names, so one file owns the paths.
LATCH_READ_PATH = "/api/webui/gates/latches"
LATCH_CLEAR_PATH = "/api/webui/gates/latches/clear"

# The clear body travels in a header, the way every other WebUI mutation body travels. The
# ``websockets`` request object carries no body at all.
LATCH_VALUES_HEADER = "X-Nanoinfra-Latch-Values"

# What the actor reads as when no proxy asserts an identity. The path is then the only true
# statement about who cleared the latch.
_PATH_ACTOR = "webui"

# How many refused attempts one entry carries. The count is authoritative and comes from #32,
# so this bound costs detail on a long history and never costs the number an operator reads.
_MAX_ATTEMPTS = 50

# Bounds for the two free-text values that reach the audit log. A record is one line, and an
# operator has to be able to read it.
#
# The actor bound is derived from the identity bound rather than chosen, because the two must
# agree: a cap that cut ``webui:<claim>`` would write a name that belongs to nobody, and the
# seam already refuses an identity it cannot name whole (#63). So this cap can only fire for a
# caller that built an actor some other way, and it never cuts the answer of ``operator_actor``.
_MAX_ACTOR_CHARS = len(_PATH_ACTOR) + 1 + MAX_IDENTITY_CHARS
_MAX_REASON_CHARS = 500

_DENIED = "denied"
_REFUSED = "refused"
_CLEARED = "cleared"


class LatchClearError(ValueError):
    """The clear request named no session or no capability class.

    A separate type, because the route answers 400 for this case and 200 for a clear that
    found nothing. A typo must not read as done, and #15 returns False for that reason.
    """


class LatchOperatorSurface:
    """The operator half of the latch, behind two methods and nothing else.

    A route holds this object. It must not be able to hand the controller on, so the
    controller stays private and no accessor returns it.
    """

    def __init__(self, *, controller: object, audit: AuditStore) -> None:
        """Take the real operator half, and refuse anything else.

        The parameter is ``object`` for the same reason ``LatchController.__init__`` takes one:
        the gateway that calls this is dynamically typed, so a static annotation guards nothing
        there. A request carries strings and JSON, and this check makes those values fail at the
        door.
        """
        if not isinstance(controller, LatchController):
            raise TypeError(
                "a latch operator surface needs the LatchController from build_gate_runtime()"
            )
        self._controller: LatchController = controller
        self._audit = audit

    def payload(self) -> dict[str, Any]:
        """Which sessions are latched, for which class, since when, and how many refusals.

        The ``summary`` line is the one #32 writes for the startup echo. It states the degraded
        case in an operator's words, so this module invents no second wording for it.
        """
        restored = restore_latches(self._audit)
        summary = restored.summary()
        if restored.degraded or not restored.latched:
            # A degraded log cannot name the sessions it lost. The banner shows the summary,
            # and an empty list here never means "nothing is enforced".
            return {"degraded": restored.degraded, "summary": summary, "latches": []}

        details, attempts = _detail_from_log(self._audit)
        latches: list[dict[str, Any]] = []
        for key in sorted(restored.latched):
            session_id, capability_class = key
            detail = details.get(key, {})
            latches.append(
                {
                    "sessionId": session_id,
                    "capabilityClass": capability_class,
                    "deniedAt": detail.get("at"),
                    "deniedBy": detail.get("actor"),
                    "reason": detail.get("reason"),
                    "refusals": restored.refusal_count(session_id, capability_class),
                    "attempts": list(attempts.get(key, ())),
                }
            )
        return {"degraded": False, "summary": summary, "latches": latches}

    def clear(self, values: Mapping[str, Any], *, actor: str) -> dict[str, Any]:
        """Lift one latch, and let the controller record who did it.

        ``cleared`` is False when nothing was latched. The call is then a no-op, and the log
        gains no record, because a no-op is not a clear.
        """
        session_id = _required_text(values, "sessionId")
        capability_class = _required_text(values, "capabilityClass")
        cleared = self._controller.clear(
            session_id=session_id,
            capability_class=capability_class,
            actor=actor[:_MAX_ACTOR_CHARS],
            reason=_optional_text(values.get("reason")),
        )
        return {
            "cleared": cleared,
            "sessionId": session_id,
            "capabilityClass": capability_class,
            "actor": actor[:_MAX_ACTOR_CHARS],
        }


def latch_values_from_request(request: Any) -> dict[str, Any] | None:
    """Read the clear body from the values header. ``None`` means an invalid payload."""
    raw = case_insensitive_header(request.headers, LATCH_VALUES_HEADER)
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


def operator_actor(request: Any) -> str:
    """Name the operator on this path, and never take the name from the request body.

    **Two answers exist and no third.** ``webui:<claim>`` names the person a verified assertion
    identified (#63). ``webui`` names the path alone, which is the true answer for a deployment
    that authenticated a shared token and nobody, and a route must not invent an identity that
    nothing authenticated.

    The request is the whole input. ``ws_http.dispatch`` resolves the identity once per request,
    and on the ``jwt`` path that means a verified signature and the access rules of #62. This
    function therefore holds no config: a function that could read ``assertionHeader`` could
    name a person from an unverified header, and on the ``jwt`` path that header carries the
    whole token, so the actor would be named after a token prefix. The ``plain`` path resolves
    its header into the same attribute inside the authenticator, so one value carries both
    formats and this function reads one attribute.

    **A refusal is never a name here.** ``dispatch`` leaves the identity empty for an assertion
    it refused, so the answer is the path. Such a request still has to pass the token checks the
    deployment already had to reach any route, which is what keeps a forged token from buying
    the privileges of the shared token.
    """
    asserted = str(getattr(request, TRUSTED_PROXY_IDENTITY_ATTR, "") or "").strip()
    if not asserted:
        return _PATH_ACTOR
    return f"{_PATH_ACTOR}:{asserted}"


def _detail_from_log(
    store: AuditStore,
) -> tuple[dict[tuple[str, str], dict[str, Any]], dict[tuple[str, str], deque[dict[str, Any]]]]:
    """Fold the log into the display detail for each latch: the denial, and the attempts.

    #32 decides *which* pairs are latched and how many refusals arrived. This pass only adds
    the fields a human reads. So a record that this pass misses costs a line of detail and
    never costs a latch.
    """
    details: dict[tuple[str, str], dict[str, Any]] = {}
    attempts: dict[tuple[str, str], deque[dict[str, Any]]] = {}
    try:
        records = store.read_all()
    except OSError:
        # The read failed. ``restore_latches`` reports that state as degraded, and this pass
        # has nothing to add to it.
        return details, attempts

    for record in records:
        key = _key(record)
        if key is None:
            continue
        decision = str(record.get("decision") or "")
        if decision == _DENIED:
            # One denial writes two records: the gate decision, and the latch event. So the
            # fields merge, and the first timestamp wins.
            detail = details.setdefault(key, {})
            attempts.setdefault(key, deque(maxlen=_MAX_ATTEMPTS))
            for field, value in (
                ("at", record.get("ts")),
                ("actor", record.get("actor")),
                ("reason", record.get("reason")),
            ):
                if detail.get(field) is None and value is not None:
                    detail[field] = value
        elif decision == _CLEARED:
            details.pop(key, None)
            attempts.pop(key, None)
        elif decision == _REFUSED and key in attempts:
            attempts[key].append(
                {
                    "at": record.get("ts"),
                    "tool": record.get("tool"),
                    "digest": record.get("command_digest"),
                }
            )
    return details, attempts


def _key(record: Mapping[str, Any]) -> tuple[str, str] | None:
    session_id = record.get("session_id")
    capability_class = record.get("capability_class")
    if not session_id or not capability_class:
        return None
    return (str(session_id), str(capability_class))


def _required_text(values: Mapping[str, Any], field: str) -> str:
    value = values.get(field)
    if not isinstance(value, str) or not value.strip():
        raise LatchClearError(f"{field} is required")
    return value.strip()


def _optional_text(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_MAX_REASON_CHARS]


__all__ = [
    "LATCH_CLEAR_PATH",
    "LATCH_READ_PATH",
    "LATCH_VALUES_HEADER",
    "LatchClearError",
    "LatchOperatorSurface",
    "latch_values_from_request",
    "operator_actor",
]
