"""Perform one connector operation in the executor, with the gate in front of it.

**Why here and not in the agent.** Three of the four things a connector call needs already
live in this process and cannot leave it: the secret store that holds the refresh token, the
approval socket a human answers on, and the audit log. So a connector call goes over the same
wire a command does, and the agent submits a request and renders the answer.

That is what makes an interactive write possible rather than merely refused: the executor
renders what will be sent, suspends the action, and waits for a person on a second path -- the
same construction ``server.py::_suspend`` uses, because it is the same problem.

**What the frame cannot say.** The agent names a connector and an operation. The method, the
path, the capability class and the scopes come from the installed manifest, so a request cannot
describe a call the package never declared, and it cannot relabel a write as a read. That is
``server_id_or_name`` again: nothing about the target rides on the agent's word.

One difference from a command is worth stating. A shell command is one opaque string, so the
prompt renders it verbatim. A connector call is a method, a URL and a body, and the body's
values *are* the action -- the event title, the attendees. So the rendered payload carries them
in a canonical form, and the digest binds those bytes. An approver who reads "create an event
called X with these two attendees" has read what will happen.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, cast
from urllib.parse import urlsplit

from loguru import logger

from nanoinfra.agent.tools.base import Schema
from nanoinfra.agent.tools.capabilities import record_observation
from nanoinfra.connectors.contracts import ConnectorOperation
from nanoinfra.connectors.credentials import CredentialError
from nanoinfra.connectors.engine import (
    ConnectorCallError,
    PreparedRequest,
    call,
    prepare,
)
from nanoinfra.connectors.setup import ActiveConnector, resolve_active
from nanoinfra.gates.approvals import approval_feasible
from nanoinfra.gates.executor.connector_credentials import (
    RefreshTokenSource,
    token_source_for,
)
from nanoinfra.gates.executor.protocol import ConnectorRequest, ExecuteResponse
from nanoinfra.gates.pending import ApprovalState, PendingApprovalStore
from nanoinfra.gates.policy import Outcome, evaluate_connector, load_policy
from nanoinfra.gates.prompt import PromptRenderError, render_approval_prompt_for_hosts
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets.store import SecretStore

if TYPE_CHECKING:
    from nanoinfra.config.connectors import ConnectorRuntimeConfig
    from nanoinfra.config.gates import GatesConfig

# The scope a connector call is judged at. One connector reaches one remote service under one
# credential, so blast radius does not vary with the operation; a per-operation scope would
# invent a tier the matrix does not model.
_SCOPE = "host"

_DECISION_ALLOW = "allow"
_DECISION_APPROVE = "approve"
_DECISION_DENIED = "denied"
_DECISION_EXPIRED = "expired"


def _load_connectors() -> ConnectorRuntimeConfig:
    from nanoinfra.config.loader import load_config

    return load_config().connectors


def _refusal(reason: str, *, terminal: bool = True) -> ExecuteResponse:
    return ExecuteResponse(
        ok=False, output="", exit_code=None, error=None, reason=reason, terminal=terminal
    )


def _render_action(prepared: PreparedRequest) -> str:
    """The bytes an approver reads, and the bytes the digest binds.

    Canonical JSON, sorted keys: one action must render one way, or the digest describes a
    formatting choice instead of an action.
    """
    line = f"{prepared.method} {prepared.url}"
    if prepared.params:
        pairs = "&".join(f"{key}={prepared.params[key]}" for key in sorted(prepared.params))
        line += f"?{pairs}"
    if prepared.body:
        body = json.dumps(prepared.body, sort_keys=True, ensure_ascii=False, separators=(", ", ": "))
        line += f"\n{body}"
    return line


def _api_host(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host


def _validate_arguments(
    op: ConnectorOperation, raw: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and check the call arguments against the operation's own schema.

    An undeclared key is refused rather than forwarded: it would otherwise become a query
    parameter or a body field that nobody reviewed and the manifest never described.
    """
    try:
        parsed = cast(object, json.loads(raw or "{}"))
    except ValueError as exc:
        return None, f"the call arguments are not JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "the call arguments must be a JSON object"
    arguments = cast(dict[str, Any], parsed)

    schema = dict(op.parameters) or {"type": "object", "properties": {}}
    schema.setdefault("additionalProperties", False)
    errors = Schema.validate_json_schema_value(arguments, schema)
    if errors:
        return None, f"the call arguments do not match {op.name}: {'; '.join(errors)}"
    return arguments, None


