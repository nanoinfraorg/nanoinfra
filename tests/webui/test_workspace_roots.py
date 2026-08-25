"""The workspaces root: what a client may name, and what config may."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoinfra.webui.file_browser import WebUIFileBrowserError
from nanoinfra.webui.workspace_roots import (
    create_workspace,
    resolve_client_workspace,
    workspaces_payload_for_root,
)


def test_the_root_is_created_on_demand_and_lists_its_children(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    (root / "notes.txt").write_text("not a workspace", encoding="utf-8")

    payload = workspaces_payload_for_root(root, root / "alpha")

    assert [w["name"] for w in payload["workspaces"]] == ["alpha", "beta"]
    assert payload["root"] == str(root)
    assert payload["workspaces"][0]["isDefault"] is True


def test_a_fresh_install_lists_an_empty_root_rather_than_an_error(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"

    payload = workspaces_payload_for_root(root, root / "default")

    assert root.is_dir()
    # The default does not exist yet either, and it lives under the root, so nothing
    # is invented for it.
    assert payload["workspaces"] == []


def test_the_configured_workspace_is_listed_even_outside_the_root(tmp_path: Path) -> None:
    """Config widens deliberately; this is where that shows up in the UI."""
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "legacy-workspace"
    outside.mkdir()

    payload = workspaces_payload_for_root(root, outside)

    assert payload["workspaces"][0]["path"] == str(outside)
    assert payload["workspaces"][0]["isDefault"] is True
    assert payload["workspaces"][0]["outsideRoot"] is True


def test_a_workspace_under_the_root_is_accepted(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / "alpha").mkdir(parents=True)

    resolved = resolve_client_workspace(
        str(root / "alpha"), root=root, default_workspace=root / "alpha"
    )

    assert resolved == (root / "alpha")


def test_nothing_named_means_the_configured_default(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    default = tmp_path / "legacy"
    default.mkdir()

    assert resolve_client_workspace(None, root=root, default_workspace=default) == default
    assert resolve_client_workspace("", root=root, default_workspace=default) == default


def test_a_path_outside_the_root_is_refused(tmp_path: Path) -> None:
    """The change this feature makes: typing a path is no longer how you get there."""
    root = tmp_path / "workspaces"
    root.mkdir()
    elsewhere = tmp_path / "Projects" / "thing"
    elsewhere.mkdir(parents=True)

    with pytest.raises(WebUIFileBrowserError, match="outside the workspaces root") as exc:
        resolve_client_workspace(str(elsewhere), root=root, default_workspace=root / "alpha")

    assert exc.value.status == 403


def test_the_configured_workspace_is_accepted_from_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    default = tmp_path / "legacy"
    default.mkdir()

    assert resolve_client_workspace(str(default), root=root, default_workspace=default) == default


def test_a_relative_escape_out_of_the_root_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    (tmp_path / "outside").mkdir()

    with pytest.raises(WebUIFileBrowserError) as exc:
        resolve_client_workspace(
            str(root / ".." / "outside"), root=root, default_workspace=root / "alpha"
        )

    assert exc.value.status == 403


def test_a_symlink_into_the_root_cannot_stand_for_somewhere_else(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "sneaky").symlink_to(outside, target_is_directory=True)

    with pytest.raises(WebUIFileBrowserError) as exc:
        resolve_client_workspace(str(root / "sneaky"), root=root, default_workspace=root / "alpha")

    assert exc.value.status == 403


def test_a_missing_workspace_under_the_root_is_a_404(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()

    with pytest.raises(WebUIFileBrowserError) as exc:
        resolve_client_workspace(str(root / "nope"), root=root, default_workspace=root / "alpha")

    assert exc.value.status == 404


def test_creating_a_workspace_makes_one_directory_under_the_root(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"

    payload = create_workspace(root, "new-thing")

    assert payload["workspace"] == str(root / "new-thing")
    assert (root / "new-thing").is_dir()


def test_creating_refuses_an_existing_name(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    (root / "alpha").mkdir(parents=True)

    with pytest.raises(WebUIFileBrowserError) as exc:
        create_workspace(root, "alpha")

    assert exc.value.status == 409


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a\\b"])
def test_a_workspace_name_is_one_component(tmp_path: Path, name: str) -> None:
    """It becomes a directory a client then browses, so it is validated like one."""
    root = tmp_path / "workspaces"

    with pytest.raises(WebUIFileBrowserError) as exc:
        create_workspace(root, name)

    assert exc.value.status == 400


def test_the_default_workspace_sits_under_the_default_root() -> None:
    """Otherwise a fresh install starts with a workspace its own picker disowns.

    Two defaults have to agree: `tools.workspacesRoot` and
    `agents.defaults.workspace`. A comment saying so is not enough -- the picker
    marks anything outside the root as "outside the root", and on a new install that
    label would be the first thing an operator saw.
    """
    from nanoinfra.config.paths import default_workspace_path, default_workspaces_root
    from nanoinfra.config.schema import Config

    config = Config()
    root = Path(config.tools.workspaces_root).expanduser()
    workspace = Path(config.agents.defaults.workspace).expanduser()

    assert root == default_workspaces_root()
    assert workspace == default_workspace_path()
    assert root in workspace.parents, f"{workspace} is not under {root}"


def test_the_pre_root_workspace_still_counts_as_a_default() -> None:
    """An install that never chose a workspace has not become deliberate by being older."""
    from nanoinfra.config.paths import is_default_workspace

    assert is_default_workspace(Path.home() / ".nanoinfra" / "workspace")


def test_a_new_workspace_is_seeded_like_one_the_cli_makes(tmp_path: Path) -> None:
    """A bare directory would be a workspace the agent reads no instructions from.

    The word has to mean the same thing whichever surface created it.
    """
    root = tmp_path / "workspaces"

    payload = create_workspace(root, "fresh")

    target = root / "fresh"
    assert (target / "AGENTS.md").is_file()
    assert (target / "HEARTBEAT.md").is_file()
    assert (target / "memory" / "MEMORY.md").is_file()
    assert (target / "prompts" / "README.md").is_file()
    assert (target / "skills").is_dir()
    assert "AGENTS.md" in payload["seeded"]


def test_a_new_workspace_claims_no_feature_it_is_not_using(tmp_path: Path) -> None:
    """Each store makes its own directory on first write; an empty one would read as in use."""
    root = tmp_path / "workspaces"

    create_workspace(root, "fresh")

    for store in ("secrets", "diagrams", "servers", "triggers", "cron"):
        assert not (root / "fresh" / store).exists(), store
