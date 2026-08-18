"""Plugin skills sit between workspace and builtin skills.

Precedence is workspace > plugin > builtin. A workspace skill is the operator's own file and wins;
a plugin skill is reviewed and installed, so it beats a builtin of the same name; a builtin is the
fallback. See nanoinfraorg/nanoinfra#139.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nanoinfra.agent import plugins as agent_plugins
from nanoinfra.agent.plugins import AGENT_PLUGIN_SCHEMA, set_agent_plugin_enabled
from nanoinfra.agent.skills import SkillsLoader


@pytest.fixture(autouse=True)
def _isolate_plugin_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "state" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(agent_plugins, "get_config_path", lambda: config_path)
    monkeypatch.setattr(agent_plugins, "get_data_dir", lambda: config_path.parent)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    path = tmp_path / "workspace"
    path.mkdir()
    return path


@pytest.fixture
def builtin(tmp_path: Path) -> Path:
    path = tmp_path / "builtin"
    path.mkdir()
    return path


def _skill_at(base: Path, name: str, body: str) -> Path:
    path = base / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {body}.\n---\n\n{body}\n", encoding="utf-8"
    )
    return path


def _enabled_plugin_skill(workspace: Path, name: str, body: str, plugin: str = "demo") -> Path:
    root = workspace / "plugins" / plugin
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": AGENT_PLUGIN_SCHEMA, "name": plugin}), encoding="utf-8"
    )
    path = _skill_at(root / "skills", name, body)
    set_agent_plugin_enabled(workspace, plugin, True)
    return path


def _loader(workspace: Path, builtin: Path, **kwargs: object) -> SkillsLoader:
    return SkillsLoader(workspace, builtin_skills_dir=builtin, **kwargs)  # type: ignore[arg-type]


def _sources(loader: SkillsLoader) -> dict[str, str]:
    return {s["name"]: s["source"] for s in loader.list_skills(filter_unavailable=False)}


def test_a_plugin_skill_is_listed_with_its_source(workspace: Path, builtin: Path) -> None:
    _enabled_plugin_skill(workspace, "deploy-check", "from plugin")

    assert _sources(_loader(workspace, builtin)) == {"deploy-check": "plugin"}


def test_a_disabled_plugin_contributes_nothing(workspace: Path, builtin: Path) -> None:
    _enabled_plugin_skill(workspace, "deploy-check", "from plugin")
    set_agent_plugin_enabled(workspace, "demo", False)

    assert _sources(_loader(workspace, builtin)) == {}


def test_workspace_beats_plugin(workspace: Path, builtin: Path) -> None:
    _skill_at(workspace / "skills", "shared", "from workspace")
    _enabled_plugin_skill(workspace, "shared", "from plugin")

    loader = _loader(workspace, builtin)

    assert _sources(loader) == {"shared": "workspace"}
    assert "from workspace" in (loader.load_skill("shared") or "")


def test_plugin_beats_builtin(workspace: Path, builtin: Path) -> None:
    _skill_at(builtin, "shared", "from builtin")
    _enabled_plugin_skill(workspace, "shared", "from plugin")

    loader = _loader(workspace, builtin)

    assert _sources(loader) == {"shared": "plugin"}
    assert "from plugin" in (loader.load_skill("shared") or "")


def test_workspace_beats_both(workspace: Path, builtin: Path) -> None:
    _skill_at(workspace / "skills", "shared", "from workspace")
    _skill_at(builtin, "shared", "from builtin")
    _enabled_plugin_skill(workspace, "shared", "from plugin")

    loader = _loader(workspace, builtin)

    assert _sources(loader) == {"shared": "workspace"}
    assert "from workspace" in (loader.load_skill("shared") or "")


def test_all_three_sources_coexist_when_names_differ(workspace: Path, builtin: Path) -> None:
    _skill_at(workspace / "skills", "ws", "w")
    _skill_at(builtin, "bi", "b")
    _enabled_plugin_skill(workspace, "pl", "p")

    assert _sources(_loader(workspace, builtin)) == {"ws": "workspace", "bi": "builtin", "pl": "plugin"}


def test_two_plugins_offering_one_name_keep_the_first_deterministically(
    workspace: Path, builtin: Path
) -> None:
    """Discovery is sorted, so the winner must not depend on filesystem order."""
    _enabled_plugin_skill(workspace, "shared", "from alpha", plugin="alpha")
    _enabled_plugin_skill(workspace, "shared", "from beta", plugin="beta")

    loader = _loader(workspace, builtin)

    assert _sources(loader) == {"shared": "plugin"}
    assert "from alpha" in (loader.load_skill("shared") or "")


def test_load_skill_reads_a_plugin_skill(workspace: Path, builtin: Path) -> None:
    _enabled_plugin_skill(workspace, "deploy-check", "plugin body")

    assert "plugin body" in (_loader(workspace, builtin).load_skill("deploy-check") or "")


def test_load_skill_returns_none_for_an_unknown_name(workspace: Path, builtin: Path) -> None:
    assert _loader(workspace, builtin).load_skill("nope") is None


def test_a_tampered_plugin_stops_serving_its_skill(workspace: Path, builtin: Path) -> None:
    """Activation binds to package content, and skill loading must honour that."""
    _enabled_plugin_skill(workspace, "deploy-check", "plugin body")
    (workspace / "plugins" / "demo" / "tamper.txt").write_text("x", encoding="utf-8")

    loader = _loader(workspace, builtin)

    assert _sources(loader) == {}
    assert loader.load_skill("deploy-check") is None


def test_disabled_skills_still_apply_to_plugin_skills(workspace: Path, builtin: Path) -> None:
    _enabled_plugin_skill(workspace, "deploy-check", "plugin body")

    loader = _loader(workspace, builtin, disabled_skills={"deploy-check"})

    assert _sources(loader) == {}


def test_load_skills_for_context_includes_a_plugin_skill(workspace: Path, builtin: Path) -> None:
    _enabled_plugin_skill(workspace, "deploy-check", "plugin body")

    rendered = _loader(workspace, builtin).load_skills_for_context(["deploy-check"])

    assert "### Skill: deploy-check" in rendered
    assert "plugin body" in rendered
    assert "description:" not in rendered, "frontmatter must be stripped"


def test_no_plugins_directory_changes_nothing(workspace: Path, builtin: Path) -> None:
    _skill_at(workspace / "skills", "ws", "w")
    _skill_at(builtin, "bi", "b")

    assert _sources(_loader(workspace, builtin)) == {"ws": "workspace", "bi": "builtin"}
