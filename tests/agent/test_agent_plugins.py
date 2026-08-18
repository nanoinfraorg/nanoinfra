"""Agent Plugins v1 discovery, activation, and the executor-owned activation marker.

See nanoinfraorg/nanoinfra#138 and proposals/agent-plugins-adoption.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from nanoinfra.agent import plugins as agent_plugins
from nanoinfra.agent.plugins import (
    AGENT_PLUGIN_MCP_SCHEMA,
    AGENT_PLUGIN_SCHEMA,
    NANOINFRA_EXTENSION,
    agent_plugin_mcp_servers,
    discover_agent_plugins,
    enabled_agent_plugin_skill_dirs,
    enabled_agent_plugin_skills,
    set_agent_plugin_enabled,
)
from nanoinfra.config.schema import MCPServerConfig


@pytest.fixture(autouse=True)
def _isolate_plugin_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both roots at the temp tree: the config dir and the gate tree."""
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: config_path.parent)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _manifest(name: str, **fields: object) -> dict[str, object]:
    return {"$schema": AGENT_PLUGIN_SCHEMA, "name": name, **fields}


def _plugin(workspace: Path, name: str = "demo", **fields: object) -> Path:
    root = workspace / "plugins" / name
    _write_json(root / "plugin.json", _manifest(name, **fields))
    return root


def _skill(root: Path, name: str, frontmatter: str | None = None, body: str = "") -> Path:
    path = root / "skills" / name
    path.mkdir(parents=True)
    metadata = frontmatter or f"name: {name}\ndescription: Plugin skill."
    (path / "SKILL.md").write_text(f"---\n{metadata}\n---\n\n{body}\n", encoding="utf-8")
    return path


def _mcp(root: Path, servers: dict[str, object]) -> None:
    _write_json(root / "mcp.json", {"$schema": AGENT_PLUGIN_MCP_SCHEMA, "mcpServers": servers})


def _loaded_skills(workspace: Path) -> list[str]:
    return [name for name, _ in enabled_agent_plugin_skills(workspace)]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


# --- activation --------------------------------------------------------------------------------


def test_a_skill_loads_only_after_an_explicit_enable(workspace: Path) -> None:
    root = _plugin(workspace)
    _skill(root, "deploy-check")

    assert _loaded_skills(workspace) == []

    set_agent_plugin_enabled(workspace, "demo", True)
    assert _loaded_skills(workspace) == ["deploy-check"]

    set_agent_plugin_enabled(workspace, "demo", False)
    assert _loaded_skills(workspace) == []


def test_enabling_an_unknown_plugin_is_an_error(workspace: Path) -> None:
    with pytest.raises(ValueError, match="unknown Agent Plugin"):
        set_agent_plugin_enabled(workspace, "nope", True)


def test_changing_an_enabled_package_deactivates_it(workspace: Path) -> None:
    """Activation binds to package content, so a mutated package must stop loading."""
    root = _plugin(workspace)
    skill_dir = _skill(root, "deploy-check")
    set_agent_plugin_enabled(workspace, "demo", True)
    assert _loaded_skills(workspace) == ["deploy-check"]

    (skill_dir / "SKILL.md").write_text(
        "---\nname: deploy-check\ndescription: Swapped.\n---\n\nrm -rf /\n", encoding="utf-8"
    )

    assert _loaded_skills(workspace) == []


def test_adding_a_file_to_an_enabled_package_deactivates_it(workspace: Path) -> None:
    root = _plugin(workspace)
    _skill(root, "deploy-check")
    set_agent_plugin_enabled(workspace, "demo", True)

    (root / "extra.txt").write_text("new\n", encoding="utf-8")

    assert _loaded_skills(workspace) == []


def test_reenabling_after_a_change_restores_the_skill(workspace: Path) -> None:
    root = _plugin(workspace)
    _skill(root, "deploy-check")
    set_agent_plugin_enabled(workspace, "demo", True)
    (root / "extra.txt").write_text("new\n", encoding="utf-8")
    assert _loaded_skills(workspace) == []

    set_agent_plugin_enabled(workspace, "demo", True)

    assert _loaded_skills(workspace) == ["deploy-check"]


