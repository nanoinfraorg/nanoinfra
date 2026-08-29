"""Whose conversation is this.

A per-user workspace separates files. Sessions are the other half: a person who
signs in should find their own chats, and should not be able to read anybody
else's by asking for a key.

The three cases in `session_belongs_to` are the whole design, and the third is the
one that is easy to get wrong: a session written with no identity is **not** shown
to a caller who has one. It predates them or it belongs to the shared posture, and
handing it over would give somebody a conversation they never had.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.security.workspace_access import default_workspace_scope
from nanoinfra.webui.identity_workspaces import (
    SESSION_IDENTITY_METADATA_KEY,
    identity_dirname,
    identity_workspace_key,
)
from nanoinfra.webui.workspaces import WebUIWorkspaceController

GOOGLE = "https://accounts.google.com"
ALICE = identity_workspace_key(GOOGLE, "1111")
BOB = identity_workspace_key(GOOGLE, "2222")


class _Session:
    def __init__(self, key: str) -> None:
        self.key = key
        self.metadata: dict[str, Any] = {}


class _Sessions:
    """Enough of SessionManager to answer the two calls this seam makes."""

    def __init__(self) -> None:
        self.saved: dict[str, _Session] = {}

    def get_or_create(self, key: str) -> _Session:
        return self.saved.setdefault(key, _Session(key))

    def save(self, session: _Session) -> None:
        self.saved[session.key] = session

    def read_session_metadata(self, key: str) -> dict[str, Any] | None:
        session = self.saved.get(key)
        return None if session is None else {"metadata": dict(session.metadata)}


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebUIWorkspaceController:
    root = tmp_path / "workspaces"
    (root / "default").mkdir(parents=True)
    monkeypatch.setattr("nanoinfra.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    class _Tools:
        workspaces_root = str(root)

    class _Config:
        tools = _Tools()

    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *a, **k: _Config())
    return WebUIWorkspaceController(
        session_manager=_Sessions(),  # type: ignore[arg-type]
        default_workspace=root / "default",
        default_restrict_to_workspace=True,
    )


def _persist(controller: WebUIWorkspaceController, chat_id: str, identity_key: str) -> None:
    scope = default_workspace_scope(Path("/tmp/x"), True, source_channel="websocket")
    controller.persist_scope(chat_id, scope, identity_key)


class TestWhatASessionRecords:
    def test_it_records_the_directory_and_not_the_subject(
        self, controller: WebUIWorkspaceController
    ) -> None:
        """A subject claim written into a session file is a subject claim in whatever
        a route later hands a client."""
        _persist(controller, "chat-1", ALICE)
        sessions: Any = controller._sessions  # noqa: SLF001 - the fake above
        stored = sessions.saved["websocket:chat-1"].metadata[SESSION_IDENTITY_METADATA_KEY]
        assert stored == identity_dirname(ALICE)
        assert "1111" not in stored

    def test_a_shared_deployment_records_nothing(
        self, controller: WebUIWorkspaceController
    ) -> None:
        _persist(controller, "chat-1", "")
        sessions: Any = controller._sessions  # noqa: SLF001
        assert SESSION_IDENTITY_METADATA_KEY not in sessions.saved["websocket:chat-1"].metadata


class TestWhoMaySeeIt:
    def test_its_own_owner_may(self, controller: WebUIWorkspaceController) -> None:
        _persist(controller, "chat-1", ALICE)
        assert controller.session_belongs_to("websocket:chat-1", ALICE)

    def test_another_identity_may_not(self, controller: WebUIWorkspaceController) -> None:
        _persist(controller, "chat-1", ALICE)
        assert not controller.session_belongs_to("websocket:chat-1", BOB)

    def test_a_caller_with_no_identity_may_not_see_a_personal_one(
        self, controller: WebUIWorkspaceController
    ) -> None:
        """Otherwise a shared token would be the way around every identity on the
        deployment."""
        _persist(controller, "chat-1", ALICE)
        assert not controller.session_belongs_to("websocket:chat-1", "")

    def test_a_caller_with_an_identity_may_not_see_a_shared_one(
        self, controller: WebUIWorkspaceController
    ) -> None:
        """The case that is easy to get wrong. It predates them, or it belongs to the
        shared posture; either way it is not theirs."""
        _persist(controller, "chat-1", "")
        assert not controller.session_belongs_to("websocket:chat-1", ALICE)

    def test_a_shared_deployment_sees_its_own_sessions(
        self, controller: WebUIWorkspaceController
    ) -> None:
        """The regression that would empty the sidebar of every install with no proxy."""
        _persist(controller, "chat-1", "")
        assert controller.session_belongs_to("websocket:chat-1", "")

    def test_a_session_that_does_not_exist_is_nobody_elses(
        self, controller: WebUIWorkspaceController
    ) -> None:
        """A missing session reads as unowned, so a first message can create it."""
        assert controller.session_belongs_to("websocket:never-written", "")
        assert not controller.session_belongs_to("websocket:never-written", ALICE)


def test_the_index_never_sends_the_owner_to_a_client() -> None:
    """The sidebar is told which sessions it may see. It is not told that others exist,
    and it is certainly not told whose they are."""
    from nanoinfra.webui.session_list_index import (
        WEBUI_SESSION_INDEX_INTERNAL_FIELDS,
        indexed_identity,
    )

    assert "_identity_dir" in WEBUI_SESSION_INDEX_INTERNAL_FIELDS
    assert indexed_identity({"_identity_dir": "u-abcdefghij"}) == "u-abcdefghij"
    assert indexed_identity({}) == ""


def test_every_session_route_is_guarded_by_one_check() -> None:
    """Scan, do not list. Five routes read a session by key, and a sixth added later
    would otherwise be the one that answers 200 for somebody else's transcript."""
    import ast

    source = (
        Path(__file__).resolve().parents[2] / "nanoinfra" / "webui" / "ws_http.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    dispatcher = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_dispatch_session_routes"
    )
    body = ast.get_source_segment(source, dispatcher) or ""
    # The guard runs before any route in this function, so it is the first `if`.
    assert "_session_is_visible" in body
    guard_at = body.index("_session_is_visible")
    first_route = body.index("/api/sessions/([^/]+)/messages")
    assert guard_at < first_route, "the ownership check must run before the first route"
