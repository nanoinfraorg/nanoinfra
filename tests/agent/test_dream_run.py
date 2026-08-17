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


class TestTheSessionKeyCannotCollide:
    """One-second resolution meant two runs shared a file -- nanoinfraorg/nanoinfra#122.

    Two runs started in the same second held the same key, so they shared a session file *and* the
    per-session dispatch lock -- serializing by accident rather than by design. The design is the
    lock in this module, and the key's job is identity. A wall clock also steps backwards under NTP,
    so more digits of it would not have been an answer.
    """

    def test_two_keys_made_in_the_same_second_differ(self) -> None:
        keys = {MemoryStore.dream_session_key() for _ in range(200)}

        assert len(keys) == 200

    def test_the_key_still_reads_as_a_dream_session_at_a_time(self) -> None:
        key = MemoryStore.dream_session_key()

        assert key.startswith("dream:")
        # The timestamp stays human-readable, because an operator reads these in a directory listing.
        assert __import__("re").match(r"^dream:\d{8}-\d{6}-[0-9a-f]+$", key), key

    def test_prune_still_recognises_the_new_shape(self, tmp_path) -> None:
        from nanoinfra.session.manager import SessionManager

        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        paths = []
        for index in range(12):
            key = MemoryStore.dream_session_key()
            path = sessions_dir / f"{SessionManager._storage_key(key)}.jsonl"
            path.write_text('{"_type": "metadata"}\n', encoding="utf-8")
            import os
            import time
            os.utime(path, (time.time() - 100 + index, time.time() - 100 + index))
            paths.append(path)

        MemoryStore.prune_dream_sessions(sessions_dir, keep=10)

        assert sum(1 for path in paths if path.exists()) == 10


class TestAttributionOfACommit:
    """A commit that says "dream" carries what Dream wrote -- nanoinfraorg/nanoinfra#111."""

    @pytest.mark.asyncio
    async def test_a_cursor_increment_alone_makes_no_dream_commit(self, tmp_path) -> None:
        """`/dream-log` shows the newest `dream:` commit, so a bookkeeping commit became the answer
        to "what did Dream change", while the real last memory change sat one commit back.
        """
        store = _store(tmp_path)
        store.git.init()

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            return _response()  # completes, writes nothing

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))

        assert outcome.completed is True
        assert store.get_last_dream_cursor() == 5, "the batch was still retired"
        assert outcome.commit_sha is None
        assert not [c for c in store.git.log() if c.message.startswith("dream:")]

    @pytest.mark.asyncio
    async def test_an_edit_made_before_the_run_is_not_attributed_to_dream(self, tmp_path) -> None:
        """A hand edit to SOUL.md, or a half-applied edit from a crashed run, used to be committed
        as "dream: manual run" with no body: attributed to Dream, listed in `/dream-restore`, and
        revertable as though Dream had written it.
        """
        store = _store(tmp_path)
        store.git.init()
        store.write_soul("# Soul\n- edited by a person")

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            store.write_memory("# Memory\n- written by dream")
            return _response()

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))

        assert outcome.commit_sha is not None
        messages = [c.message for c in store.git.log()]
        dream_commits = [m for m in messages if m.startswith("dream:")]
        assert len(dream_commits) == 1
        assert "SOUL.md" not in dream_commits[0], (
            "the person's edit must not appear in the body of a commit that names Dream"
        )
        # The operational test: `/dream-log` and `/dream-restore` both filter on the `dream:`
        # prefix, so a commit outside that prefix is not offered as a Dream change to undo.
        outside = [m for m in messages if not m.startswith("dream:") and "SOUL.md" in m]
        assert outside, "the person's edit is committed, outside the prefix Dream commits carry"
        assert store.git.log(message_prefix="dream:")[0].message == dream_commits[0]


