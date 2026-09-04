"""Gateway delivery loop for local triggers."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

from nanoinfra.agent.automation_turns import AutomationTurnError
from nanoinfra.agent.tools.groups import ATTACHED_GROUPS_META
from nanoinfra.agent.turn_delivery import AUTOMATION_WITHHOLD_DELIVERY_META
from nanoinfra.automations.delivery import normalize_policy, should_deliver
from nanoinfra.automations.state import AutomationDeliveryLog, response_fingerprint
from nanoinfra.bus.events import InboundMessage, OutboundMessage
from nanoinfra.connectors.attachment import ATTACHED_CONNECTORS_META, RESOURCE_MENTIONS_META
from nanoinfra.cron.agent_binding import agent_addendum_prefix, job_agent_binding
from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META
from nanoinfra.session.automation_turns import (
    AUTOMATION_PRESETS_META,
    AUTOMATION_SKILLS_META,
    TURN_AGENT_META,
    automation_agent_metadata,
)
from nanoinfra.triggers.local_session_turns import LOCAL_TRIGGER_META
from nanoinfra.triggers.local_store import LocalTriggerStore
from nanoinfra.triggers.local_types import LocalTrigger, TriggerDelivery
from nanoinfra.webui.metadata import WEBUI_MESSAGE_SOURCE_METADATA_KEY, WEBUI_TURN_METADATA_KEY
from nanoinfra.webui.resource_mentions import (
    ResourceMentionResolver,
    UnresolvedMentionError,
    resource_mentions_runtime_context,
)


async def run_local_trigger_queue(
    *,
    store: LocalTriggerStore,
    submit_turn: Callable[[InboundMessage], Awaitable[OutboundMessage | None]] | None = None,
    is_channel_enabled: Callable[[str], bool],
    poll_interval_s: float = 0.5,
    batch_size: int = 20,
    delivery_log: AutomationDeliveryLog | None = None,
    publish: Callable[[OutboundMessage], Awaitable[None]] | None = None,
) -> None:
    """Poll local trigger deliveries and submit them as session turns."""
    if submit_turn is None:
        raise ValueError("run_local_trigger_queue requires submit_turn")
    logger.info("Local trigger queue started")
    recovered = store.recover_processing_deliveries()
    if recovered:
        logger.warning(
            "Trigger: recovered {} interrupted delivery file(s) from processing",
            recovered,
        )
    while True:
        deliveries = store.claim_deliveries(limit=batch_size)
        if not deliveries:
            await asyncio.sleep(poll_interval_s)
            continue

        for delivery in deliveries:
            try:
                await _deliver_delivery(
                    store,
                    delivery,
                    submit_turn=submit_turn,
                    is_channel_enabled=is_channel_enabled,
                    delivery_log=delivery_log,
                    publish=publish,
                )
                store.complete_delivery(delivery)
            except asyncio.CancelledError as exc:
                store.retry_delivery(delivery, str(exc) or exc.__class__.__name__)
                _write_delivery_run_record(
                    store,
                    delivery,
                    status="interrupted",
                    error=str(exc) or exc.__class__.__name__,
                )
                raise
            except _TerminalDeliveryError as exc:
                store.record_delivery(
                    delivery.trigger_id,
                    status="error",
                    error=str(exc),
                    run_at_ms=delivery.created_at_ms,
                )
                _write_delivery_run_record(
                    store,
                    delivery,
                    status="error",
                    error=str(exc),
                )
                store.complete_delivery(delivery)
                logger.warning(
                    "Trigger: dropped delivery {} for {}: {}",
                    delivery.id,
                    delivery.trigger_id,
                    exc,
                )
            except AutomationTurnError as exc:
                error = str(exc) or exc.__class__.__name__
                store.record_delivery(
                    delivery.trigger_id,
                    status="error",
                    error=error,
                    run_at_ms=delivery.created_at_ms,
                )
                _write_delivery_run_record(
                    store,
                    delivery,
                    status="error",
                    error=error,
                )
                store.complete_delivery(delivery)
                logger.warning(
                    "Trigger: delivery {} for {} reached the agent but failed: {}",
                    delivery.id,
                    delivery.trigger_id,
                    error,
                )
            except Exception as exc:
                error = str(exc) or exc.__class__.__name__
                retried = store.retry_delivery(delivery, error)
                _write_delivery_run_record(
                    store,
                    delivery,
                    status="retrying" if retried else "error",
                    error=error,
                )
                store.record_delivery(
                    delivery.trigger_id,
                    status="error",
                    error=error,
                    run_at_ms=delivery.created_at_ms,
                )
                logger.exception(
                    "Trigger: failed delivery {} for {}{}",
                    delivery.id,
                    delivery.trigger_id,
                    "; queued retry" if retried else "; moved to failed queue",
                )


class _TerminalDeliveryError(RuntimeError):
    pass


async def _deliver_delivery(
    store: LocalTriggerStore,
    delivery: TriggerDelivery,
    *,
    submit_turn: Callable[[InboundMessage], Awaitable[OutboundMessage | None]],
    is_channel_enabled: Callable[[str], bool],
    delivery_log: AutomationDeliveryLog | None = None,
    publish: Callable[[OutboundMessage], Awaitable[None]] | None = None,
) -> None:
    trigger = store.get(delivery.trigger_id)
    if trigger is None:
        raise _TerminalDeliveryError("trigger not found")
    if not trigger.enabled:
        raise _TerminalDeliveryError("trigger is disabled")
    if not is_channel_enabled(trigger.channel):
        raise _TerminalDeliveryError(f"target channel is not enabled: {trigger.channel}")

    # Alongside the other preconditions, and before the processing record is written: a reference
    # that no longer resolves is not something a retry fixes, and the model must not get a chance
    # to fall back to matching on a name. An agent config no longer has is the same shape of
    # failure, with a sharper consequence -- falling back to the deployment's default agent would
    # widen a trigger somebody narrowed on purpose (#257).
    binding = None
    if trigger.agent:
        try:
            binding = job_agent_binding(
                agent=trigger.agent,
                skills=trigger.skills,
                roster=store.named_agents,
                asked_by=f"automation:{trigger.id}",
            )
        except ValueError as exc:
            raise _TerminalDeliveryError(str(exc)) from exc

    reference_context = None
    resolved_references: list[dict[str, Any]] = []
    if trigger.references:
        resolution = ResourceMentionResolver(store.workspace_path).resolve(trigger.references)
        try:
            resolution.require_all_resolved()
        except UnresolvedMentionError as exc:
            raise _TerminalDeliveryError(str(exc)) from exc
        reference_context = resource_mentions_runtime_context(resolution.resolved)
        resolved_references = [mention.to_payload() for mention in resolution.resolved]

    store.write_delivery_run_record(delivery, trigger=trigger, status="processing")
    policy = normalize_policy(trigger.delivery)
    metadata = _delivery_metadata(trigger, delivery)
    if binding is not None:
        # Who acts, and the ceiling that travels with the turn. The name goes on the same seam a
        # person's chosen agent uses, so `AgentLoop` resolves it against the roster and the turn
        # record says which agent answered. Nothing is written when no agent is named: absent and
        # "the default agent" have to be one state.
        if binding.declared_tool_groups:
            metadata.update(automation_agent_metadata(binding.name, binding.tool_groups))
        else:
            # No ceiling declared, so no ceiling key: an empty tuple is what a binding holds in
            # both states, and writing it would cap a trigger nobody capped.
            metadata[TURN_AGENT_META] = binding.name
    declared_skills = list(binding.skills) if binding is not None else list(trigger.skills)
    if declared_skills:
        metadata[AUTOMATION_SKILLS_META] = declared_skills
    if trigger.mcp_presets:
        # The same key a mention writes, so an unattended turn reaches a `mention` server the only
        # way it can: by having declared it in advance (#204).
        metadata[AUTOMATION_PRESETS_META] = list(trigger.mcp_presets)
    if trigger.connectors:
        metadata[ATTACHED_CONNECTORS_META] = list(trigger.connectors)
    attached_groups = list(trigger.tool_groups)
    if binding is not None:
        # Attached as well as capped: a group set to `attach: mention` would otherwise withhold
        # its schemas from the very agent whose whole tool set it is, and nobody types `@servers`
        # into a trigger. This widens nothing -- the ceiling *is* this list.
        attached_groups += [
            group for group in binding.tool_groups if group not in attached_groups
        ]
    if attached_groups:
        metadata[ATTACHED_GROUPS_META] = attached_groups
    if reference_context is not None:
        metadata[RUNTIME_CONTEXT_INPUT_META] = [reference_context]
    if resolved_references:
        # The same key the composer writes for an `@server:` mention, so "the turn named this
        # server" is one fact with one reader (#226). Device memory is loaded from it, and it is
        # dropped before the turn is stored (`_LIVE_TURN_ONLY_META`) because a trigger re-resolves
        # its references when it fires.
        metadata[RESOURCE_MENTIONS_META] = resolved_references
    withholding = policy != "always" and publish is not None
    if withholding:
        metadata[AUTOMATION_WITHHOLD_DELIVERY_META] = True
    msg = InboundMessage(
        channel=trigger.channel,
        sender_id=trigger.sender_id,
        chat_id=trigger.chat_id,
        # The agent's own instructions in front of the message, because the message came from
        # whatever fired this trigger: trusted text precedes untrusted text.
        content=agent_addendum_prefix(binding) + delivery.content,
        metadata=metadata,
        session_key_override=trigger.session_key,
    )
    response = await submit_turn(msg)
    if withholding and response is not None and publish is not None:
        await _deliver_if_policy_allows(
            trigger,
            response,
            policy=policy,
            delivery_log=delivery_log,
            publish=publish,
        )
    store.record_delivery(
        trigger.id,
        status="ok",
        run_at_ms=delivery.created_at_ms,
    )
    _write_delivery_run_record(
        store,
        delivery,
        trigger=trigger,
        status="ok",
        response=response.content if response else "",
    )



async def _deliver_if_policy_allows(
    trigger: LocalTrigger,
    response: OutboundMessage,
    *,
    policy: str,
    delivery_log: AutomationDeliveryLog | None,
    publish: Callable[[OutboundMessage], Awaitable[None]],
) -> None:
    """Publish a withheld trigger response, or record that it was silenced and why."""
    content = response.content or ""
    fingerprint = response_fingerprint(content)
    last = delivery_log.last_fingerprint(trigger.id) if delivery_log else None
    deliver = should_deliver(
        policy,  # pyright: ignore[reportArgumentType]
        content=content,
        # A failed turn raises out of submit_turn, so the retry path owns it and never reaches here.
        failed=False,
        last_fingerprint=last,
        fingerprint=fingerprint,
    )
    if not deliver:
        logger.info(
            "Trigger: '{}' silenced by its '{}' delivery policy", trigger.name, policy
        )
        return
    await publish(response)
    if delivery_log is not None:
        # After the publish, not before: a failed send must not teach an on-change policy that
        # the operator had already been told.
        delivery_log.record(trigger.id, fingerprint)


def _write_delivery_run_record(
    store: LocalTriggerStore,
    delivery: TriggerDelivery,
    *,
    status: str,
    trigger: LocalTrigger | None = None,
    error: str | None = None,
    response: str | None = None,
) -> None:
    try:
        store.write_delivery_run_record(
            delivery,
            trigger=trigger,
            status=status,
            error=error,
            response=response,
        )
    except Exception:
        logger.exception(
            "Trigger: failed to write run record for delivery {}",
            delivery.id,
        )


def _delivery_metadata(trigger: LocalTrigger, delivery: TriggerDelivery) -> dict[str, Any]:
    metadata = dict(trigger.origin_metadata or {})
    metadata[LOCAL_TRIGGER_META] = {
        "trigger_id": trigger.id,
        "trigger_name": trigger.name,
        "delivery_id": delivery.id,
        "created_at_ms": delivery.created_at_ms,
        "persist_content": _history_content(trigger, delivery),
    }
    if trigger.channel == "websocket":
        metadata.pop(WEBUI_TURN_METADATA_KEY, None)
        metadata[WEBUI_TURN_METADATA_KEY] = f"trigger:{trigger.id}:{uuid.uuid4().hex}"
        source: dict[str, str] = {"kind": "local_trigger"}
        if trigger.name:
            source["label"] = trigger.name
        metadata[WEBUI_MESSAGE_SOURCE_METADATA_KEY] = source
    return metadata


def _history_content(trigger: LocalTrigger, delivery: TriggerDelivery) -> str:
    label = trigger.name.strip() if trigger.name else trigger.id
    return f"Local trigger received: {label}\n\n{delivery.content}"
