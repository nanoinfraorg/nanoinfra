from pathlib import Path

import pytest

from nanoinfra.security.workspace_access import default_workspace_scope
from nanoinfra.webui.file_preview import WebUIFilePreviewError, file_preview_payload


def test_restricted_preview_allows_media_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    uploaded = media / "upload.txt"
    uploaded.write_text("uploaded", encoding="utf-8")
    monkeypatch.setattr("nanoinfra.webui.file_preview.get_media_dir", lambda: media)

    scope = default_workspace_scope(workspace, restrict_to_workspace=True)

    payload = file_preview_payload(str(uploaded), scope=scope)

    assert payload["content"] == "uploaded"
    assert Path(payload["path"]) == uploaded.resolve()


def test_restricted_preview_rejects_other_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr("nanoinfra.webui.file_preview.get_media_dir", lambda: media)

    scope = default_workspace_scope(workspace, restrict_to_workspace=True)

    with pytest.raises(WebUIFilePreviewError, match="outside the current workspace") as exc_info:
        file_preview_payload(str(outside), scope=scope)

    assert exc_info.value.status == 403


@pytest.mark.parametrize("name", ["topology.mmd", "topology.mermaid"])
def test_a_mermaid_file_reports_a_mermaid_language(tmp_path: Path, name: str) -> None:
    """A diagram file names its own language, so the WebUI can offer to render it.

    Without a mapping the extension falls through as its own name -- ``"mmd"`` -- which no
    highlighter and no renderer recognises, so the panel could only ever show it as anonymous
    text.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    diagram = workspace / name
    diagram.write_text("flowchart TB\n    a --> b\n", encoding="utf-8")

    scope = default_workspace_scope(workspace, restrict_to_workspace=True)
    payload = file_preview_payload(str(diagram), scope=scope)

    assert payload["language"] == "mermaid"


def test_known_languages_keep_their_mapping(tmp_path: Path) -> None:
    """The mermaid entries are an addition, not a rewrite of the table."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    for name, expected in (("a.py", "python"), ("b.md", "markdown"), ("c.svg", "svg")):
        (workspace / name).write_text("x", encoding="utf-8")
        scope = default_workspace_scope(workspace, restrict_to_workspace=True)
        payload = file_preview_payload(str(workspace / name), scope=scope)
        assert payload["language"] == expected