def test_a_legacy_path_only_marker_is_upgraded_in_place(workspace: Path) -> None:
    """A marker predating fingerprints named only the root; do not punish a reviewed package."""
    root = _plugin(workspace)
    _skill(root, "deploy-check")
    marker_dir = agent_plugins._plugin_activation_dir(workspace, "demo", create=True)
    (marker_dir / "enabled").write_text(str(root.resolve()), encoding="utf-8")

    assert _loaded_skills(workspace) == ["deploy-check"]

    upgraded = json.loads((marker_dir / "enabled").read_text(encoding="utf-8"))
    assert set(upgraded) == {"fingerprint", "root"}


def test_a_foreign_marker_payload_does_not_activate(workspace: Path) -> None:
    root = _plugin(workspace)
    _skill(root, "deploy-check")
    marker_dir = agent_plugins._plugin_activation_dir(workspace, "demo", create=True)
    (marker_dir / "enabled").write_text('{"fingerprint":"deadbeef","root":"/nope"}', encoding="utf-8")

    assert _loaded_skills(workspace) == []


# --- the one intended deviation ---------------------------------------------------------------


def test_the_activation_marker_lives_in_the_gate_tree_not_the_data_dir(workspace: Path) -> None:
    """The agent owns the data dir, so a marker there would be self-grantable (#138)."""
    _plugin(workspace)
    set_agent_plugin_enabled(workspace, "demo", True)

    activation = agent_plugins._plugin_activation_dir(workspace, "demo", create=False)
    data = agent_plugins._plugin_data_dir(workspace, "demo", create=True)

    assert (activation / "enabled").is_file()
    assert "gates" in activation.parts
    assert not (data / "enabled").exists()
    assert not activation.is_relative_to(data)


def test_the_marker_is_group_readable_and_not_world_readable(workspace: Path) -> None:
    """Skill loading runs as the agent and must read it; nobody else may."""
    _plugin(workspace)
    set_agent_plugin_enabled(workspace, "demo", True)
    marker = agent_plugins._plugin_activation_dir(workspace, "demo", create=False) / "enabled"

    mode = os.stat(marker).st_mode & 0o777
    assert mode & 0o007 == 0, "world must not read an activation marker"
    assert mode & 0o040, "the shared group must read it, since the agent loads skills"


def test_plugin_data_stays_private_to_its_owner(workspace: Path) -> None:
    """PLUGIN_DATA carries no authority, so it keeps upstream's 0700."""
    _plugin(workspace)
    data = agent_plugins._plugin_data_dir(workspace, "demo", create=True)

    assert os.stat(data).st_mode & 0o077 == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics")
@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unwritable_gate_tree_refuses_activation_clearly(workspace: Path) -> None:
    """The kernel refusal is the enforcement; it must not surface as a bare errno."""
    _plugin(workspace)
    gates = agent_plugins.get_data_dir() / "gates"
    gates.mkdir(parents=True, exist_ok=True)
    gates.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="cannot create the Agent Plugin directory"):
            set_agent_plugin_enabled(workspace, "demo", True)
    finally:
        gates.chmod(0o750)


def test_setgid_is_set_so_the_shared_group_is_inherited(workspace: Path) -> None:
    _plugin(workspace)
    set_agent_plugin_enabled(workspace, "demo", True)
    activation = agent_plugins._plugin_activation_dir(workspace, "demo", create=False)

    assert os.stat(activation).st_mode & 0o2000, "setgid keeps the shared group on new entries"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_activation_segment_cannot_escape(workspace: Path, tmp_path: Path) -> None:
    _plugin(workspace)
    base = agent_plugins.get_data_dir() / "gates" / "plugin-activation"
    base.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / agent_plugins._workspace_namespace(workspace)).symlink_to(outside)

    with pytest.raises(RuntimeError, match="escapes its parent"):
        agent_plugins._plugin_activation_dir(workspace, "demo", create=True)


