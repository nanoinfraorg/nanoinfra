"""Execution helpers for session-bound cron jobs."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from loguru import logger

from nanoinfra.agent.tools.cron import CronTool
from nanoinfra.agent.turn_delivery import AUTOMATION_WITHHOLD_DELIVERY_META
from nanoinfra.automations.commissioning import COMMISSIONING_TURN_META
from nanoinfra.automations.delivery import normalize_policy, should_deliver
from nanoinfra.automations.state import AutomationDeliveryLog, response_fingerprint
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.cron.service import CronJobTerminalError
from nanoinfra.cron.session_delivery import origin_delivery_context
from nanoinfra.cron.session_turns import CRON_DEFER_UNTIL_IDLE_META, CRON_TRIGGER_META
from nanoinfra.cron.types import CronJob
from nanoinfra.cron.webui_metadata import cron_proactive_delivery_metadata
from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META, RuntimeContextBlock
from nanoinfra.session.automation_turns import AUTOMATION_SKILLS_META
from nanoinfra.utils.prompt_templates import render_template
from nanoinfra.webui.resource_mentions import (
    ResourceMentionResolver,
    UnresolvedMentionError,
    resource_mentions_runtime_context,
)

if TYPE_CHECKING:
    from nanoinfra.agent.tools.registry import ToolRegistry


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
) -> BoundTurn:
    """Render one bound cron job into the turn that runs it.

    Shared with commissioning (#183) on purpose. A rehearsal that built its own message would
    rehearse a different automation than the one that runs: the references, the declared skills
    and the delivery routing are all decided here.

    A reference that no longer resolves stops the run rather than letting the model fall back to
    matching on a name -- the fallback reintroduces exactly the ambiguity the reference removed,
    and there is nobody watching at 03:00.
    """
    reference_context: RuntimeContextBlock | None = None
    if job.references and workspace_path is not None:
        resolution = ResourceMentionResolver(workspace_path).resolve(job.references)
        try:
            resolution.require_all_resolved()
        except UnresolvedMentionError as exc:
            raise CronJobTerminalError(str(exc)) from exc
        reference_context = resource_mentions_runtime_context(resolution.resolved)

    prompt = render_template(
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
    if job.skills:
        metadata[AUTOMATION_SKILLS_META] = list(job.skills)
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
