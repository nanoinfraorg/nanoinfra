"""One Dream run, driven from one place.

Dream had two drivers: a cron branch in ``nanoinfra/cli/gateway_runtime.py`` and ``/dream`` in
``nanoinfra/command/builtin.py``. They were near-copies, and every divergence found in the review of
this subsystem was a difference between the two:

- ``/dream`` started a run with ``asyncio.create_task`` and no in-flight flag. ``process_direct``
  locks per session key and ``dream_session_key()`` differs per run, so two runs did not serialize.
  Both called ``build_dream_prompt()``, so both got the same batch and the same end cursor, and the
  later run's prompt was rendered before the earlier run's write. The later run overwrote the
  earlier one, **both reported completion, both advanced the cursor, and the source entries were
  retired**. The fact the first run had written was then held by nothing (#106).
- cron passed ``_system_job_metadata`` and ``/dream`` passed nothing, so the same work ran as
  automation from one door and at interactive privilege from the other (#120).

A lock in one of two copies is a lock in neither, so this module owns the run and both callers ask
it for one. The lock is in-process, which is the case that exists: cron cannot overlap cron, because
``cron/service.py`` re-arms its timer only after the job returns, and both doors live in the same
gateway process. Two gateways over one workspace would still overlap, and that is a wider problem
than this lock -- ``memory/MEMORY.md`` has no cross-process lock at all.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

#: Held for the whole of one run, so a second caller can see that a run is in flight instead of
#: starting one beside it.
_DREAM_RUN_LOCK = asyncio.Lock()


def dream_run_in_progress() -> bool:
    """True while a Dream run holds the lock."""
    return _DREAM_RUN_LOCK.locked()


def dream_turn_metadata(*, message: str = "memory consolidation") -> dict[str, Any]:
    """The turn metadata for a Dream run, whichever door started it.

    #5 classifies a turn from its metadata and #8 refuses a remote command in an unattended one, so
    a run that passes no metadata runs at interactive privilege. Both doors pass this, because the
    work is the same work and the classification cannot depend on who asked (#120).
    """
    from nanoinfra.cron.session_turns import CRON_TRIGGER_META

    run_id = f"dream:{int(time.time() * 1000)}"
    return {
        CRON_TRIGGER_META: {
            "job_id": "dream",
            "job_name": "dream",
            "run_id": run_id,
            "prompt_ref": "system:dream",
            "persist_content": f"Scheduled system job triggered: dream\n\n{message}",
        }
    }


@dataclass
class DreamRunOutcome:
    """What one request for a Dream run produced.

    ``started`` is False for a request that ran nothing: another run held the lock, or there was no
    unconsolidated history. A caller reports those rather than treating them as a failure.
    """

    started: bool
    reason: str
    completed: bool = False
    diff_body: str = ""
    elapsed: float = 0.0
    commit_sha: str | None = None
    error: str | None = None
    last_cursor: int | None = None
    response: Any = None


async def run_dream(
    *,
    store: Any,
    agent: Any,
    commit_prefix: str = "dream: memory consolidation",
    timezone_name: str | None = None,
) -> DreamRunOutcome:
    """Consolidate one batch of history into durable memory.

    Returns rather than raises, because both callers report an outcome to somebody: cron to the log
    and ``/dream`` to a chat. An exception inside the run is carried in ``error`` and the cursor is
    left where it was, so the batch is offered again.
    """
    if _DREAM_RUN_LOCK.locked():
        return DreamRunOutcome(started=False, reason="in_progress")

    async with _DREAM_RUN_LOCK:
        return await _run_dream_locked(
            store=store,
            agent=agent,
            commit_prefix=commit_prefix,
            timezone_name=timezone_name,
        )


async def _run_dream_locked(
    *,
    store: Any,
    agent: Any,
    commit_prefix: str,
    timezone_name: str | None,
) -> DreamRunOutcome:
    from nanoinfra.agent.memory import DreamRunProgress, MemoryStore

    progress = DreamRunProgress()
    started_at = time.monotonic()
    response: Any = None
    diff_body = ""
    completed = False
    error: str | None = None
    last_cursor: int | None = None

    result = store.build_dream_prompt()
    if result is None:
        return DreamRunOutcome(started=False, reason="no_input")
    prompt, last_cursor = result

    # A durable file already changed before this run started -- a person's edit, or a half-applied
    # edit from a crashed run. `auto_commit` stages every tracked file, so leaving it here would fold
    # it into this run's commit: attributed to Dream, listed by `/dream-restore`, and revertable as
    # though Dream had written it (#111). It gets its own commit, under a message that says what it
    # is, so what follows is Dream's alone.
    _commit_pre_existing_edits(store)

    try:
        response = await agent.process_direct(
            prompt,
            session_key=MemoryStore.dream_session_key(),
            ephemeral=True,
            tools=store.build_dream_tools(),
            on_progress=progress,
            runtime=agent.dream_runtime(),
            metadata=dream_turn_metadata(),
        )
        # The real file delta grounds the audit record; clean completion decides whether this
        # history batch has finished processing.
        diff_body = store.dream_content_diff()
        completed = MemoryStore.dream_run_completed(
            response,
            ended_in_error=progress.ended_in_error,
        )
        if completed:
            store.set_last_dream_cursor(last_cursor)
    except Exception as exc:  # noqa: BLE001 - reported to a caller, never swallowed
        error = str(exc)
        logger.exception("Dream run failed")

    commit_sha = _finish_dream_run(
        store=store,
        agent=agent,
        response=response,
        diff_body=diff_body,
        commit_prefix=commit_prefix,
        timezone_name=timezone_name,
    )
    return DreamRunOutcome(
        started=True,
        reason="failed" if error else ("completed" if completed else "incomplete"),
        completed=completed,
        diff_body=diff_body,
        elapsed=time.monotonic() - started_at,
        commit_sha=commit_sha,
        error=error,
        last_cursor=last_cursor,
        response=response,
    )


def _finish_dream_run(
    *,
    store: Any,
    agent: Any,
    response: Any,
    diff_body: str,
    commit_prefix: str,
    timezone_name: str | None,
) -> str | None:
    """Record usage, commit a real edit, compact and prune.

    Every step is guarded on its own, because this used to be a ``finally`` block: one raising step
    skipped the rest, and in ``/dream`` it also left the "Dreaming..." message unresolved (#121).
    """
    from nanoinfra.agent.memory import MemoryStore
    from nanoinfra.webui.token_usage import record_response_token_usage

    try:
        record_response_token_usage(response, source="dream", timezone_name=timezone_name)
    except Exception:
        logger.exception("Dream: recording token usage failed")

    commit_sha: str | None = None
    try:
        # A no-op run does not enter the commit path at all, so an empty diff never produces a
        # commit whose body says nothing changed.
        if diff_body and store.git.is_initialized():
            commit_sha = store.git.auto_commit(
                MemoryStore.build_dream_commit_message(commit_prefix, diff_body)
            )
    except Exception:
        logger.exception("Dream: committing durable memory failed")

    try:
        store.compact_history()
    except Exception:
        logger.exception("Dream: compacting history failed")

    try:
        MemoryStore.prune_dream_sessions(agent.sessions.sessions_dir)
    except Exception:
        logger.exception("Dream: pruning session files failed")

    return commit_sha


def _commit_pre_existing_edits(store: Any) -> None:
    """Commit durable memory edits that were already there, under a non-Dream message."""
    try:
        if not store.git.is_initialized():
            return
        pending = store.dream_content_diff()
        if not pending:
            return
        sha = store.git.auto_commit(
            "chore: memory edits made outside a Dream run\n\n" + pending
        )
        if sha:
            logger.info("Dream: committed pre-existing memory edits as {}", sha)
    except Exception:
        logger.exception("Dream: committing pre-existing memory edits failed")