def test_the_public_surface_matches_the_ported_upstream_module() -> None:
    """Pin the API so drift from upstream is visible in review.

    The upstream tree is not available in CI, so the file cannot be diffed here. What can be
    pinned is the surface: if a public name is added or removed, this fails and whoever changed it
    has to decide whether upstream carries the same name. That keeps future upstream fixes
    cherry-pickable instead of quietly incompatible.
    """
    public = {name for name in vars(agent_plugins) if not name.startswith("_")}
    ours = {
        "AGENT_PLUGIN_MCP_SCHEMA",
        "AGENT_PLUGIN_SCHEMA",
        "AgentPlugin",
        "NANOINFRA_EXTENSION",
        "agent_plugin_mcp_servers",
        "discover_agent_plugins",
        "enabled_agent_plugin_skill_dirs",
        "enabled_agent_plugin_skills",
        "merged_mcp_servers",
        "set_agent_plugin_enabled",
    }
    imported = {
        "annotations", "base64", "json", "re", "suppress", "dataclass", "replace", "sha256",
        "Path", "cast", "logger", "ValidationError", "parse_skill_metadata",
        "valid_skill_metadata", "get_config_path", "get_data_dir", "MCPServerConfig",
    }

    assert public - imported == ours


# --- manifest validation -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("manifest", "valid"),
    [
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "demo"}, True),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "a.b-c"}, True),
        ({"name": "demo"}, False),
        ({"$schema": "https://example.com/other", "name": "demo"}, False),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "Demo"}, False),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "de--mo"}, False),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "../escape"}, False),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "-lead"}, False),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": "x" * 65}, False),
        ({"$schema": AGENT_PLUGIN_SCHEMA, "name": 7}, False),
    ],
)
def test_manifest_boundary(workspace: Path, manifest: object, valid: bool) -> None:
    root = workspace / "plugins" / "pkg"
    _write_json(root / "plugin.json", manifest)

    assert bool(discover_agent_plugins(workspace)) is valid


def test_duplicate_identities_disable_both(workspace: Path) -> None:
    _write_json(workspace / "plugins" / "one" / "plugin.json", _manifest("dup"))
    _write_json(workspace / "plugins" / "two" / "plugin.json", _manifest("dup"))

    assert discover_agent_plugins(workspace) == []


def test_our_extension_namespace_supplies_presentation(workspace: Path) -> None:
    _plugin(
        workspace,
        extensions={
            NANOINFRA_EXTENSION: {
                "displayName": "Deploy Toolkit",
                "category": "Ops",
                "accentColor": "#a1b2c3",
            }
        },
    )

    plugin = discover_agent_plugins(workspace)[0]
    assert plugin.display_name == "Deploy Toolkit"
    assert plugin.category == "Ops"
    assert plugin.accent_color == "#a1b2c3"


def test_a_foreign_extension_namespace_is_ignored(workspace: Path) -> None:
    """A field under another client's namespace must not configure us."""
    _plugin(workspace, extensions={"dev.someoneelse": {"displayName": "Injected"}})

    assert discover_agent_plugins(workspace)[0].display_name == "demo"


def test_an_invalid_accent_colour_is_dropped(workspace: Path) -> None:
    _plugin(workspace, extensions={NANOINFRA_EXTENSION: {"accentColor": "red"}})

    assert discover_agent_plugins(workspace)[0].accent_color is None


# --- skill validation --------------------------------------------------------------------------


def test_a_skill_whose_metadata_name_disagrees_is_refused(workspace: Path) -> None:
    """The directory is the identity; a manifest naming something else could shadow a skill."""
    root = _plugin(workspace)
    _skill(root, "deploy-check", frontmatter="name: other\ndescription: Mismatched.")
    set_agent_plugin_enabled(workspace, "demo", True)

    assert _loaded_skills(workspace) == []


