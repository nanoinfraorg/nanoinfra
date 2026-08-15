# tests/webui/test_file_preview_containment.py
"""The WebUI file preview must stay inside the workspace, whatever the agent's file policy says.

`restrict_to_workspace` governs the agent's own file tools. The preview route is a different
capability: it decides what an authenticated WebUI client may read off the host. Reading one
setting to answer both questions meant that turning off a tool restriction also granted a remote
read of any file the process user could open, which includes `~/.nanoinfra/config.json` with the
provider API keys.

So containment here is unconditional. An operator who needs a wider preview adds a read root
deliberately, which is the capability-specific mechanism `.agent/security.md` already requires.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.security.workspace_access import (
    WorkspaceSandboxStatus,
    WorkspaceScope,
)
from nanoinfra.webui.file_preview import WebUIFilePreviewError, file_preview_payload


def _scope(workspace: Path, *, restrict: bool) -> WorkspaceScope:
    return WorkspaceScope(
        project_path=workspace,
        access_mode="full",
        restrict_to_workspace=restrict,
        sandbox_status=WorkspaceSandboxStatus(
            restrict_to_workspace=restrict,
            workspace_root=str(workspace),
            level="none",
            enforced=False,
            provider="none",
            provider_label="none",
            summary="test scope",
        ),
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "notes.md").write_text("inside the workspace\n", encoding="utf-8")
    return root


@pytest.mark.parametrize("restrict", [True, False])
def test_a_file_inside_the_workspace_still_previews(workspace: Path, restrict: bool) -> None:
    payload = file_preview_payload("notes.md", scope=_scope(workspace, restrict=restrict))

    assert "inside the workspace" in payload["content"]


@pytest.mark.parametrize("restrict", [True, False])
def test_a_file_outside_the_workspace_is_refused(
    workspace: Path, tmp_path: Path, restrict: bool
) -> None:
    """The setting must not change the answer. It governs the agent, not this route."""
    secret_file = tmp_path / "config.json"
    secret_file.write_text('{"apiKey": "sk-should-never-be-read"}\n', encoding="utf-8")

    with pytest.raises(WebUIFilePreviewError) as excinfo:
        file_preview_payload(str(secret_file), scope=_scope(workspace, restrict=restrict))

    assert excinfo.value.status in (400, 403, 404)


def test_a_traversal_escape_is_refused(workspace: Path, tmp_path: Path) -> None:
    """A relative path must not walk out of the workspace either."""
    (tmp_path / "outside.txt").write_text("out\n", encoding="utf-8")

    with pytest.raises(WebUIFilePreviewError):
        file_preview_payload("../outside.txt", scope=_scope(workspace, restrict=False))


def test_the_data_directory_is_refused_even_when_the_agent_is_unrestricted(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The concrete leak this closes: provider API keys live in the data directory."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = data_dir / "config.json"
    config.write_text('{"providers": {"anthropic": {"apiKey": "sk-live"}}}\n', encoding="utf-8")
    monkeypatch.setenv("NANOINFRA_DATA_DIR", str(data_dir))

    with pytest.raises(WebUIFilePreviewError):
        file_preview_payload(str(config), scope=_scope(workspace, restrict=False))
