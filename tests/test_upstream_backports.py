"""Four fixes ported from HKUDS/nanobot, each verified against our own source first.

The value of a backport is not that upstream made it; it is that the hole was here too. Each
test below names the upstream commit and asserts the property in *our* tree, so a later
refactor that reopens one of these fails here rather than in a channel nobody was watching.

The triage that produced them is `proposals/upstream-sync-triage.md`.
"""

from __future__ import annotations

import asyncio
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
