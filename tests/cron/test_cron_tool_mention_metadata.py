"""A job created from a message that carried a resource mention.

Reproduces the failure seen in production: the WebUI attaches the turn's runtime context blocks to
the inbound metadata, the cron tool copied that metadata verbatim into the job, and `jobs.json`
became unwritable -- which then failed every following tick, not just the creating call.
"""

import json

import pytest

from nanoinfra.agent.tools.context import RequestContext, request_context
from nanoinfra.agent.tools.cron import CronTool
from nanoinfra.cron.service import CronService
from nanoinfra.runtime_context import RUNTIME_CONTEXT_INPUT_META, RuntimeContextBlock


def _tool(tmp_path) -> CronTool:
    service = CronService(tmp_path / "cron" / "jobs.json")
    # The store is only written while the service owns it, which is the state a live gateway is in.
    service._running = True
    return CronTool(service)


def _mention_metadata() -> dict[str, object]:
    return {
        "webui": True,
        RUNTIME_CONTEXT_INPUT_META: [
            RuntimeContextBlock(source="resource_mentions", content="[Runtime Context]")
        ],
        "resource_mentions": [
            {"kind": "server", "id": "c42f7a86a78f4534ae9a4664c79379f6", "name": "barrahome"}
        ],
    }


async def test_a_mentioned_server_becomes_a_job_reference(tmp_path) -> None:
    tool = _tool(tmp_path)
    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            session_key="websocket:chat-1",
            metadata=_mention_metadata(),
        )
    ):
        result = tool._add_job(None, "Report the uptime", 300, None, None, None)

    assert result.startswith("Created job")
    job = tool._cron.list_jobs()[0]
    assert job.references == [{"kind": "server", "id": "c42f7a86a78f4534ae9a4664c79379f6"}]


async def test_the_job_stays_writable_when_the_turn_carried_context_blocks(tmp_path) -> None:
    tool = _tool(tmp_path)
    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            session_key="websocket:chat-1",
            metadata=_mention_metadata(),
        )
    ):
        tool._add_job(None, "Report the uptime", 300, None, None, None)

    job = tool._cron.list_jobs()[0]
    # The block was the crash; the resolved payload is stale the moment the resource is renamed.
    assert RUNTIME_CONTEXT_INPUT_META not in job.payload.origin_metadata
    assert "resource_mentions" not in job.payload.origin_metadata
    assert job.payload.origin_metadata == {"webui": True}

    stored = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert [j["name"] for j in stored["jobs"]] == [job.name]


async def test_an_unwritable_job_is_not_left_in_the_store(tmp_path) -> None:
    """The phantom: two failed adds showed up in the UI and broke every later tick."""
    service = CronService(tmp_path / "cron" / "jobs.json")
    service._running = True
    tool = CronTool(service)

    class Unserializable:
        pass

    with request_context(
        RequestContext(
            channel="websocket",
            chat_id="chat-1",
            session_key="websocket:chat-1",
            metadata={"rogue": Unserializable()},
        )
    ):
        with pytest.raises(TypeError):
            tool._add_job(None, "Report the uptime", 300, None, None, None)

    assert service.list_jobs() == []
    # And the next save is not poisoned by the rejected job.
    with request_context(
        RequestContext(channel="websocket", chat_id="chat-1", session_key="websocket:chat-1")
    ):
        tool._add_job(None, "Second attempt", 300, None, None, None)
    stored = json.loads((tmp_path / "cron" / "jobs.json").read_text(encoding="utf-8"))
    assert [j["name"] for j in stored["jobs"]] == ["Second attempt"]