class TestARepairedRunCounts:
    """A repaired error is not a failure -- nanoinfraorg/nanoinfra#113.

    The runner appends "[Analyze the error above and try a different approach.]" to a failed tool
    result, so failure then success is the designed path. Latching on any error meant a run that did
    exactly what it was asked reported "did not complete", and the next run re-derived the same facts
    from the same entries, forever, on every run that ever mistyped an edit.
    """

    @pytest.mark.asyncio
    async def test_an_error_a_later_call_repaired_still_completes(self, tmp_path) -> None:
        store = _store(tmp_path)

        async def process_direct(*_args: Any, **kwargs: Any) -> Any:
            progress = kwargs["on_progress"]
            await progress(tool_events=[{"phase": "error", "name": "edit_file"}])
            await progress(tool_events=[{"phase": "end", "name": "write_file"}])
            store.write_memory("# Memory\n- the fact that survived the retry")
            return _response()

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))

        assert outcome.completed is True
        assert store.get_last_dream_cursor() == 5

    @pytest.mark.asyncio
    async def test_a_run_whose_last_action_failed_does_not_advance(self, tmp_path) -> None:
        store = _store(tmp_path)

        async def process_direct(*_args: Any, **kwargs: Any) -> Any:
            progress = kwargs["on_progress"]
            await progress(tool_events=[{"phase": "end", "name": "read_file"}])
            await progress(tool_events=[{"phase": "error", "name": "write_file"}])
            return _response()

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))

        assert outcome.completed is False
        assert store.get_last_dream_cursor() == 0


