"""One Dream driver, and one run at a time -- nanoinfraorg/nanoinfra#106, #120.

`/dream` fired `asyncio.create_task(_run_dream())` with no in-flight flag. `process_direct`'s lock
is per session key and `dream_session_key()` differs per run, so it did not serialize two runs.
Both runs called `build_dream_prompt()`, so both got the same batch and the same end cursor, and
run B's prompt was rendered before A's write: B overwrote A, **both reported completion, both
advanced the cursor, and the source entries were retired**. The lost fact was unrecoverable.

Every divergence in that review was a difference between the cron driver in `gateway_runtime.py`
and the `/dream` driver in `builtin.py`, including the one in #120 where cron classified the turn as
unattended and `/dream` ran it at interactive privilege. A lock in one of two copies is a lock in
neither, so the two collapse into one function here.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanoinfra.agent.dream_run import (
    DreamRunOutcome,
    dream_run_in_progress,
    run_dream,
)
from nanoinfra.agent.memory import MemoryStore


def _store(tmp_path: Path, entries: int = 5) -> MemoryStore:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(workspace)
    store.write_soul("# Soul")
    store.write_memory("# Memory")
    for index in range(1, entries + 1):
        store.append_history(f"entry-{index:02d}")
    return store


def _agent(store: MemoryStore, tmp_path: Path, process_direct: Any) -> SimpleNamespace:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        context=SimpleNamespace(memory=store, timezone="UTC"),
        sessions=SimpleNamespace(sessions_dir=sessions_dir),
        process_direct=process_direct,
        dream_runtime=lambda: None,
    )


def _response() -> Any:
    from nanoinfra.bus.events import OutboundMessage

    return OutboundMessage(
        channel="cli",
        chat_id="direct",
        content="done",
        metadata={"_stop_reason": "completed"},
    )


class TestOneRunAtATime:
    @pytest.mark.asyncio
    async def test_a_second_run_is_refused_while_one_is_in_flight(self, tmp_path) -> None:
        store = _store(tmp_path)
        first_is_running = asyncio.Event()
        release = asyncio.Event()
        prompts: list[str] = []

        async def process_direct(prompt: str, *_args: Any, **_kwargs: Any) -> Any:
            prompts.append(prompt)
            first_is_running.set()
            await release.wait()
            return _response()

        agent = _agent(store, tmp_path, process_direct)
        first = asyncio.create_task(run_dream(store=store, agent=agent))
        await asyncio.wait_for(first_is_running.wait(), timeout=5)

        assert dream_run_in_progress() is True
        second = await run_dream(store=store, agent=agent)

        assert second.started is False
        assert second.reason == "in_progress"
        assert len(prompts) == 1, "the second run must not render a prompt of its own"

        release.set()
        outcome = await asyncio.wait_for(first, timeout=5)
        assert outcome.started is True
        assert dream_run_in_progress() is False

    @pytest.mark.asyncio
    async def test_a_refused_run_retires_no_batch(self, tmp_path) -> None:
        """The unrecoverable half: a run that wrote nothing must not advance the cursor."""
        store = _store(tmp_path)
        first_is_running = asyncio.Event()
        release = asyncio.Event()

        async def process_direct(_prompt: str, *_args: Any, **_kwargs: Any) -> Any:
            first_is_running.set()
            await release.wait()
            return _response()

        agent = _agent(store, tmp_path, process_direct)
        first = asyncio.create_task(run_dream(store=store, agent=agent))
        await asyncio.wait_for(first_is_running.wait(), timeout=5)

        await run_dream(store=store, agent=agent)
        cursor_after_refusal = store.get_last_dream_cursor()

        release.set()
        await asyncio.wait_for(first, timeout=5)

        assert cursor_after_refusal == 0
        assert store.get_last_dream_cursor() == 5, "the run that did the work advances the cursor"

    @pytest.mark.asyncio
    async def test_the_flag_clears_when_a_run_raises(self, tmp_path) -> None:
        """A lock that a failure leaves held would refuse every later run forever."""
        store = _store(tmp_path)

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("the provider refused")

        agent = _agent(store, tmp_path, process_direct)
        outcome = await run_dream(store=store, agent=agent)

        assert outcome.started is True
        assert outcome.completed is False
        assert outcome.error is not None
        assert dream_run_in_progress() is False


class TestBothDriversAgree:
    @pytest.mark.asyncio
    async def test_the_turn_carries_automation_metadata(self, tmp_path) -> None:
        """#120: cron passed `_system_job_metadata` and `/dream` passed nothing.

        The metadata is what classifies the turn, so the same work ran at interactive privilege
        from one door and as automation from the other. One function, one classification.
        """
        store = _store(tmp_path)
        seen: list[Any] = []

        async def process_direct(_prompt: str, *_args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("metadata"))
            return _response()

        agent = _agent(store, tmp_path, process_direct)
        await run_dream(store=store, agent=agent)

        assert len(seen) == 1
        assert isinstance(seen[0], dict)
        assert seen[0], "a turn with no metadata is classified as interactive"

    @pytest.mark.asyncio
    async def test_no_input_is_reported_rather_than_run(self, tmp_path) -> None:
        store = _store(tmp_path, entries=0)
        calls: list[str] = []

        async def process_direct(prompt: str, *_args: Any, **_kwargs: Any) -> Any:
            calls.append(prompt)
            return _response()

        agent = _agent(store, tmp_path, process_direct)
        outcome = await run_dream(store=store, agent=agent)

        assert outcome.started is False
        assert outcome.reason == "no_input"
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_completed_run_advances_the_cursor_and_reports_the_batch(
        self,
        tmp_path,
    ) -> None:
        store = _store(tmp_path)

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            return _response()

        agent = _agent(store, tmp_path, process_direct)
        outcome = await run_dream(store=store, agent=agent)

        assert isinstance(outcome, DreamRunOutcome)
        assert outcome.started is True
        assert outcome.completed is True
        assert outcome.last_cursor == 5
        assert store.get_last_dream_cursor() == 5
        assert outcome.elapsed >= 0

    @pytest.mark.asyncio
    async def test_an_incomplete_run_leaves_the_cursor_alone(self, tmp_path) -> None:
        from nanoinfra.bus.events import OutboundMessage

        store = _store(tmp_path)

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            return OutboundMessage(
                channel="cli",
                chat_id="direct",
                content="ran out of steps",
                metadata={"_stop_reason": "max_iterations"},
            )

        agent = _agent(store, tmp_path, process_direct)
        outcome = await run_dream(store=store, agent=agent)

        assert outcome.completed is False
        assert store.get_last_dream_cursor() == 0


class TestTheCommitRule:
    """Moved here with the rule it covers.

    ``gateway_runtime._commit_dream_changes`` held it and is gone: the rule now applies to both
    doors, where before ``/dream`` entered the commit path whenever git was initialized and cron
    skipped it for a no-op. Leaving those two tests pointed at a deleted helper would have been the
    same shape as #123 -- a test that names something nothing calls.
    """

    @pytest.mark.asyncio
    async def test_a_run_that_changed_nothing_makes_no_commit(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        store = _store(tmp_path)
        store.git.init()
        store.git.auto_commit("initial")
        store.git.auto_commit = MagicMock(wraps=store.git.auto_commit)

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            return _response()

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))

        assert outcome.commit_sha is None
        store.git.auto_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_real_edit_is_committed_under_the_caller_s_prefix(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        store = _store(tmp_path)
        store.git.init()
        store.git.auto_commit("initial")

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            store.write_memory("# Memory\n- Research notes")
            return _response()

        store.git.auto_commit = MagicMock(wraps=store.git.auto_commit)
        outcome = await run_dream(
            store=store,
            agent=_agent(store, tmp_path, process_direct),
            commit_prefix="dream: periodic memory consolidation",
        )

        assert outcome.commit_sha is not None
        store.git.auto_commit.assert_called_once()
        message = store.git.auto_commit.call_args.args[0]
        assert message.startswith("dream: periodic memory consolidation\n\n")

    @pytest.mark.asyncio
    async def test_a_failing_cleanup_step_does_not_hide_the_outcome(self, tmp_path) -> None:
        """This used to be a ``finally`` block, so one raising step skipped the rest (#121).

        In ``/dream`` it also left "Dreaming..." unresolved, because the raise escaped the task.
        """
        store = _store(tmp_path)

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            return _response()

        agent = _agent(store, tmp_path, process_direct)
        agent.sessions.sessions_dir = tmp_path / "does-not-exist"

        outcome = await run_dream(store=store, agent=agent)

        assert outcome.started is True
        assert outcome.completed is True
        assert store.get_last_dream_cursor() == 5