@pytest.mark.parametrize(
    "frontmatter",
    [
        "name: deploy-check",  # no description
        "name: deploy-check\ndescription: ''",  # empty description
        "not: yaml: mapping: [",  # unparseable
    ],
)
def test_invalid_skill_metadata_is_refused(workspace: Path, frontmatter: str) -> None:
    root = _plugin(workspace)
    _skill(root, "deploy-check", frontmatter=frontmatter)
    set_agent_plugin_enabled(workspace, "demo", True)

    assert _loaded_skills(workspace) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_skill_symlinked_out_of_the_package_is_refused(workspace: Path, tmp_path: Path) -> None:
    root = _plugin(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: sneaky\ndescription: Outside.\n---\n", encoding="utf-8"
    )
    (root / "skills").mkdir()
    (root / "skills" / "sneaky").symlink_to(outside)
    set_agent_plugin_enabled(workspace, "demo", True)

    assert _loaded_skills(workspace) == []


def test_skill_dirs_are_authorized_per_read_and_revalidated(workspace: Path) -> None:
    root = _plugin(workspace)
    skill_dir = _skill(root, "deploy-check")
    set_agent_plugin_enabled(workspace, "demo", True)
    enabled_agent_plugin_skills(workspace)

    assert enabled_agent_plugin_skill_dirs(workspace) == (skill_dir.resolve(),)

    # A cached authorization must not survive the package changing underneath it.
    (root / "extra.txt").write_text("new\n", encoding="utf-8")
    assert enabled_agent_plugin_skill_dirs(workspace) == ()


def test_skill_dirs_narrow_to_the_requested_path(workspace: Path) -> None:
    root = _plugin(workspace)
    first = _skill(root, "one")
    _skill(root, "two")
    set_agent_plugin_enabled(workspace, "demo", True)

    dirs = enabled_agent_plugin_skill_dirs(workspace, requested_path=first / "SKILL.md")

    assert dirs == (first.resolve(),)


# --- MCP components ----------------------------------------------------------------------------


def test_mcp_servers_require_an_explicit_enable(workspace: Path) -> None:
    root = _plugin(workspace)
    _mcp(root, {"api": {"type": "stdio", "command": "echo"}})

    assert agent_plugin_mcp_servers(workspace) == {}

    set_agent_plugin_enabled(workspace, "demo", True)
    assert set(agent_plugin_mcp_servers(workspace)) == {"demo"}


def test_a_single_server_takes_the_plugin_name(workspace: Path) -> None:
    root = _plugin(workspace)
    _mcp(root, {"api": {"type": "stdio", "command": "echo"}})
    set_agent_plugin_enabled(workspace, "demo", True)

    assert set(agent_plugin_mcp_servers(workspace)) == {"demo"}


def test_multiple_servers_are_namespaced_and_cannot_shadow_an_identity(workspace: Path) -> None:
    root = _plugin(workspace)
    _mcp(
        root,
        {
            "a": {"type": "stdio", "command": "echo"},
            "b": {"type": "stdio", "command": "echo"},
        },
    )
    set_agent_plugin_enabled(workspace, "demo", True)

    names = set(agent_plugin_mcp_servers(workspace))
    assert names == {"demo--a", "demo--b"}


def test_configured_servers_win_a_collision(workspace: Path) -> None:
    root = _plugin(workspace)
    _mcp(root, {"api": {"type": "stdio", "command": "echo"}})
    set_agent_plugin_enabled(workspace, "demo", True)
    configured = {"demo": MCPServerConfig(type="stdio", command="operator-owned")}

    merged = agent_plugin_mcp_servers(workspace, configured)

    assert merged["demo"].command == "operator-owned"


def test_plugin_root_and_data_are_injected_and_cannot_be_overridden(workspace: Path) -> None:
    root = _plugin(workspace)
    _mcp(root, {"api": {"type": "stdio", "command": "echo", "args": ["${PLUGIN_ROOT}/x"]}})
    set_agent_plugin_enabled(workspace, "demo", True)

    server = agent_plugin_mcp_servers(workspace)["demo"]

    assert server.env["PLUGIN_ROOT"] == str(root.resolve())
    assert server.env["PLUGIN_DATA"]
    assert server.args == [f"{root.resolve()}/x"]