@dataclass
class ConnectorActionRunner:
    """Answers one connector request. Holds the credential store and the gate.

    Built beside ``Executor`` and given the same three collaborators, because a connector call
    needs exactly what a command needs: policy, an approval path, and a record.
    """

    workspace: Path
    connectors_loader: Callable[[], ConnectorRuntimeConfig] = _load_connectors
    gates_loader: Callable[[], GatesConfig] = load_policy
    audit: Any = None
    pending: PendingApprovalStore | None = None
    tokens: ApprovalTokenStore | None = None
    # One token source per connector, so a warm access token serves the next call of the same
    # class rather than paying for an exchange per action.
    _sources: dict[str, RefreshTokenSource] = dataclass_field(
        default_factory=dict[str, RefreshTokenSource]
    )

    def _token_source(self, active: ActiveConnector) -> RefreshTokenSource:
        source = self._sources.get(active.name)
        if source is None:
            source = token_source_for(active, SecretStore(self.workspace))
            self._sources[active.name] = source
        return source

    def _resolve(
        self, request: ConnectorRequest
    ) -> tuple[ActiveConnector, ConnectorOperation] | ExecuteResponse:
        active, problems = resolve_active(self.connectors_loader())
        by_name = {entry.name: entry for entry in active}
        entry = by_name.get(request.connector)
        if entry is None:
            failed = next(
                (problem for problem in problems if problem.connector == request.connector), None
            )
            if failed is not None:
                # A deployment fact rather than an answer about this action, so it does not
                # latch the class: an operator who fixes the config must not also clear a latch.
                return _refusal(
                    f"connector {request.connector!r} is configured and did not activate: "
                    f"{failed.reason}",
                    terminal=False,
                )
            return _refusal(
                f"connector {request.connector!r} is not active in this deployment. Active: "
                f"{sorted(by_name) or 'none'}.",
                terminal=False,
            )
        op = next((item for item in entry.operations if item.name == request.operation), None)
        if op is None:
            return _refusal(
                f"connector {entry.name!r} does not offer {request.operation!r} here. It offers "
                f"{sorted(item.name for item in entry.operations)}.",
                terminal=False,
            )
        return entry, op

    async def handle(self, request: ConnectorRequest) -> ExecuteResponse:
        """Answer one connector request. Never raises for a refusal: a refusal is a response."""
        if request.token_nonce:
            return _refusal(
                "This request carries an approval nonce. The executor issues every nonce and "
                "gives none to the agent, so no caller on this socket can hold one."
            )

        resolved = self._resolve(request)
        if isinstance(resolved, ExecuteResponse):
            return resolved
        entry, op = resolved

        arguments, problem = _validate_arguments(op, request.arguments_json)
        if arguments is None:
            return _refusal(problem or "the call arguments were refused")

        try:
            prepared = prepare(entry.plugin, op, arguments, defaults=entry.defaults)
        except ConnectorCallError as exc:
            return _refusal(str(exc))

        action = _render_action(prepared)
        hosts = (_api_host(prepared.url),)
        tool = entry.plugin.tool_name(op)

        record_observation(
            capability_class=op.capability_class,
            decision="preview" if request.preview_requested else "would_gate",
            tool=tool,
            connector=entry.name,
            operation=op.name,
            scope=_SCOPE,
            credential=entry.credential.name,
        )

        if request.preview_requested:
            return self._preview(request, entry, op, action)

        decision = evaluate_connector(
            self.gates_loader(),
            capability_class=op.capability_class,
            execution_context=request.execution_context,
            connector=entry.name,
            operation=op.name,
        )

        if decision.outcome is Outcome.APPROVE:
            suspended = await self._suspend(request, entry, op, action, hosts, tool)
            if isinstance(suspended, ExecuteResponse):
                return suspended

        elif decision.outcome is not Outcome.ALLOW:
            self._record(
                _DECISION_DENIED,
                request,
                entry,
                op,
                tool=tool,
                action=action,
                hosts=hosts,
                reason=decision.reason,
            )
            return _refusal(f"Refusing {tool}. {decision.reason}")

        else:
            self._record(
                _DECISION_ALLOW,
                request,
                entry,
                op,
                tool=tool,
                action=action,
                hosts=hosts,
                reason=decision.reason,
                grant_id=decision.grant_id,
            )

        return await self._run(request, entry, op, arguments, tool)

    def _preview(
        self,
        request: ConnectorRequest,
        entry: ActiveConnector,
        op: ConnectorOperation,
        action: str,
    ) -> ExecuteResponse:
        """What would be sent, and what the gate would answer. Nothing is sent, nobody is asked.

        The decision is recorded nowhere but this response: writing it as a denial would latch
        the session, so asking what the gate would say would block the work being previewed.
        """
        decision = evaluate_connector(
            self.gates_loader(),
            capability_class=op.capability_class,
            execution_context=request.execution_context,
            connector=entry.name,
            operation=op.name,
        )
        return ExecuteResponse(
            ok=True,
            output=action,
            exit_code=None,
            error=None,
            reason=f"preview only. Nothing was sent to {entry.plugin.display_name}.",
            preview_outcome=decision.outcome.value,
            preview_reason=decision.reason,
            preview_grant_id=decision.grant_id,
            preview_scope=_SCOPE,
            preview_command=action,
        )

    async def _suspend(
        self,
        request: ConnectorRequest,
        entry: ActiveConnector,
        op: ConnectorOperation,
        action: str,
        hosts: tuple[str, ...],
        tool: str,
    ) -> ExecuteResponse | None:
        """Hold the call until a person answers, or return the refusal that ends it.

        Same order as a command's suspension, and for the same reason: every case where no
        correct answer can exist refuses first, because a suspension nobody may answer is a
        hang. Then the payload renders, then the record lands, and only then does it wait.
        """
        gates = self.gates_loader()
        pending = self.pending
        approvals = self.tokens
        if pending is None or approvals is None:
            return _refusal(
                "this executor has no approval store, so no human can answer an approval. "
                f'Declare a standing grant naming {{"connectors": ["{entry.name}"], '
                f'"operations": ["{op.name}"]}}, or run an executor that carries the approval '
                "path.",
                terminal=False,
            )
        session_id = (request.session_id or "").strip()
        if not session_id:
            return _refusal(
                "the request names no session, and an approval binds to one session. So no "
                "token could cover this call."
            )

        feasibility = approval_feasible(
            gates=gates,
            origin_path=request.origin_path or "",
            origin_actor=request.origin_actor or "",
        )
        if not feasibility.ok:
            return _refusal(
                f"{feasibility.reason} Three fixes answer this. Add an approver on a second "
                "authenticated path for interactive work. Declare a standing grant naming this "
                "connector and operation for recurring work. Or set "
                "gates.interactive.mutate.remote.host to 'allow' for a deployment with one "
                "operator and one path.",
                terminal=False,
            )

        try:
            prompt = render_approval_prompt_for_hosts(command=action, hosts=hosts)
        except PromptRenderError as exc:
            return _refusal(
                f"the approval payload could not be rendered ({exc}). A human cannot approve "
                "bytes nobody can display.",
                terminal=False,
            )

        self._record(
            _DECISION_APPROVE,
            request,
            entry,
            op,
            tool=tool,
            action=action,
            hosts=hosts,
            reason=(
                f"an operator must approve this call on a path other than "
                f"{request.origin_path!r}. The request waits for {gates.approval_timeout_s}s."
            ),
        )
        approval = pending.create(
            session_id=session_id,
            origin_path=(request.origin_path or "").strip(),
            origin_actor=(request.origin_actor or "").strip(),
            execution_context=request.execution_context,
            capability_class=op.capability_class,
            scope=_SCOPE,
            hosts=hosts,
            command=action,
            payload=prompt.text,
            target_digest=prompt.target_digest,
            timeout_s=float(gates.approval_timeout_s),
        )
        logger.info(
            "gates: {} waits for an approval on a second path (session {})",
            tool,
            session_id,
        )
        outcome = await asyncio.to_thread(pending.wait, approval.request_id)

        if outcome.state is ApprovalState.DENIED:
            self._record(
                _DECISION_DENIED,
                request,
                entry,
                op,
                tool=tool,
                action=action,
                hosts=hosts,
                reason=f"an operator denied this call: {outcome.reason or 'no reason given'}.",
                actor=outcome.actor,
                approval_path=outcome.approval_path,
            )
            return _refusal(
                f"An operator denied {tool}: {outcome.reason or 'no reason given'}."
            )
        if outcome.state is not ApprovalState.APPROVED or outcome.token_nonce is None:
            self._record(
                _DECISION_EXPIRED,
                request,
                entry,
                op,
                tool=tool,
                action=action,
                hosts=hosts,
                reason=outcome.reason or "the approval did not arrive, so the call refused.",
            )
            return _refusal(
                outcome.reason or f"No approval arrived for {tool}, so it did not run."
            )

        verification = approvals.consume(
            nonce=outcome.token_nonce,
            session_id=session_id,
            target_digest=prompt.target_digest,
        )
        if not verification.ok:
            self._record(
                _DECISION_DENIED,
                request,
                entry,
                op,
                tool=tool,
                action=action,
                hosts=hosts,
                reason=f"the approval token did not verify at execution ({verification.refusal}).",
                actor=outcome.actor,
                approval_path=outcome.approval_path,
            )
            return _refusal(
                f"The approval for {tool} did not verify ({verification.refusal}). An approval "
                "covers one session and one rendered call."
            )

        self._record(
            _DECISION_ALLOW,
            request,
            entry,
            op,
            tool=tool,
            action=action,
            hosts=hosts,
            reason=(
                f"{outcome.actor!r} approved this call on path {outcome.approval_path!r}, and "
                f"the request arrived on {request.origin_path!r}."
            ),
            actor=outcome.actor,
            approval_path=outcome.approval_path,
            approval_id=approval.request_id,
        )
        return None

    async def _run(
        self,
        request: ConnectorRequest,
        entry: ActiveConnector,
        op: ConnectorOperation,
        arguments: dict[str, Any],
        tool: str,
    ) -> ExecuteResponse:
        """Mint the token, make the call, and hand back the projection."""
        try:
            payload = await call(
                entry.plugin,
                op,
                arguments,
                tokens=self._token_source(entry),
                defaults=entry.defaults,
            )
        except CredentialError as exc:
            # A credential problem is not the action failing. It names the connector, because
            # the fix is an operator re-authorising it.
            return _refusal(f"{entry.name}: {exc}", terminal=False)
        except ConnectorCallError as exc:
            return ExecuteResponse(
                ok=False,
                output="",
                exit_code=None,
                error=str(exc),
                reason=str(exc),
                terminal=not exc.retryable,
            )
        return ExecuteResponse(
            ok=True,
            output=json.dumps(payload, ensure_ascii=False, indent=2),
            exit_code=0,
            error=None,
            reason=f"{tool} ran as {op.capability_class}.",
        )

    def _record(
        self,
        decision: str,
        request: ConnectorRequest,
        entry: ActiveConnector,
        op: ConnectorOperation,
        *,
        tool: str,
        action: str,
        hosts: tuple[str, ...],
        reason: str,
        actor: str | None = None,
        approval_path: str | None = None,
        grant_id: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        """Write the audit record for one connector decision.

        The four facts the log has to answer "what used the Google credential, and for what"
        with: the connector and operation (in ``tool``), the class, the rendered call, and the
        credential ref. A read is recorded too, because "who read the calendar" is a question
        an operator will ask even though the class is not gated.

        Unlike a command, a failed write here does not refuse the action: the decision has
        already been taken and, for an approved call, a person has already answered. Losing the
        record is logged loudly rather than turned into a second refusal a human cannot act on.
        """
        if self.audit is None:
            return
        try:
            self.audit.record(
                decision=decision,
                capability_class=op.capability_class,
                execution_context=request.execution_context,
                session_id=request.session_id,
                tool=tool,
                scope=_SCOPE,
                hosts=list(hosts),
                secret_ref=entry.credential.name,
                command=action,
                reason=reason or None,
                actor=actor,
                origin_path=request.origin_path,
                origin_actor=request.origin_actor,
                approval_path=approval_path,
                grant_id=grant_id,
                approval_id=approval_id,
            )
        except OSError as exc:
            logger.error("the audit record for {} failed to write: {}", tool, exc)


__all__ = ["ConnectorActionRunner"]
