"""An automation names the chat it is linked to.

Reported from the Automations panel: a job created from a chat titled "Weekly GitHub Issue Blocker
Summary" showed its linked chat as **`/model deepseek`** -- the first message of that conversation, and
a slash command that had failed.

Two causes, one on top of the other:

- The title of a WebUI chat lives at ``metadata["title"]``, and ``_websocket_origin_payload`` read
  ``data["title"]`` at the top level. That is always absent, so **every** automation linked to a WebUI
  chat resolved an empty title and fell through to the preview. The sidebar reads the same fact from
  the flattened session index and gets it right, so two readers answered one question differently.
- The preview then offered a slash command, because it skipped hidden records and not commands.

The panel's linked-chat column is where an operator looks to answer "which chat does this job post
to", so a job named after a failed command makes that column useless as soon as there are two of them.
"""

from __future__ import annotations

from typing import Any

from nanoinfra.cron.types import CronJob
from nanoinfra.webui.session_automations import serialize_automation_jobs

SESSION_KEY = "websocket:25a489e3-012a-4865-9514-263e6f552897"


class _SessionManager:
    """Returns a session file shaped the way the store writes one."""

    def __init__(self, *, title: str | None, messages: list[dict[str, Any]]) -> None:
        self._title = title
        self._messages = messages

    def read_session_file(self, key: str) -> dict[str, Any] | None:
        if key != SESSION_KEY:
            return None
        metadata: dict[str, Any] = {"_nanoinfra_model_preset": "fast-writing"}
        if self._title is not None:
            # Where a WebUI title actually lives. Never at the top level.
            metadata["title"] = self._title
        return {
            "key": SESSION_KEY,
            "created_at": "2026-08-16T01:31:17",
            "updated_at": "2026-08-17T00:12:15",
            "metadata": metadata,
            "messages": self._messages,
        }


def _job() -> CronJob:
    # `from_store_dict` is what reads jobs.json, so this is the camelCase shape on disk.
    return CronJob.from_store_dict({
        "id": "4aa2442d",
        "name": "nanoinfra-blockers-monday",
        "enabled": True,
        "schedule": {"kind": "cron", "expr": "0 9 * * 1", "tz": "America/Mexico_City"},
        "payload": {
            "kind": "agent_turn",
            "message": "Check open issues and report new blockers.",
            "deliver": False,
            "sessionKey": SESSION_KEY,
            "originChannel": "websocket",
            "originChatId": "25a489e3-012a-4865-9514-263e6f552897",
        },
    })


def _origin(*, title: str | None, messages: list[dict[str, Any]]) -> dict[str, Any]:
    rows = serialize_automation_jobs(
        [_job()],
        include_details=True,
        session_manager=_SessionManager(title=title, messages=messages),
    )
    origin = rows[0]["origin"]
    assert isinstance(origin, dict)
    return origin


def test_the_linked_chat_carries_the_chat_title() -> None:
    origin = _origin(
        title="Weekly GitHub Issue Blocker Summary",
        messages=[{"role": "user", "content": "/model deepseek", "_command": True}],
    )

    assert origin["title"] == "Weekly GitHub Issue Blocker Summary", (
        "the panel showed the first message instead, for every automation linked to a WebUI chat"
    )


def test_a_slash_command_is_not_a_preview() -> None:
    """A chat with no title yet must not be named after a command.

    The record carries ``_command``, so this needs no guessing about what the text looks like.
    """
    origin = _origin(
        title=None,
        messages=[
            {"role": "user", "content": "/model deepseek", "_command": True},
            {"role": "user", "content": "Every monday at 9am, check open issues"},
        ],
    )

    assert origin["title"] == ""
    assert not origin["preview"].startswith("/model")
    assert "Every monday" in origin["preview"]


def test_a_titled_chat_keeps_its_preview_too() -> None:
    """The preview is still carried, because the panel uses it as a tooltip and a fallback."""
    origin = _origin(
        title="Weekly GitHub Issue Blocker Summary",
        messages=[{"role": "user", "content": "Every monday at 9am, check open issues"}],
    )

    assert origin["title"] == "Weekly GitHub Issue Blocker Summary"
    assert "Every monday" in origin["preview"]


def test_a_chat_with_only_commands_falls_back_to_the_session_key() -> None:
    """Better an id than a wrong name: the label's last fallback is the session key."""
    origin = _origin(
        title=None,
        messages=[{"role": "user", "content": "/model deepseek", "_command": True}],
    )

    assert origin["title"] == ""
    assert origin["preview"] == ""
    assert origin["session_key"] == SESSION_KEY