def test_a_package_setting_plugin_root_itself_is_refused(workspace: Path) -> None:
    root = _plugin(workspace)
    _mcp(root, {"api": {"type": "stdio", "command": "echo", "env": {"PLUGIN_ROOT": "/tmp"}}})
    set_agent_plugin_enabled(workspace, "demo", True)

    assert agent_plugin_mcp_servers(workspace) == {}


@pytest.mark.parametrize(
    "server",
    [
        {"type": "streamableHttp", "command": "echo"},  # stdio only
        {"type": "stdio", "command": "/usr/bin/echo"},  # absolute path outside the package
        {"type": "stdio", "command": "../escape"},  # traversal
        {"type": "stdio", "command": "two words"},
        {"type": "stdio", "command": ""},
        {"type": "stdio", "command": "echo", "unknownField": 1},  # unknown semantics
        {"type": "stdio", "command": "echo", "cwd": "/etc"},  # cwd outside the package
    ],
)
def test_invalid_mcp_servers_are_refused(workspace: Path, server: dict[str, object]) -> None:
    root = _plugin(workspace)
    _mcp(root, {"api": server})
    set_agent_plugin_enabled(workspace, "demo", True)

    assert agent_plugin_mcp_servers(workspace) == {}


def test_an_mcp_component_with_the_wrong_schema_is_refused(workspace: Path) -> None:
    root = _plugin(workspace)
    _write_json(
        root / "mcp.json",
        {"$schema": "https://example.com/other", "mcpServers": {"api": {"command": "echo"}}},
    )
    set_agent_plugin_enabled(workspace, "demo", True)

    assert agent_plugin_mcp_servers(workspace) == {}


def test_discovery_reports_components_and_state(workspace: Path) -> None:
    root = _plugin(workspace)
    _skill(root, "deploy-check")
    _mcp(root, {"api": {"type": "stdio", "command": "echo"}})
    set_agent_plugin_enabled(workspace, "demo", True)

    plugin = discover_agent_plugins(workspace)[0]

    assert plugin.enabled is True
    assert plugin.mcp_servers == ("api",)


# --- logo --------------------------------------------------------------------------------------


def test_a_logo_is_validated_by_magic_bytes(workspace: Path) -> None:
    root = _plugin(workspace, extensions={NANOINFRA_EXTENSION: {"logo": "./logo.png"}})
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    assert discover_agent_plugins(workspace)[0].logo.startswith("data:image/png;base64,")


def test_a_logo_with_a_lying_suffix_is_dropped(workspace: Path) -> None:
    root = _plugin(workspace, extensions={NANOINFRA_EXTENSION: {"logo": "./logo.png"}})
    (root / "logo.png").write_bytes(b"<svg>not a png</svg>")

    assert discover_agent_plugins(workspace)[0].logo is None


def test_an_oversized_logo_is_dropped(workspace: Path) -> None:
    root = _plugin(workspace, extensions={NANOINFRA_EXTENSION: {"logo": "./logo.png"}})
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * (256 * 1024))

    assert discover_agent_plugins(workspace)[0].logo is None


def test_a_logo_outside_the_package_is_dropped(workspace: Path) -> None:
    _plugin(workspace, extensions={NANOINFRA_EXTENSION: {"logo": "../../secret.png"}})

    assert discover_agent_plugins(workspace)[0].logo is None


# --- containment -------------------------------------------------------------------------------


def test_no_plugins_directory_is_not_an_error(workspace: Path) -> None:
    assert discover_agent_plugins(workspace) == []
    assert enabled_agent_plugin_skills(workspace) == []
    assert agent_plugin_mcp_servers(workspace) == {}


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_a_symlinked_plugins_directory_is_ignored(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _write_json(outside / "pkg" / "plugin.json", _manifest("sneaky"))
    (workspace / "plugins").symlink_to(outside)

    assert discover_agent_plugins(workspace) == []
