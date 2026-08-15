"""The executor: the only process that reaches a host -- nanoinfraorg/nanoinfra#18.

Everything the agent must lose lives here. The credential store, the four transports, the
target guard, the scope resolver, and the gate itself. The agent submits a structured request
and renders the reply, so a compromised agent can ask and cannot act.

The order inside ``handle`` is the same order the tool used before the split, and for the same
reasons. Refusals that need no credential run first, the gate runs before the credential
resolves, and the job record follows the decision. What changed is the address space: the agent
no longer holds the plaintext, the backends, or the guard.

This module imports the privileged parts on purpose. ``nanoinfra/agent/tools/server_execution.py``
must import none of them, and a test asserts that. Import direction is how the split stays true
rather than merely intended.

**An ``approve`` outcome suspends the action (#38).** The gate used to allow every interactive
turn, so an ``approve`` decision executed. It now renders the payload with #14, creates a
pending record, and blocks on it until an operator answers or the deadline passes. The reasons
for a blocked call rather than a poll live in ``nanoinfra/gates/pending.py``. The answer arrives
on a second socket that ``nanoinfra/gates/executor/operator_socket.py`` owns.

Two consequences shape this module. Each connection now gets its own thread, because a pending
approval that stopped every other action would be a denial of service on the whole agent. And
one action holds one inventory read for the whole wait, so the command that runs after an
approval is the command the operator read.

**The transcript scrub lives here too (#41).** The agent used to decrypt every secret of the
workspace to build its redaction sentinels, which put the whole credential store in the process
the model runs in. ``nanoinfra/gates/executor/scrub.py`` holds that work now, and it answers on a
third socket. The agent sends one text and reads the scrubbed text back.

**The outcome is a second record (#46).** The decision record landed and said nothing about what
followed, so ``exit_code`` and ``duration_ms`` stayed null on every record. ``_run`` now appends
one completion record when the action ends, and ``_record`` hands back the decision record so the
completion can name it. A refusal appends nothing, because nothing ran. An action that ends with
no exit code still appends a record, because an unknown outcome and an action that never ran are
opposite facts for a reviewer.

**``credential.access`` decides before the decryption (#39).** ``_run`` asks the class before
``resolve_plaintext`` reads the secret store, and before a backend opens a transport. The
decision covers the decryption alone, so a server with no ``secretRef`` reaches no credential
decision. Nobody answers a second prompt. ``_decide`` and ``_suspend`` each hand ``_run`` the
authorization the action already carries. The record then names the grant, or the approval, that
satisfied the class.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from loguru import logger

from nanoinfra.agent.tools.capabilities import (
    CREDENTIAL_ACCESS,
    MUTATE_REMOTE,
    command_digest,
    record_observation,
)
from nanoinfra.agent.tools.context import EXECUTION_CONTEXT_INTERACTIVE
from nanoinfra.gates.approvals import approval_feasible
from nanoinfra.gates.executor.operator_socket import (
    ApprovalService,
    bind_operator_socket,
    default_operator_socket_path,
    serve_operator_socket,
)
from nanoinfra.gates.executor.protocol import (
    ExecuteResponse,
    ProtocolError,
    decode_request,
    encode_response,
    read_frame,
    write_frame,
)
from nanoinfra.gates.executor.scrub import bind_scrub_socket, serve_scrub_socket
from nanoinfra.gates.executor.scrub_protocol import default_scrub_socket_path
from nanoinfra.gates.pending import ApprovalState, PendingApprovalStore
from nanoinfra.gates.policy import (
    ActionAuthorization,
    Decision,
    Outcome,
    evaluate,
    evaluate_credential_access,
    load_policy,
)
from nanoinfra.gates.prompt import PromptRenderError, render_approval_prompt
from nanoinfra.gates.tokens import ApprovalTokenStore
from nanoinfra.secrets.store import SecretStore
from nanoinfra.servers.execution.base import (
    ABSOLUTE_CEILING_S,
    BoundedOutput,
    truncate_output,
)
from nanoinfra.servers.execution.timeout import IdleTimeoutTracker, run_with_idle_timeout
from nanoinfra.servers.job_store import JobStore
from nanoinfra.servers.lookup import resolve_server
from nanoinfra.servers.network_guard import validate_server_target
from nanoinfra.servers.scope import (
    ALL,
    ScopeResolutionError,
    one_inventory_read_per_action,
    prime_inventory_read,
    resolve_scope,
    resolve_scope_label,
)
from nanoinfra.servers.store import ServerStore

if TYPE_CHECKING:
    from nanoinfra.config.gates import GatesConfig
    from nanoinfra.gates.executor.protocol import ExecuteRequest

_KNOWN_PROVIDER_IDS = frozenset({"ssh", "ansible-runner", "ssm", "api"})
_STREAMING_PROVIDERS = frozenset({"ssh"})
_UNSTOPPABLE_ON_TIMEOUT = frozenset({"ansible-runner", "ssm"})
_PARTIAL_OUTPUT_PERSIST_INTERVAL_S = 5.0

# What each end of a decision is called in the audit log. The vocabulary is the operator's, and
# #32 rebuilds a latch from `denied`, so these strings are part of the contract.
#
# `expired` deliberately latches nothing across a restart, because an expiry is not a decision
# anybody took. The tool still latches it in the live process, so a retry inside the session
# refuses. `approval_refused` lives in operator_socket.py beside the code that writes it, and it
# latches nothing either: one mistyped actor must not block a session a real approver can answer.
_DECISION_ALLOW = "allow"
_DECISION_APPROVE = "approve"
_DECISION_DENIED = "denied"
_DECISION_EXPIRED = "expired"

# The socket's own mode is not honoured on every platform, so the directory carries the
# control. 0o700 keeps another local account out of the executor's door.
_SOCKET_DIR_MODE = 0o700


@dataclass(slots=True, frozen=True)
class _Written:
    """What one audit write hands back to the caller.

    ``refusal`` holds a response when the write failed. The caller returns it, because an action
    that nothing records does not run.

    ``record`` holds the record that landed. #46 needs it, because a completion record names the
    decision record it follows. Both fields are None when this executor carries no audit store.
    """

    refusal: ExecuteResponse | None = None
    record: dict[str, Any] | None = None


@dataclass(slots=True)
class Executor:
    """Answers one request. Holds the credential store, the transports, and the gate."""

    workspace: Path
    gates_loader: Callable[[], GatesConfig] = load_policy
    # The audit store (#16). The executor decides, so the executor records. #33 wired this into
    # the tool, and the split moved the decision here, so the record moved with it.
    audit: Any = None
    # The two halves of the approval path (#38). Both live in this process, because the executor
    # is the authority. An executor built without them refuses an `approve` outcome rather than
    # execute it, which is the fail-closed direction: no store means no human can answer.
    pending: PendingApprovalStore | None = None
    tokens: ApprovalTokenStore | None = None

    async def handle(self, request: ExecuteRequest) -> ExecuteResponse:
        """Answer one request. Never raises for a refusal: a refusal is a response."""
        if request.token_nonce:
            # The executor issues every nonce and hands none to the agent (#38), so a nonce on
            # this wire arrives from model-visible text. A verification attempt would treat a
            # forgery as a stale approval, so the frame gets a refusal instead.
            return _error(
                "This request carries an approval nonce. The executor issues every nonce and "
                "gives none to the agent, so no caller on this socket can hold one."
            )
        servers = ServerStore(self.workspace)
        server = resolve_server(servers, request.server_id_or_name)
        if server is None:
            return _error(f"No server matches {request.server_id_or_name!r}.")
        if server.provider_id not in _KNOWN_PROVIDER_IDS:
            return _error(f"Unknown providerId: {server.provider_id!r}.")

        # #35: one inventory read per action, and no read on the event loop. The guard, the
        # observation record, the gate, and the preview line all need the same host set, and
        # the answer cannot change inside one action. The cache closes with this block, so the
        # next action reads again and an inventory write reaches it (#24).
        with one_inventory_read_per_action():
            await asyncio.to_thread(prime_inventory_read, server)
            return await self._decide(server, request, servers)

    async def _decide(
        self, server: Any, request: ExecuteRequest, servers: Any
    ) -> ExecuteResponse:
        """Guard, gate, record, and run one action, inside one inventory read."""
        guard_error = _guard(server)
        if guard_error is not None:
            return _error(guard_error)

        try:
            idle_override = int(request.timeout_s) if request.timeout_s else None
        except ValueError:
            return _error(f"Invalid timeout_s: {request.timeout_s!r} is not an integer.")

        record_observation(
            capability_class=MUTATE_REMOTE,
            decision="preview" if request.preview_requested else "would_gate",
            tool="execute_on_server",
            server_id=server.id,
            server_name=server.name,
            provider_id=server.provider_id,
            # #4's blast radius. resolve_scope_label() never raises, so a log-only record
            # cannot fail an action. The split dropped this field briefly, and a record
            # without it cannot answer how many hosts an action would have reached.
            scope=resolve_scope_label(server),
            command_digest=command_digest(request.command),
        )

        if request.preview_requested:
            return ExecuteResponse(
                ok=True,
                output=_preview_line(server, request.command),
                exit_code=None,
                error=None,
                reason="the caller asked for a preview",
            )

        # The gate resolves each grant host, so it runs in a worker thread as well. Its own
        # reads hit the cache when the grant names this project, and a grant that names
        # another project pays for its read off the event loop (#35).
        decision, resolution = await asyncio.to_thread(self._gate, server, request, servers)

        if decision.outcome is Outcome.APPROVE:
            # The action suspends here (#38). Every record and every refusal for the wait lives
            # in _suspend, because the wait is one decision with several possible ends.
            return await self._suspend(server, request, resolution, idle_override)

        written = self._record(
            _DECISION_ALLOW if decision.outcome is Outcome.ALLOW else _DECISION_DENIED,
            server,
            request,
            reason=decision.reason,
            resolution=resolution,
            grant_id=decision.grant_id,
        )
        if written.refusal is not None:
            return written.refusal

        if decision.outcome is not Outcome.ALLOW:
            return _withheld(server, request, decision.reason)

        # The authorization this action carries into the credential decision (#39). A matched grant
        # names itself. A plain `allow` on the matrix is an authorization too: an operator who
        # allowed the action at this scope authorized the credential it needs, and a second gate on
        # the same decision refused every allowed action against a server that holds a secretRef.
        return await self._run(
            server,
            request,
            idle_override,
            resolution=resolution,
            decision_record=written.record,
            authorization=ActionAuthorization(
                grant_id=decision.grant_id,
                policy_decision=decision.outcome.value,
                scope=getattr(resolution, "scope", None),
            ),
        )

    async def _suspend(
        self, server: Any, request: ExecuteRequest, resolution: Any, idle_override: int | None
    ) -> ExecuteResponse:
        """Hold one action until an operator answers it, or until the deadline passes.

        The order is the security property. Every reason that no correct answer can exist
        refuses at once, because a suspension nobody may answer is a hang. Then the payload
        renders from resolver output (#14). Then the record lands (#16). Only then does the
        action wait.
        """
        gates = self.gates_loader()
        pending = self.pending
        tokens = self.tokens
        if pending is None or tokens is None:
            # A deployment fact, and not an answer about this action, so it does not latch (#42).
            return self._refuse(
                server,
                request,
                resolution,
                "this executor has no approval store, so no human can answer an approval. "
                "Declare a standing grant for this action, or run an executor that carries "
                "the approval path.",
                terminal=False,
            )

        session_id = (request.session_id or "").strip()
        if not session_id:
            return self._refuse(
                server,
                request,
                resolution,
                "the request names no session, and an approval binds to one session (#12). "
                "So no token could cover this action.",
            )

        feasibility = approval_feasible(
            gates=gates,
            origin_path=request.origin_path or "",
            # The agent's assertion about the person who raised the turn. It can only widen who
            # may answer, and only behind gates.identityIndependence.
            origin_actor=request.origin_actor or "",
        )
        if not feasibility.ok:
            # A configuration gap, and not an answer about this action. So it does not latch, and
            # the message names the fix that suits each case (#42).
            return self._refuse(
                server,
                request,
                resolution,
                f"{feasibility.reason} Three fixes answer this. Add an approver on a second "
                "authenticated path for interactive work. Declare a standing grant in "
                "gates.standingGrants for recurring work, which matches an exact resolved "
                "command and never an ad-hoc one. Or set this scope to 'allow' for a deployment "
                "with one operator and one path.",
                terminal=False,
            )

        try:
            prompt = render_approval_prompt(command=request.command, resolution=resolution)
        except PromptRenderError as exc:
            return self._refuse(
                server,
                request,
                resolution,
                f"the approval payload could not be rendered ({exc}). A human cannot approve "
                "bytes nobody can display.",
                terminal=False,
            )

        suspended = self._record(
            _DECISION_APPROVE,
            server,
            request,
            reason=(
                f"an operator must approve this action on a path other than "
                f"{request.origin_path!r}. The request waits for "
                f"{gates.approval_timeout_s}s."
            ),
            resolution=resolution,
            origin_path=request.origin_path,
        )
        if suspended.refusal is not None:
            return suspended.refusal

        approval = pending.create(
            session_id=session_id,
            origin_path=(request.origin_path or "").strip(),
            origin_actor=(request.origin_actor or "").strip(),
            execution_context=request.execution_context,
            capability_class=MUTATE_REMOTE,
            scope=prompt.scope or getattr(resolution, "scope", ""),
            hosts=prompt.hosts,
            command=request.command,
            payload=prompt.text,
            target_digest=prompt.target_digest,
            timeout_s=float(gates.approval_timeout_s),
        )
        logger.info(
            "gates: action {} waits for an approval on a second path (session {}, {} host(s))",
            approval.request_id,
            session_id,
            approval.host_count,
        )
        # The wait blocks a worker thread and never the event loop. The connection stays open,
        # so the same connection that submitted the action also executes it.
        outcome = await asyncio.to_thread(pending.wait, approval.request_id)

        if outcome.state is ApprovalState.DENIED:
            return self._refuse(
                server,
                request,
                resolution,
                f"an operator denied this action: {outcome.reason or 'no reason given'}.",
                actor=outcome.actor,
                approval_path=outcome.approval_path,
                origin_path=request.origin_path,
            )
        if outcome.state is not ApprovalState.APPROVED or outcome.token_nonce is None:
            return self._refuse(
                server,
                request,
                resolution,
                outcome.reason or "the approval did not arrive, so the action refused.",
                decision=_DECISION_EXPIRED,
                origin_path=request.origin_path,
            )

        verification = tokens.consume(
            nonce=outcome.token_nonce,
            session_id=session_id,
            target_digest=prompt.target_digest,
        )
        if not verification.ok:
            return self._refuse(
                server,
                request,
                resolution,
                f"the approval token did not verify at execution ({verification.refusal}). "
                "An approval covers one session and one resolved action.",
                actor=outcome.actor,
                approval_path=outcome.approval_path,
                origin_path=request.origin_path,
            )

        written = self._record(
            _DECISION_ALLOW,
            server,
            request,
            reason=(
                f"{outcome.actor!r} approved this action on path {outcome.approval_path!r}, "
                f"and the request arrived on {request.origin_path!r}."
            ),
            resolution=resolution,
            actor=outcome.actor,
            approval_path=outcome.approval_path,
            origin_path=request.origin_path,
            approval_id=approval.request_id,
        )
        if written.refusal is not None:
            return written.refusal

        # The approval a human just gave is the authorization this action carries into the
        # credential decision (#39). One action costs one human decision.
        return await self._run(
            server,
            request,
            idle_override,
            resolution=resolution,
            decision_record=written.record,
            authorization=ActionAuthorization(
                approval_id=approval.request_id,
                actor=outcome.actor,
                approval_path=outcome.approval_path,
            ),
        )

    def _refuse(
        self,
        server: Any,
        request: ExecuteRequest,
        resolution: Any,
        reason: str,
        *,
        decision: str = _DECISION_DENIED,
        actor: str | None = None,
        approval_path: str | None = None,
        origin_path: str | None = None,
        terminal: bool = True,
    ) -> ExecuteResponse:
        """Record one refusal and return it. The record lands first, or the refusal says so.

        ``terminal`` travels to the tool, which latches the class for a terminal refusal alone.

        A refusal gets no completion record (#46). Nothing ran, and never ran and unknown are
        opposite facts for a reviewer.
        """
        written = self._record(
            decision,
            server,
            request,
            reason=reason,
            resolution=resolution,
            actor=actor,
            approval_path=approval_path,
            origin_path=origin_path,
        )
        if written.refusal is not None:
            return written.refusal
        return _withheld(server, request, reason, terminal=terminal)

    def _record(
        self,
        decision: str,
        server: Any,
        request: ExecuteRequest,
        *,
        reason: str,
        resolution: Any,
        capability_class: str = MUTATE_REMOTE,
        actor: str | None = None,
        approval_path: str | None = None,
        origin_path: str | None = None,
        secret_ref: str | None = None,
        grant_id: str | None = None,
        approval_id: str | None = None,
    ) -> _Written:
        """Write the audit record, or refuse the action when the write fails.

        The executor decides, so the executor records. #16 raises rather than swallow a write
        failure, so an action that nothing recorded does not run: the audit log is the only
        account of what this process did.

        A refusal records too, and it still refuses when the record fails. The caller then reads
        both facts.

        ``decision`` is the operator's vocabulary rather than an enum name, because #32 rebuilds
        a latch from these strings and #29 shows them to a person.

        ``capability_class`` defaults to the action's class. A credential decision passes its
        own class instead (#39), because one action holds two decisions and a reviewer must read
        which one refused.

        The answer carries the record that landed, because #46 appends the outcome as a second
        record and that record names the decision it follows.
        """
        if self.audit is None:
            return _Written()
        try:
            written: dict[str, Any] = self.audit.record(
                decision=decision,
                capability_class=capability_class,
                execution_context=request.execution_context,
                session_id=request.session_id,
                tool="execute_on_server",
                scope=getattr(resolution, "scope", None),
                hosts=list(getattr(resolution, "hosts", ()) or ()),
                secret_ref=secret_ref,
                command=request.command,
                reason=reason or None,
                actor=actor,
                origin_path=origin_path,
                approval_path=approval_path,
                grant_id=grant_id,
                approval_id=approval_id,
            )
        except OSError as exc:
            return _Written(
                refusal=_error(
                    f"The executor did not act on {server.name!r}. The gate decided, and the "
                    f"audit record could not be written ({exc}). An action that nothing records "
                    "does not run."
                )
            )
        return _Written(record=written)

    def _record_completion(
        self,
        decision_record: dict[str, Any] | None,
        *,
        exit_code: int | None,
        started: float,
        reason: str,
    ) -> None:
        """Append the outcome of one action as a second record (#46).

        The decision record already landed, and it landed before the action ran. So a failed
        write here costs the outcome record alone, and this method logs it rather than raise.
        The alternative refuses an action that already reached the host, which hides a real
        result and repairs nothing.

        ``started`` is a ``time.monotonic`` reading from the moment before the transport ran. A
        monotonic clock cannot run backwards, so the duration cannot come out negative.
        """
        if self.audit is None or decision_record is None:
            return
        duration_ms = int((time.monotonic() - started) * 1000)
        try:
            self.audit.record_completion(
                follows=decision_record,
                exit_code=exit_code,
                duration_ms=duration_ms,
                reason=reason,
            )
        except OSError:
            logger.exception(
                "gates: the executor could not record the outcome of the action it recorded as "
                "{}",
                decision_record.get("record_id"),
            )

    def _gate(
        self, server: Any, request: ExecuteRequest, servers: ServerStore
    ) -> tuple[Decision, Any]:
        """Ask the gate, and hand back the resolution so the record can name the hosts.

        The executor verifies, and it does not trust the caller's word about the target.
        """
        interactive = request.execution_context == EXECUTION_CONTEXT_INTERACTIVE

        try:
            resolution = resolve_scope(server)
        except ScopeResolutionError as exc:
            # No context is tolerated here, interactive included (#37). This branch used to
            # allow an interactive action, on the reasoning that the guard already refuses a
            # pattern it cannot expand. The guard does not: an `inventoryHost` holding a plain
            # address takes the single-address path, passes, and then fails resolution when the
            # config names no local inventory. The action then ran with a host set nothing had
            # seen, and the `all` refusal never ran either, because it needs a resolution.
            #
            # #37 also removed the reason this was tolerated. The resolver now asks ansible from
            # its own configuration when no local inventory exists, which is the inventory the
            # play will use, so a deployment that relies on ansible.cfg still resolves.
            return (
                Decision(
                    Outcome.DENY,
                    f"The host set did not resolve, so the blast radius is unknown ({exc}). "
                    "Add an inventory the resolver can read, or install ansible-core so the "
                    "resolver can ask ansible for its own configuration.",
                ),
                None,
            )

        if resolution.scope == ALL:
            return (
                Decision(
                    Outcome.DENY,
                    "The pattern names an unbounded host set, so its scope is `all`. No policy "
                    "permits `all` scope, and no approval path exists for it.",
                ),
                resolution,
            )

        decision = evaluate(
            self.gates_loader(),
            capability_class=MUTATE_REMOTE,
            scope=resolution.scope,
            execution_context=request.execution_context,
            hosts=resolution.hosts,
            command=request.command,
            servers=servers,
        )
        if decision.outcome is Outcome.APPROVE and not interactive:
            # #8's rule, and #38 does not relax it. Nobody waits on an unattended turn, so a
            # prompt there becomes a hang or a rubber stamp. An operator who wrote `approve`
            # for an unattended context reads which key to change.
            return (
                Decision(
                    Outcome.DENY,
                    f"{MUTATE_REMOTE} at {resolution.scope} scope is 'approve' for an "
                    f"unattended context, and no person waits on this turn. A runtime approval "
                    "there is a hang or a rubber stamp, so set "
                    f"gates.unattended.mutate.remote.{resolution.scope} to 'grant' and declare "
                    "a standing grant.",
                ),
                resolution,
            )
        return decision, resolution

    def _credential_gate(
        self,
        server: Any,
        request: ExecuteRequest,
        resolution: Any,
        authorization: ActionAuthorization,
    ) -> ExecuteResponse | None:
        """Decide one decryption and record it -- #39. Returns None when the credential resolves.

        A refusal names the class and the server's ``secretRef``, so an operator reads which
        credential stayed encrypted. The record answers "which approval authorized this
        decryption?": it holds the decision, the ``secretRef``, and the grant or the approval
        that satisfied the class.

        No record holds the plaintext, and this code never sees one. It runs before the secret
        store opens (#18).
        """
        decision = evaluate_credential_access(
            self.gates_loader(),
            execution_context=request.execution_context,
            authorization=authorization,
        )
        allowed = decision.outcome is Outcome.ALLOW
        reason = f"{decision.reason} The server names secret {server.secret_ref!r}."
        written = self._record(
            _DECISION_ALLOW if allowed else _DECISION_DENIED,
            server,
            request,
            reason=reason,
            resolution=resolution,
            capability_class=CREDENTIAL_ACCESS,
            secret_ref=server.secret_ref,
            grant_id=decision.grant_id,
            approval_id=decision.approval_id,
            actor=authorization.actor,
            approval_path=authorization.approval_path,
            origin_path=request.origin_path,
        )
        if written.refusal is not None:
            return written.refusal
        return None if allowed else _withheld(server, request, reason)

    async def _run(
        self,
        server: Any,
        request: ExecuteRequest,
        idle_override: int | None,
        *,
        resolution: Any,
        decision_record: dict[str, Any] | None,
        authorization: ActionAuthorization,
    ) -> ExecuteResponse:
        """Decide the credential, resolve it, dial the host, and record the outcome.

        ``authorization`` is what already authorized this action. Every argument is required,
        so a new call site cannot reach a decryption with the authorization left out (#39).

        ``decision_record`` is the record that authorized this action, and the completion record
        names it (#46). It is required for the same reason: a new call site that ran an action
        and reported no outcome would put the log back where #46 found it.
        """
        backend, default_idle = _backend_for(server.provider_id)
        idle_timeout = idle_override if idle_override is not None else default_idle

        secret_value: str | None = None
        if server.secret_ref:
            # The credential decision, before the store opens and before a transport does
            # (#39). A server with no secretRef reaches no decision at all, because the class
            # covers the decryption rather than the action.
            refusal = self._credential_gate(server, request, resolution, authorization)
            if refusal is not None:
                return refusal
            # The one place a plaintext exists, and it exists only in this process (#18).
            secret_value = SecretStore(self.workspace).resolve_plaintext(server.secret_ref)
            if secret_value is None:
                return _error(
                    f"Server {server.name!r} references secret {server.secret_ref!r}, "
                    "which no longer exists."
                )

        jobs = JobStore(self.workspace)
        job = jobs.create(
            server_id=server.id,
            provider_id=server.provider_id,
            command=request.command,
            timeout_s=idle_timeout,
        )
        jobs.mark_running(job.id)

        tracker = IdleTimeoutTracker(
            idle_timeout_s=idle_timeout, absolute_ceiling_s=ABSOLUTE_CEILING_S
        )
        streams = server.provider_id in _STREAMING_PROVIDERS
        partial = BoundedOutput()
        last_persist = time.monotonic()

        def on_activity(chunk: str) -> None:
            nonlocal last_persist
            tracker.touch(chunk)
            if not streams:
                return
            partial.append(chunk)
            now = time.monotonic()
            if now - last_persist < _PARTIAL_OUTPUT_PERSIST_INTERVAL_S:
                return
            last_persist = now
            with contextlib.suppress(OSError, KeyError, ValueError):
                jobs.update_output(job.id, partial.text())

        # The clock starts here, so the duration covers the transport and nothing else (#46).
        started = time.monotonic()
        try:
            result = await run_with_idle_timeout(
                backend.run(server, request.command, secret_value, on_activity=on_activity),
                tracker,
                partial_output=(partial.text if streams else None),
            )
        except BaseException:
            # A lost transport, and a cancelled or killed executor, all land here. The action
            # ended and this process never read an exit code, so the outcome is unknown. A
            # reviewer needs that record, because unknown and never ran are opposite facts.
            # The re-raise leaves every existing error path in place.
            self._record_completion(
                decision_record,
                exit_code=None,
                started=started,
                reason=_completion_reason(
                    server.name, "ended with no answer from the transport", None
                ),
            )
            raise
        output = truncate_output(result.output)

        if result.timed_out:
            caveat = (
                " The remote command may still run, and this side cannot confirm it stopped."
                if server.provider_id in _UNSTOPPABLE_ON_TIMEOUT
                else ""
            )
            jobs.complete(
                job.id, exit_code=None, output=output, error=f"Timed out.{caveat}",
                status="timed_out",
            )
            self._record_completion(
                decision_record,
                exit_code=None,
                started=started,
                reason=_completion_reason(server.name, "timed out", None) + caveat,
            )
            return ExecuteResponse(
                ok=False, output=output, exit_code=None,
                error=f"Timed out running the command on {server.name!r}.{caveat}", reason="",
            )
        if result.error:
            jobs.complete(
                job.id, exit_code=result.exit_code, output=output, error=result.error,
                status="failed",
            )
            # The reason names no transport message and no output. A backend error text can
            # carry either one, and #16 keeps a digest of the command for that reason.
            self._record_completion(
                decision_record,
                exit_code=result.exit_code,
                started=started,
                reason=_completion_reason(server.name, "failed", result.exit_code),
            )
            return ExecuteResponse(
                ok=False, output=output, exit_code=result.exit_code,
                error=f"Failed on {server.name!r}: {result.error}", reason="",
            )

        jobs.complete(job.id, exit_code=result.exit_code, output=output, error=None,
                      status="completed")
        self._record_completion(
            decision_record,
            exit_code=result.exit_code,
            started=started,
            reason=_completion_reason(server.name, "ended", result.exit_code),
        )
        return ExecuteResponse(
            ok=True, output=output, exit_code=result.exit_code, error=None, reason="",
        )


def _error(message: str) -> ExecuteResponse:
    return ExecuteResponse(ok=False, output="", exit_code=None, error=message, reason="")


def _completion_reason(server_name: str, ending: str, exit_code: int | None) -> str:
    """State how one action ended, in the words a reviewer reads (#46).

    ``ending`` is a short phrase that names the end, such as ``ended`` or ``timed out``.

    A missing exit code is a fact of its own, so the text names it. One helper writes that
    clause, so four endings cannot word it four ways. The reason holds no command output and no
    transport message, because both can carry a secret.
    """
    unknown = "" if exit_code is not None else ", so the exit code is unknown"
    return f"the action {ending} on {server_name!r}{unknown}."


def _withheld(
    server: Any, request: ExecuteRequest, reason: str, *, terminal: bool = True
) -> ExecuteResponse:
    """The shape of every gate refusal: no error, one reason, and the resolved action.

    The tool renders this as a withheld action rather than an ordinary error, so the refusal
    becomes terminal and the latch forms (#15).

    ``terminal`` is False when the refusal describes the deployment rather than the action. No
    approver exists, or a payload cannot render, and the agent can change nothing about its request
    to pass. A latch there costs the operator a clear for their own config, and it teaches them to
    clear a latch without reading it (#42).
    """
    return ExecuteResponse(
        ok=False,
        output=_preview_line(server, request.command),
        exit_code=None,
        error=None,
        reason=reason,
        terminal=terminal,
    )


_MAX_LABELLED_HOSTS = 8


def _preview_line(server: Any, command: str) -> str:
    """The resolved action in one line, including the blast radius.

    The host list is not decoration. An operator reads this line after a refusal to learn which
    grant to write, and a group that names three hosts must say which three. #18 dropped this
    briefly, and a group preview then named no host and not even the pattern.

    A cap keeps a thousand-host pattern from flooding a result.
    """
    return (
        f"Preview (not executed): server={server.name!r} (id={server.id!r}) "
        f"provider={server.provider_id!r} command={command!r} target={_targets_label(server)}"
    )


def _targets_label(server: Any) -> str:
    """What the guard validated, as text. Falls back to the reason it could not resolve."""
    try:
        resolution = resolve_scope(server)
    except ScopeResolutionError as exc:
        return f"unresolved ({exc})"
    hosts = list(resolution.hosts)
    shown = ", ".join(hosts[:_MAX_LABELLED_HOSTS])
    more = f" +{len(hosts) - _MAX_LABELLED_HOSTS} more" if len(hosts) > _MAX_LABELLED_HOSTS else ""
    pattern = f"{resolution.pattern!r} -> " if resolution.pattern else ""
    return f"{pattern}{len(hosts)} host(s): {shown}{more}"


def _guard(server: Any) -> str | None:
    """Validate what this process will dial, or say why it cannot.

    ssm carries no dialed address and IAM authorizes it instead, so it has nothing to check
    here. Every other provider that names an address gets that address checked.
    """
    config: dict[str, str] = server.config
    if server.provider_id == "ssh":
        target = config.get("host")
    elif server.provider_id == "api":
        from urllib.parse import urlparse

        target = urlparse(config.get("baseUrl", "")).hostname
    elif server.provider_id == "ansible-runner":
        # Both fields are patterns to ansible. AnsibleRunnerBackend passes
        # `inventoryHost or group` as host_pattern, and resolve_scope expands the same field.
        # So the guard expands whichever one the backend will use, and checks every host.
        #
        # `inventoryHost` used to take the single-address path, which validated a label. A
        # label naming three hosts was checked as one name, and it failed closed only by
        # accident of DNS. A group label that did resolve to a permitted address would have
        # passed the guard while the play ran against every host in the group.
        target = config.get("inventoryHost")
        if target or config.get("group"):
            try:
                hosts = resolve_scope(server).hosts
            except ScopeResolutionError as exc:
                if target:
                    # No local inventory to read. Ansible still reads ansible.cfg and
                    # /etc/ansible/hosts, and the resolver cannot see either, so fall back to
                    # the single-address check this field had before. That keeps interactive
                    # runs working, and a group name still fails closed because it does not
                    # resolve in DNS.
                    ok, error = validate_server_target(target)
                    return None if ok else f"Refusing to execute: {error}"
                return f"Cannot validate network target: {exc}"
            for host in hosts:
                ok, error = validate_server_target(host)
                if not ok:
                    return f"Refusing to execute: {error} One blocked host refuses all of them."
            return None
    else:
        return None

    if not target:
        return "Cannot validate network target: the server config names no address to check."
    ok, error = validate_server_target(target)
    return None if ok else f"Refusing to execute: {error}"


def _backend_for(provider_id: str) -> tuple[Any, int]:
    """Import one backend lazily. Each optional library stays optional."""
    if provider_id == "ssh":
        from nanoinfra.servers.execution.ssh_backend import DEFAULT_IDLE_TIMEOUT_S, SSHBackend

        return SSHBackend(), DEFAULT_IDLE_TIMEOUT_S
    if provider_id == "ansible-runner":
        from nanoinfra.servers.execution.ansible_backend import (
            DEFAULT_IDLE_TIMEOUT_S,
            AnsibleRunnerBackend,
        )

        return AnsibleRunnerBackend(), DEFAULT_IDLE_TIMEOUT_S
    if provider_id == "ssm":
        from nanoinfra.servers.execution.ssm_backend import DEFAULT_IDLE_TIMEOUT_S, SSMBackend

        return SSMBackend(), DEFAULT_IDLE_TIMEOUT_S
    from nanoinfra.servers.execution.api_backend import DEFAULT_IDLE_TIMEOUT_S, ApiBackend

    return ApiBackend(), DEFAULT_IDLE_TIMEOUT_S


def serve_forever(
    socket_path: Path | str,
    *,
    workspace: Path | str,
    max_requests: int | None = None,
    operator_socket_path: Path | str | None = None,
    scrub_socket_path: Path | str | None = None,
) -> None:
    """Bind the three sockets and serve until terminated.

    Three listeners run. The execute socket takes requests from the agent. The operator socket
    takes answers from a human (#38), and only the executor owns it. The scrub socket takes one
    transcript text at a time from the agent (#41), because this process holds the sentinels.

    The scrub socket binds before the execute socket. A caller that waits for the execute socket
    therefore finds the scrub socket bound, and no early turn withholds its text for a socket
    that is one moment late.

    Each connection gets its own thread. A pending approval holds one connection for the whole
    wait, so a serial loop would let one unanswered action stop every other action. That is a
    denial of service on the whole agent.

    ``max_requests`` counts accepted execute connections, and it exists for tests. The answer to
    the last connection may land after this call returns, because the handler owns its own
    thread. Production passes nothing and the loop runs until the supervisor stops the process.

    The socket files are removed on exit. A stale file blocks the next bind, and a supervisor
    that restarts the executor must not need a human to delete one.
    """
    path = Path(socket_path)
    # A private mode only on a directory this process creates. A two-uid deployment owns that
    # decision: with separate accounts the directory is owned by the executor and carries setgid
    # plus group traversal (2710), so the agent account can reach a known socket name without
    # listing the directory or creating anything in it. A blanket chmod here would lock the agent
    # out, and a split the agent cannot talk to is worse than the mode it replaced.
    if not path.parent.exists():
        path.parent.mkdir(parents=True)
        os.chmod(path.parent, _SOCKET_DIR_MODE)
    if path.exists():
        path.unlink()

    audit = _audit_store()
    pending = PendingApprovalStore()
    tokens = ApprovalTokenStore()
    executor = Executor(
        workspace=Path(workspace), audit=audit, pending=pending, tokens=tokens
    )
    service = ApprovalService(pending=pending, tokens=tokens, audit=audit)

    operator_path = (
        Path(operator_socket_path)
        if operator_socket_path is not None
        else default_operator_socket_path(path)
    )
    # The bind happens here rather than in the thread. A bind failure is a deployment fault, and
    # it must stop this process. An executor that serves requests and answers none of them would
    # suspend every unusual action and then expire it.
    operator_listener = bind_operator_socket(operator_path)
    operator_thread = threading.Thread(
        target=serve_operator_socket,
        args=(operator_listener, service),
        name="nanoinfra-operator-listener",
        daemon=True,
    )
    operator_thread.start()

    scrub_path = (
        Path(scrub_socket_path)
        if scrub_socket_path is not None
        else default_scrub_socket_path(path)
    )
    # The same rule as the operator socket. A scrub socket nobody bound makes every persist
    # withhold its text, so the failure must stop this process rather than degrade every turn.
    scrub_listener = bind_scrub_socket(scrub_path)
    threading.Thread(
        target=serve_scrub_socket,
        args=(scrub_listener, Path(workspace)),
        name="nanoinfra-scrub-listener",
        daemon=True,
    ).start()

    served = 0
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(8)
        logger.info("gates: executor listening on {}", path)
        try:
            while max_requests is None or served < max_requests:
                conn, _ = server.accept()
                served += 1
                threading.Thread(
                    target=_serve_one_connection,
                    args=(conn, executor),
                    name="nanoinfra-executor",
                    daemon=True,
                ).start()
        finally:
            operator_listener.close()
            scrub_listener.close()
            with contextlib.suppress(OSError):
                operator_path.unlink()
            with contextlib.suppress(OSError):
                scrub_path.unlink()
            with contextlib.suppress(OSError):
                path.unlink()


def _serve_one_connection(conn: socket.socket, executor: Executor) -> None:
    """Own one connection for its whole life, and close it at the end."""
    with conn:
        _serve_one(conn, executor)


def _audit_store() -> Any:
    """The audit store this process writes to (#16).

    It lives beside the gateway's data rather than in the workspace. The workspace is reachable
    by the agent's filesystem tools, and an audit log a model can edit is not an audit log.

    ``pin_root`` holds the device and inode of that directory for the life of this process
    (#36). A model cannot un-append a record, and it could still rename the directory that
    holds every record, because write rights on a parent allow a rename of any entry inside it.
    entrypoint.sh takes that write right away. This pin covers the case the layout cannot: an
    executor that keeps running while its audit root moves underneath it. The store then raises
    on every read and every write, ``_record`` refuses the action and names the cause, and the
    latch restore degrades and keeps every session latched. The cost is availability, and the
    alternative cost is every latch the log holds.
    """
    from nanoinfra.config.paths import get_data_dir
    from nanoinfra.gates.audit import AuditStore

    store = AuditStore(get_data_dir() / "gates", config=load_policy().audit, pin_root=True)
    logger.info("gates: executor audit root {} pinned as {}", store.root, store.pinned_identity)
    return store


def _serve_one(conn: socket.socket, executor: Executor) -> None:
    """Answer one connection. A bad frame gets a refusal, and never a crash.

    A peer that speaks nonsense must not take the executor down. The executor is the only way
    to reach a host, so its availability is part of the control.
    """
    try:
        payload = read_frame(conn)
        request = decode_request(payload)
    except ProtocolError as exc:
        logger.warning("gates: executor refused a frame: {}", exc)
        with contextlib.suppress(OSError, ProtocolError):
            write_frame(conn, encode_response(_error(f"Malformed request: {exc}")))
        return

    try:
        response = asyncio.run(executor.handle(request))
    except Exception as exc:  # noqa: BLE001 -- one bad request must not end the process
        logger.exception("gates: executor failed a request")
        response = _error(f"The executor failed this request: {exc}")

    with contextlib.suppress(OSError, ProtocolError):
        write_frame(conn, encode_response(response))


__all__ = ["Executor", "serve_forever"]