class TestEverythingDreamCanWriteIsAudited:
    """The audit set covered three files out of everything -- nanoinfraorg/nanoinfra#112.

    ``dream.md`` *instructs* Dream to create ``skills/<name>/SKILL.md``, ``build_dream_tools`` grants
    ``write_file`` over that directory, and a skill whose frontmatter says ``always: true`` is loaded
    into **every** system prompt. None of it was in ``_DREAM_CONTENT_PATHS`` or in the tracked set, so
    a run whose only edit was a skill reported "no memory changes", committed nothing, kept no
    history, retired the batch, and then injected its new text into every later prompt.

    ``always: true`` makes a skill more durable than MEMORY.md, not less.
    """

    def test_every_place_the_registry_can_write_is_versioned(self, tmp_path) -> None:
        """The two sets are derived from one place, so they cannot drift apart again."""
        from nanoinfra.agent.memory import GIT_TRACKED_DIRS
        from nanoinfra.agent.tools.apply_patch import ApplyPatchTool
        from nanoinfra.agent.tools.filesystem import EditFileTool, WriteFileTool

        store = _store(tmp_path)
        registry = store.build_dream_tools()
        writers = [
            tool
            for tool in registry._tools.values()
            if isinstance(tool, (WriteFileTool, EditFileTool, ApplyPatchTool))
        ]
        assert writers, "the registry must grant some write, or this test proves nothing"

        writable_dirs = {
            Path(tool._allowed_dir).relative_to(store.workspace).as_posix()
            for tool in writers
            if tool._allowed_dir is not None
        }
        writable_files = {
            Path(path).relative_to(store.workspace).as_posix()
            for tool in writers
            for path in tool._extra_write_allowed_files
        }

        assert writable_dirs <= set(GIT_TRACKED_DIRS), (
            f"the registry can write {writable_dirs - set(GIT_TRACKED_DIRS)}, which git does not track"
        )
        assert writable_files <= set(store._DREAM_CONTENT_PATHS), (
            f"the registry can write {writable_files - set(store._DREAM_CONTENT_PATHS)}, "
            "which the audit record does not cover"
        )

    @pytest.mark.asyncio
    async def test_a_run_that_writes_only_a_skill_is_reported_and_committed(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.git.init()

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            skill = store.workspace / "skills" / "ops-check" / "SKILL.md"
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text(
                "---\nname: ops-check\ndescription: check ops\nalways: true\n---\n\nRun the checks.\n",
                encoding="utf-8",
            )
            return _response()

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))

        assert outcome.diff_body, "the operator was told nothing changed"
        assert "skills/ops-check/SKILL.md" in outcome.diff_body
        assert outcome.commit_sha is not None
        assert store.git.log(message_prefix="dream:"), "it must appear in /dream-restore"

    @pytest.mark.asyncio
    async def test_a_skill_dream_wrote_can_be_reverted(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.git.init()
        skill = store.workspace / "skills" / "ops-check" / "SKILL.md"

        async def process_direct(*_args: Any, **_kwargs: Any) -> Any:
            skill.parent.mkdir(parents=True, exist_ok=True)
            skill.write_text("---\nname: ops-check\nalways: true\n---\n\nbody\n", encoding="utf-8")
            return _response()

        outcome = await run_dream(store=store, agent=_agent(store, tmp_path, process_direct))
        assert outcome.commit_sha is not None

        reverted = store.git.revert(outcome.commit_sha.replace("revert", ""), message_prefix="dream:") \
            if False else store.git.revert(store.git.log(message_prefix="dream:")[0].sha)

        assert reverted is not None
        assert not skill.exists(), "undoing the commit that added the skill removes it again"


class TestTheTrackedSetHasOneDefinition:
    """Two lists, and they disagreed -- the leftover half of nanoinfraorg/nanoinfra#111."""

    def test_the_bootstrap_and_the_store_track_the_same_paths(self, tmp_path) -> None:
        from nanoinfra.agent.memory import GIT_TRACKED_DIRS, GIT_TRACKED_FILES

        store = _store(tmp_path)

        assert list(store.git._tracked_files) == list(GIT_TRACKED_FILES)
        assert list(store.git._tracked_dirs) == list(GIT_TRACKED_DIRS)


class TestDreamCannotReplaceWhatItDidNotSee:
    """A partial view plus a whole-file write -- nanoinfraorg/nanoinfra#108.

    ``_render_current_memory_files`` caps each embedded file at 8 KB, the section's docstring calls
    it "the ground truth the model must edit against", and the template tells the model "do not rely
    on a remembered version of a file". ``write_file`` was registered for those exact files with no
    read-before-write check.

    So Dream was shown the first 8 KB of a 16 KB MEMORY.md, was told that was the file, was told to
    prune, and issued one ``write_file``. 8 KB of durable facts went.
    """

    def test_the_prompt_says_when_a_file_is_shown_in_part(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.write_memory("# Memory\n" + "\n".join(f"- fact {i:04d}" for i in range(1200)))

        result = store.build_dream_prompt()

        assert result is not None
        prompt = result[0]
        assert "shown in part" in prompt or "not the whole file" in prompt, (
            "a model told a false premise acts on it"
        )

    def test_a_file_shown_in_part_gets_no_whole_file_write(self, tmp_path) -> None:
        from nanoinfra.agent.tools.filesystem import WriteFileTool

        store = _store(tmp_path)
        store.write_memory("# Memory\n" + "\n".join(f"- fact {i:04d}" for i in range(1200)))

        registry = store.build_dream_tools()
        writer = next(
            (t for t in registry._tools.values() if isinstance(t, WriteFileTool)),
            None,
        )

        assert writer is not None
        allowed = {Path(p).as_posix() for p in writer._extra_write_allowed_files}
        assert store.memory_file.as_posix() not in allowed, (
            "a file the model saw in part must be editable and not replaceable"
        )

    def test_a_file_that_fits_is_still_replaceable(self, tmp_path) -> None:
        from nanoinfra.agent.tools.filesystem import WriteFileTool

        store = _store(tmp_path)
        store.write_memory("# Memory\n- one small fact")

        registry = store.build_dream_tools()
        writer = next(t for t in registry._tools.values() if isinstance(t, WriteFileTool))

        allowed = {Path(p).as_posix() for p in writer._extra_write_allowed_files}
        assert store.memory_file.as_posix() in allowed

    def test_the_edit_tool_still_reaches_an_oversized_file(self, tmp_path) -> None:
        """Pruning has to stay possible: the model edits what it saw."""
        from nanoinfra.agent.tools.filesystem import EditFileTool

        store = _store(tmp_path)
        store.write_memory("# Memory\n" + "\n".join(f"- fact {i:04d}" for i in range(1200)))

        registry = store.build_dream_tools()
        editor = next(t for t in registry._tools.values() if isinstance(t, EditFileTool))

        allowed = {Path(p).as_posix() for p in editor._extra_write_allowed_files}
        assert store.memory_file.as_posix() in allowed
