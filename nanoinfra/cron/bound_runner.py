"""Execution helpers for session-bound cron jobs."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from nanoinfra.agent.delegation import DelegateBinding
from nanoinfra.agent.tools.cron import CronTool
from nanoinfra.agent.tools.groups import ATTACHED_GROUPS_META
from nanoinfra.agent.turn_delivery import AUTOMATION_WITHHOLD_DELIVERY_META
from nanoinfra.automations.commissioning import COMMISSIONING_TURN_META
from nanoinfra.automations.delivery import normalize_policy, should_deliver
from nanoinfra.automations.state import AutomationDeliveryLog, response_fingerprint
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.connectors.attachment import ATTACHED_CONNECTORS_META, RESOURCE_MENTIONS_META
from nanoinfra.cron.agent_binding import (
    agent_addendum_prefix,
    job_agent_binding,
    roster_from_config,
)
from nanoinfra.cron.service import CronJobTerminalError
from nanoinfra.cron.session_delivery import origin_delivery_context
from nanoinfra.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META, CRON_TRIGGER_META
from nanoinfra.cron.types import CronJob
from nanoinfra.cron.webui_metadata import cron_proactive_delivery_metadata
from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META, RuntimeContextBlock
from nanoinfra.session.automation_turns import (
    AUTOMATION_PRESETS_META,
    AUTOMATION_SKILLS_META,
    automation_agent_metadata,
)
from nanoinfra.utils.prompt_templates import render_template
from nanoinfra.webui.resource_mentions import (
    ResourceMentionResolver,
    UnresolvedMentionError,
    resource_mentions_runtime_context,
)

if TYPE_CHECKING:
    from nanoinfra.agent.tools.registry import ToolRegistry
    from nanoinfra.config.schema import NamedAgentConfig


class BoundCronAgent(Protocol):
    tools: ToolRegistry

    async def submit_cron_turn(self, msg: InboundMessage) -> OutboundMessage | None:
        ...


class CronRunRecorder(Protocol):
    def write_run_record(self, run_id: str, record: dict[str, Any]) -> None:
        ...


def _cron_prompt_ref(prompt: str) -> dict[str, Any]:
    return {
        "id": "cron.agent_turn.reminder",
        "version": 1,
        "sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }


def _job_agent_binding(
    job: CronJob,
    *,
    named_agents: "Mapping[str, NamedAgentConfig] | None",
) -> DelegateBinding | None:
    """The agent this run acts as, or ``None`` for the deployment's default agent.

    A job whose agent config no longer accepts is a terminal failure, not a retry: nothing about
    running again fixes a name config does not have, and the run must not silently fall back to
    an agent with more reach than the one somebody chose.
    """
    if not job.agent:
        return None
    roster = named_agents if named_agents is not None else roster_from_config()
    try:
        return job_agent_binding(
            agent=job.agent,
            skills=job.skills,
            roster=roster,
            asked_by=f"automation:{job.id}",
        )
    except ValueError as exc:
        raise CronJobTerminalError(str(exc)) from exc


def _bound_session_delivery_context(
    job: CronJob,
    *,
    turn_seed: str,
    source_label: str | None,
) -> tuple[str, str, dict[str, Any]]:
    channel, chat_id, metadata = origin_delivery_context(job)

    if channel == "websocket":
        metadata["webui"] = True
        metadata.update(
            cron_proactive_delivery_metadata(
                "websocket",
                metadata,
                turn_seed=turn_seed,
                source_label=source_label,
            )
        )

    return channel, chat_id, metadata


@dataclass(frozen=True, slots=True)
class BoundTurn:
    """One cron job rendered as the turn that will run it."""

    channel: str
    chat_id: str
    prompt: str
    prompt_ref: dict[str, Any]
    run_id: str
    metadata: dict[str, Any]

    def message(self, *, session_key: str) -> InboundMessage:
        return InboundMessage(
            channel=self.channel,
            sender_id="cron",
            chat_id=self.chat_id,
            content=self.prompt,
            metadata=self.metadata,
            session_key_override=session_key,
        )


def build_bound_turn(
    job: CronJob,
    *,
    workspace_path: Path | None = None,
    commissioning_id: str | None = None,
    named_agents: "Mapping[str, NamedAgentConfig] | None" = None,
) -> BoundTurn:
    """Render one bound cron job into the turn that runs it.

    Shared with commissioning (#183) on purpose. A rehearsal that built its own message would
    rehearse a different automation than the one that runs: the references, the declared skills,
    the acting agent and the delivery routing are all decided here.

    A reference that no longer resolves stops the run rather than letting the model fall back to
    matching on a name -- the fallback reintroduces exactly the ambiguity the reference removed,
    and there is nobody watching at 03:00. An agent that no longer resolves stops the run for the
    same reason and a sharper one: falling back to the deployment's default agent would *widen* a
    job somebody narrowed on purpose.

    ``named_agents`` is read from config when the job names an agent and the caller passes none.
    A job that names none -- every job today -- never reaches that read.
    """
    binding = _job_agent_binding(job, named_agents=named_agents)
    reference_context: RuntimeContextBlock | None = None
    resolved_references: list[dict[str, Any]] = []
    if job.references and workspace_path is not None:
        resolution = ResourceMentionResolver(workspace_path).resolve(job.references)
        try:
            resolution.require_all_resolved()
        except UnresolvedMentionError as exc:
            raise CronJobTerminalError(str(exc)) from exc
        reference_context = resource_mentions_runtime_context(resolution.resolved)
        resolved_references = [mention.to_payload() for mention in resolution.resolved]

    prompt = agent_addendum_prefix(binding) + render_template(
        "agent/cron_reminder.md",
        strip=True,
        message=job.payload.message,
    )
    prompt_ref = _cron_prompt_ref(prompt)
    run_id = f"{job.id}:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}"
    channel, chat_id, metadata = _bound_session_delivery_context(
        job,
        turn_seed=f"cron:{job.id}",
        source_label=job.name,
    )
    metadata[CRON_TRIGGER_META] = {
        "job_id": job.id,
        "job_name": job.name,
        "run_id": run_id,
        "prompt_ref": prompt_ref,
        "persist_content": (
            f"Scheduled cron job triggered: {job.name}\n\n{job.payload.message}"
        ),
    }
    metadata[CRON_DEFER_UNTIL_IDLE_META] = True
    if reference_context is not None:
        metadata[RUNTIME_CONTEXT_INPUT_META] = [reference_context]
    if resolved_references:
        # The same key the composer writes for an `@server:` mention, so "the turn named this
        # server" is one fact with one reader (#226). Device memory is loaded from it, and it is
        # dropped before the turn is stored (`_LIVE_TURN_ONLY_META`) because a job re-resolves its
        # references when it fires.
        metadata[RESOURCE_MENTIONS_META] = resolved_references
    if binding is not None:
        # Who acts, and the ceiling that travels with the turn. The name goes on the same seam a
        # person's chosen agent uses, so `AgentLoop` resolves it against the roster and the turn
        # record says which agent answered -- an unattributed run of a narrowed job would be the
        # misattribution #248 exists to stop. Nothing is written when no agent is named: absent
        # and "the default agent" have to be one state.
        metadata.update(automation_agent_metadata(binding.name, binding.tool_groups))
    declared_skills = list(binding.skills) if binding is not None else list(job.skills)
    if declared_skills:
        metadata[AUTOMATION_SKILLS_META] = declared_skills
    if job.mcp_presets:
        # The same key a mention writes, so an unattended turn reaches a `mention` server the only
        # way it can: by having declared it in advance (#204).
        metadata[AUTOMATION_PRESETS_META] = list(job.mcp_presets)
    if job.connectors:
        metadata[ATTACHED_CONNECTORS_META] = list(job.connectors)
    attached_groups = list(job.tool_groups)
    if binding is not None:
        # The agent's own groups are attached as well as capped. A group set to `attach: mention`
        # otherwise withholds its schemas from the very agent whose whole tool set it is, and an
        # unattended turn has nobody to type `@servers`. Attaching them widens nothing: they are
        # inside the ceiling by definition, because the ceiling *is* this list.
        attached_groups += [
            group for group in binding.tool_groups if group not in attached_groups
        ]
    if attached_groups:
        metadata[ATTACHED_GROUPS_META] = attached_groups
    if commissioning_id is not None:
        metadata[COMMISSIONING_TURN_META] = commissioning_id
    return BoundTurn(
        channel=channel,
        chat_id=chat_id,
        prompt=prompt,
        prompt_ref=prompt_ref,
        run_id=run_id,
        metadata=metadata,
    )


async def run_bound_cron_job(
    job: CronJob,
    *,
    agent: BoundCronAgent,
    cron: CronRunRecorder,
    delivery_log: AutomationDeliveryLog | None = None,
    publish: Callable[[OutboundMessage], Awaitable[None]] | None = None,
    workspace_path: Path | None = None,
) -> str | None:
    """Execute a session-bound cron job as a normal agent session turn."""
    session_key = job.payload.session_key
    if not session_key:
        raise ValueError(f"cron job {job.id} is missing payload.session_key")

    turn = build_bound_turn(job, workspace_path=workspace_path)
    prompt = turn.prompt
    prompt_ref = turn.prompt_ref
    run_id = turn.run_id
    metadata = turn.metadata
    policy = normalize_policy(job.delivery)
    # Only withhold when there is a decision to make. Leaving the default path untouched means an
    # existing job's delivery keeps going through exactly the code it went through before.
    withholding = policy != "always" and publish is not None
    if withholding:
        metadata[AUTOMATION_WITHHOLD_DELIVERY_META] = True
    run_record_base: dict[str, Any] = {
        "job_id": job.id,
        "job_name": job.name,
        "session_key": session_key,
        "prompt_ref": prompt_ref,
        "prompt_vars": {"message": job.payload.message},
        "rendered_prompt": prompt,
    }

    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "queued",
        },
    )

    cron_tool = agent.tools.get("cron")
    cron_token = None
    if isinstance(cron_tool, CronTool):
        cron_token = cron_tool.set_cron_context(True)
    try:
        resp = await agent.submit_cron_turn(turn.message(session_key=session_key))
    except (Exception, asyncio.CancelledError) as exc:
        error_text = str(exc) or exc.__class__.__name__
        cron.write_run_record(
            run_id,
            {
                **run_record_base,
                "status": "error",
                "error": error_text,
            },
        )
        raise
    finally:
        if isinstance(cron_tool, CronTool) and cron_token is not None:
            cron_tool.reset_cron_context(cron_token)

    response = resp.content if resp else ""
    cron.write_run_record(
        run_id,
        {
            **run_record_base,
            "status": "ok",
            "response": response,
        },
    )
    if withholding and resp is not None and publish is not None:
        await _deliver_if_policy_allows(
            job,
            resp,
            policy=policy,
            delivery_log=delivery_log,
            publish=publish,
        )
    return response


async def _deliver_if_policy_allows(
    job: CronJob,
    response: OutboundMessage,
    *,
    policy: str,
    delivery_log: AutomationDeliveryLog | None,
    publish: Callable[[OutboundMessage], Awaitable[None]],
) -> None:
    """Publish a withheld cron response, or record that it was silenced and why."""
    content = response.content or ""
    fingerprint = response_fingerprint(content)
    last = delivery_log.last_fingerprint(job.id) if delivery_log else None
    deliver = should_deliver(
        policy,  # pyright: ignore[reportArgumentType]
        content=content,
        # A failure never reaches here: run_bound_cron_job raises, and the retry path owns it.
        failed=False,
        last_fingerprint=last,
        fingerprint=fingerprint,
    )
    if not deliver:
        logger.info("Cron: job '{}' silenced by its '{}' delivery policy", job.name, policy)
        return
    await publish(response)
    if delivery_log is not None:
        # Recorded only after a successful publish. Recording first would mean a failed send
        # taught an on-change policy that the operator had already been told.
        delivery_log.record(job.id, fingerprint)
