"""The boundary a verified identity puts around a workspace.

A refusal here is the product, not an error path: a client that names another
person's directory gets `workspace_scope_rejected`, not a corrected answer, so a
misconfigured deployment is visible instead of quietly sharing files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nanoinfra.security.workspace_access import (
    WORKSPACE_SCOPE_METADATA_KEY,
    WorkspaceScopeError,
    default_workspace_scope,
)
from nanoinfra.webui.identity_workspaces import (
    IDENTITY_DEFAULT_WORKSPACE,
    identity_workspace_key,
    identity_workspace_path,
)
from nanoinfra.webui.workspaces import WebUIWorkspaceController

GOOGLE = "https://accounts.google.com"
ALICE = identity_workspace_key(GOOGLE, "1111")
BOB = identity_workspace_key(GOOGLE, "2222")


@pytest.fixture
def controller(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebUIWorkspaceController:
    root = tmp_path / "workspaces"
    root.mkdir()
    shared = root / "default"
    shared.mkdir()
    monkeypatch.setattr("nanoinfra.webui.workspaces.get_webui_dir", lambda: tmp_path / "webui")

    class _Tools:
        workspaces_root = str(root)

    class _Config:
        tools = _Tools()

    monkeypatch.setattr("nanoinfra.config.loader.load_config", lambda *a, **k: _Config())
    return WebUIWorkspaceController(
        session_manager=None,
        default_workspace=shared,
        default_restrict_to_workspace=True,
    )


def _envelope(path: Path) -> dict[str, Any]:
    scope = default_workspace_scope(path, True, source_channel="websocket")
    return {WORKSPACE_SCOPE_METADATA_KEY: scope.payload()}


class TestWhereAPersonStarts:
    def test_a_verified_identity_starts_in_its_own_directory(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        """Under their own root, not at it: a switcher lists what is under a root."""
        scope = controller.identity_default_scope(ALICE, name="alice@example.com")
        own_root = identity_workspace_path(tmp_path / "workspaces", ALICE)
        assert Path(scope.project_path) == own_root / IDENTITY_DEFAULT_WORKSPACE

    def test_the_directory_is_created_on_first_use(
        self, controller: WebUIWorkspaceController
    ) -> None:
        scope = controller.identity_default_scope(ALICE, name="alice@example.com")
        assert Path(scope.project_path).is_dir()

    def test_no_key_is_the_shared_posture(self, controller: WebUIWorkspaceController) -> None:
        """A deployment with no trusted proxy behaves exactly as it did before."""
        assert controller.identity_default_scope("") == controller.default_scope()
        assert controller.identity_root("") is None

    def test_two_identities_start_in_two_directories(
        self, controller: WebUIWorkspaceController
    ) -> None:
        a = controller.identity_default_scope(ALICE, name="alice@example.com")
        b = controller.identity_default_scope(BOB, name="bob@example.com")
        assert a.project_path != b.project_path


class TestTheBoundary:
    def test_its_own_directory_is_inside(self, controller: WebUIWorkspaceController) -> None:
        scope = controller.identity_default_scope(ALICE, name="alice@example.com")
        assert controller.within_identity(scope, ALICE)

    def test_a_workspace_under_it_is_inside(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        own = identity_workspace_path(tmp_path / "workspaces", ALICE)
        own.mkdir(parents=True, exist_ok=True)
        child = own / "a-project"
        child.mkdir()
        assert controller.within_identity(
            default_workspace_scope(child, True, source_channel="websocket"), ALICE
        )

    def test_another_identity_is_outside(self, controller: WebUIWorkspaceController) -> None:
        theirs = controller.identity_default_scope(BOB, name="bob@example.com")
        assert not controller.within_identity(theirs, ALICE)

    def test_the_shared_default_is_outside(
        self, controller: WebUIWorkspaceController
    ) -> None:
        assert not controller.within_identity(controller.default_scope(), ALICE)

    def test_with_no_key_everything_is_inside(self, controller: WebUIWorkspaceController) -> None:
        assert controller.within_identity(controller.default_scope(), "")


class TestWhatAnEnvelopeMayAsk:
    def test_its_own_workspace_is_accepted(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        own = identity_workspace_path(tmp_path / "workspaces", ALICE)
        own.mkdir(parents=True, exist_ok=True)
        scope = controller.scope_from_envelope(
            _envelope(own), session_key=None, controls_available=True, identity_key=ALICE
        )
        assert Path(scope.project_path) == own

    def test_another_identity_is_refused_rather_than_corrected(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        theirs = identity_workspace_path(tmp_path / "workspaces", BOB)
        theirs.mkdir(parents=True, exist_ok=True)
        with pytest.raises(WorkspaceScopeError) as refusal:
            controller.scope_from_envelope(
                _envelope(theirs), session_key=None, controls_available=True, identity_key=ALICE
            )
        assert refusal.value.status == 403

    def test_the_shared_default_is_refused_for_a_verified_identity(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        with pytest.raises(WorkspaceScopeError):
            controller.scope_from_envelope(
                _envelope(tmp_path / "workspaces" / "default"),
                session_key=None,
                controls_available=True,
                identity_key=ALICE,
            )

    def test_an_envelope_naming_nothing_lands_in_its_own_directory(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        scope = controller.scope_from_envelope(
            {}, session_key=None, controls_available=True, identity_key=ALICE
        )
        assert (
            Path(scope.project_path)
            == identity_workspace_path(tmp_path / "workspaces", ALICE) / IDENTITY_DEFAULT_WORKSPACE
        )

    def test_without_a_key_the_shared_workspace_is_still_allowed(
        self, controller: WebUIWorkspaceController, tmp_path: Path
    ) -> None:
        """The regression that would break every deployment that has no proxy."""
        scope = controller.scope_from_envelope(
            _envelope(tmp_path / "workspaces" / "default"),
            session_key=None,
            controls_available=True,
            identity_key="",
        )
        assert Path(scope.project_path) == tmp_path / "workspaces" / "default"


def test_a_carrier_that_answers_every_attribute_is_not_an_identity() -> None:
    """The fail-open case, and the reason both readers demand a string.

    `getattr(carrier, ATTR, "")` looks safe and is not: a mock, a proxy object or
    anything else that fabricates attributes on demand answers with a truthy value,
    and the caller would then hand that object a workspace of its own. Only what
    the handshake wrote -- a string -- counts.
    """
    from unittest.mock import MagicMock

    from nanoinfra.channels.websocket.runtime import _connection_workspace_key

    assert _connection_workspace_key(MagicMock()) == ""
    assert _connection_workspace_key(object()) == ""

    written = MagicMock()
    written._nanoinfra_trusted_proxy_workspace_key = ALICE
    assert _connection_workspace_key(written) == ALICE


def test_the_refusal_does_not_send_an_operator_after_the_wrong_setting() -> None:
    """A person who named someone else's directory has a client problem, not a
    config problem. The shared-root text tells the reader to widen
    tools.workspacesRoot -- advice that, followed here, puts everyone back into one
    directory."""
    from nanoinfra.webui.file_browser import WebUIFileBrowserError
    from nanoinfra.webui.workspace_roots import resolve_client_workspace

    with pytest.raises(WebUIFileBrowserError) as shared:
        resolve_client_workspace(
            "/somewhere/else", root=Path("/roots"), default_workspace=Path("/roots/default")
        )
    assert "tools.workspacesRoot" in shared.value.message

    with pytest.raises(WebUIFileBrowserError) as personal:
        resolve_client_workspace(
            "/roots/u-other/default",
            root=Path("/roots/u-mine"),
            default_workspace=Path("/roots/u-mine/default"),
            root_is_personal=True,
        )
    assert personal.value.message == "that workspace is not yours"
    assert "workspacesRoot" not in personal.value.message
