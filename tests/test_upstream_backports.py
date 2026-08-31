"""Four fixes ported from HKUDS/nanobot, each verified against our own source first.

The value of a backport is not that upstream made it; it is that the hole was here too. Each
test below names the upstream commit and asserts the property in *our* tree, so a later
refactor that reopens one of these fails here rather than in a channel nobody was watching.

The triage that produced them is `proposals/upstream-sync-triage.md`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from nanoinfra.channels.slack.runtime import _validate_slack_download_request

# --- a4acd839: a Slack file URL is data from a workspace, not a trusted address -----------


async def test_a_slack_file_url_pointing_inside_is_refused() -> None:
    """The download used to follow any URL Slack handed it, redirects included."""
    for target in (
        "http://127.0.0.1:8765/secret",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/internal",
    ):
        with pytest.raises(httpx.RequestError, match="unsafe Slack file URL"):
            await _validate_slack_download_request(httpx.Request("GET", target))


async def test_a_public_slack_file_url_passes() -> None:
    await _validate_slack_download_request(
        httpx.Request("GET", "https://files.slack.com/files-pri/T1-F1/report.pdf")
    )


def test_the_slack_download_validates_every_request_including_redirects() -> None:
    """A one-time check on the first URL misses a 302 into the network.

    Asserted on the source because the property is which client the download builds, and
    building one in a test would either reach the network or prove nothing.
    """
    source = Path("nanoinfra/channels/slack/runtime.py").read_text(encoding="utf-8")
    download = source[source.index("async def _download_slack_file") :]
    client = download[download.index("httpx.AsyncClient(") : download.index(") as client:")]

    assert "PinnedDNSAsyncTransport()" in client
    assert "_validate_slack_download_request" in client
    assert "httpx_env_proxy_mounts()" in client


# --- f5e46762: the no-tools request path had no timeout ----------------------------------


class _HangingProvider:
    """A provider that never answers, which is what a network stall looks like."""

    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **_kwargs: Any) -> Any:
        self.calls += 1
        await asyncio.sleep(3600)
        raise AssertionError("the sleep should have been cancelled")


async def test_a_stalled_no_tools_request_gives_up_instead_of_holding_the_lock() -> None:
    """A hang here looks like a session that simply stopped answering."""
    from nanoinfra.agent.runner import AgentRunner

    runner = AgentRunner()
    provider = _HangingProvider()
    spec = type(
        "_Spec",
        (),
        {
            "llm_timeout_s": 0.05,
            "runtime": type("_Runtime", (), {"provider": provider})(),
            "tools": None,
        },
    )()
    # The request kwargs are not what is under test, and building them needs a whole runtime.
    # Stubbing the builder keeps this test about the one property: a stalled call gives up.
    runner._build_request_kwargs = lambda *_a, **_k: {"messages": []}  # pyright: ignore[reportAttributeAccessIssue]

    async def _request() -> Any:
        return await runner._request_no_tools(  # pyright: ignore[reportPrivateUsage]
            spec, [{"role": "user", "content": "hello"}], provider_context=None
        )

    response = await asyncio.wait_for(_request(), timeout=5)
    assert provider.calls == 1
    assert response.finish_reason == "error"
    # The message names the knob, because the operator reading it is the one who sets it.
    assert "NANOINFRA_LLM_TIMEOUT_S" in response.content


def test_both_request_paths_resolve_the_same_timeout() -> None:
    """One resolver, so the two paths cannot drift apart again."""
    import inspect

    from nanoinfra.agent.runner import AgentRunner

    source = inspect.getsource(AgentRunner)
    assert source.count("_resolve_llm_timeout_s(spec)") >= 2
    assert 'os.environ.get("NANOINFRA_LLM_TIMEOUT_S"' in source


# --- ff674144: a truncated summary is not a summary --------------------------------------


def test_a_length_stopped_consolidation_is_treated_as_a_failure() -> None:
    """`length` means the model ran out of room mid-summary.

    Accepting it replaced a conversation's history with a half-written summary, and whatever
    the model had not reached was simply gone.
    """
    source = Path("nanoinfra/agent/memory.py").read_text(encoding="utf-8")

    assert 'finish_reason in {"error", "length"}' in source
    assert 'finish_reason == "error"' not in source


# --- cc07ac1e: do not archive what the prompt already carries ----------------------------


def test_the_archive_prompt_says_not_to_repeat_recent_history() -> None:
    template = Path("nanoinfra/templates/agent/consolidator_archive.md").read_text(
        encoding="utf-8"
    )

    assert "already present in the system prompt's Recent History" in template


# --- 82e50e2c: the session summary appeared twice in one prompt --------------------------


def test_the_summary_is_dropped_from_recent_history() -> None:
    """A compaction archives the summary and the same text reaches recent history.

    So a long session paid for it on every turn and the model read two copies with two
    different framings.
    """
    from nanoinfra.agent.context import ContextBuilder

    summary = "The user prefers direct commits and no PRs."
    entries = [
        {"timestamp": "t1", "content": f"Previous conversation summary (last active x):\n{summary}"},
        {"timestamp": "t2", "content": summary},
        {"timestamp": "t3", "content": "something that is not the summary"},
    ]

    kept = ContextBuilder._without_duplicate_session_summary(entries, summary)  # pyright: ignore[reportPrivateUsage]

    assert [entry["content"] for entry in kept] == ["something that is not the summary"]


def test_history_survives_when_there_is_no_summary() -> None:
    from nanoinfra.agent.context import ContextBuilder

    entries = [{"timestamp": "t1", "content": "a message"}]

    assert ContextBuilder._without_duplicate_session_summary(entries, "") == entries  # pyright: ignore[reportPrivateUsage]


# --- 679a0746 + our own half: an automation freezes no identity ---------------------------


def test_a_persisted_automation_carries_no_identity_or_workspace() -> None:
    """A cron job kept the workspace one person's turn resolved, by absolute path.

    Nothing read it back -- `webui/workspaces.py` resolves a later run's workspace from the
    session file -- so it was a copy nobody used, written into `jobs.json` and echoed by the
    automations payload. One person's identity directory therefore reached a store the agent
    reads and a page another person can open.
    """
    from nanoinfra.runtime_context import persistable_metadata

    persisted = persistable_metadata(
        {
            "webui": True,
            "sender": "webui:ops@example.test",
            "workspace_scope": {"workspace": "/home/x/.nanoinfra/workspaces/u-9f2/default"},
            "identity_dir": "u-9f2",
        }
    )

    # Routing survives, because a later delivery needs it.
    assert persisted["webui"] is True
    assert persisted["sender"] == "webui:ops@example.test"
    # Who asked does not, because an automation has no asker.
    assert "workspace_scope" not in persisted
    assert "identity_dir" not in persisted
    assert "u-9f2" not in json.dumps(persisted)


# --- 649e3958: an unbounded scan burned the turn ------------------------------------------


async def test_find_files_stops_at_its_entry_budget_and_says_so(tmp_path: Path) -> None:
    """`find_files` walked until it finished, however large the tree.

    A workspace with a `node_modules` or a mounted dataset spent the turn in `os.walk`.
    """
    from nanoinfra.agent.tools import search as search_tools

    for index in range(40):
        (tmp_path / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")

    tool = search_tools.FindFilesTool(str(tmp_path))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(search_tools, "SCAN_ENTRY_BUDGET", 5)
        answer = await tool.execute(path=".")

    assert search_tools.SCAN_TRUNCATED_NOTE in str(answer)
    # A partial list is useful; one that does not say it is partial is a wrong answer.
    assert "file-000.txt" in str(answer)


async def test_find_files_answers_normally_within_the_budget(tmp_path: Path) -> None:
    from nanoinfra.agent.tools import search as search_tools

    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    (tmp_path / "two.txt").write_text("x", encoding="utf-8")

    answer = await search_tools.FindFilesTool(str(tmp_path)).execute(path=".")

    assert "one.txt" in str(answer)
    assert "two.txt" in str(answer)
    assert search_tools.SCAN_TRUNCATED_NOTE not in str(answer)


def test_the_scan_runs_off_the_event_loop() -> None:
    """The walk is synchronous, so holding the loop stalls every other session."""
    import inspect

    from nanoinfra.agent.tools.search import FindFilesTool

    source = inspect.getsource(FindFilesTool.execute)
    assert "asyncio.to_thread" in source
