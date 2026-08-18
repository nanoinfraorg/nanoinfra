"""Flipping the restrictToWorkspace default must not tighten a running install.

`tools.restrictToWorkspace` gates the ExecTool workspace path guard. The shipped default was
False, so the guard was inert unless an operator turned it on. Flipping it to True secures new
installs, and would silently start blocking commands on every existing one.

`save_config` writes a full `model_dump`, so any config this codebase has ever written contains
the key explicitly. A config file that omits it therefore predates the flip (or was hand-written),
and the migration pins it to the value it has been running with. See nanoinfraorg/nanoinfra#135.
"""

from __future__ import annotations

import json
from pathlib import Path

from nanoinfra.config.loader import load_config, save_config
from nanoinfra.config.schema import Config, ToolsConfig


def _write(path: Path, payload: dict[str, object]) -> Path:
    config = path / "config.json"
    config.write_text(json.dumps(payload), encoding="utf-8")
    return config


def test_the_shipped_default_is_now_restrictive() -> None:
    assert ToolsConfig().restrict_to_workspace is True


def test_a_config_without_the_key_keeps_the_old_behaviour(tmp_path: Path) -> None:
    """The upgrade case: an install that never set it has been running unrestricted."""
    config = _write(tmp_path, {"tools": {}})

    assert load_config(config).tools.restrict_to_workspace is False


def test_a_config_with_no_tools_block_keeps_the_old_behaviour(tmp_path: Path) -> None:
    config = _write(tmp_path, {"agents": {"defaults": {"model": "x"}}})

    assert load_config(config).tools.restrict_to_workspace is False


def test_an_explicit_true_is_preserved(tmp_path: Path) -> None:
    config = _write(tmp_path, {"tools": {"restrictToWorkspace": True}})

    assert load_config(config).tools.restrict_to_workspace is True


def test_an_explicit_false_is_preserved(tmp_path: Path) -> None:
    config = _write(tmp_path, {"tools": {"restrictToWorkspace": False}})

    assert load_config(config).tools.restrict_to_workspace is False


def test_the_legacy_exec_key_still_wins_over_the_pin(tmp_path: Path) -> None:
    """The older tools.exec.restrictToWorkspace move must not be shadowed by the new pin."""
    config = _write(tmp_path, {"tools": {"exec": {"restrictToWorkspace": True}}})

    assert load_config(config).tools.restrict_to_workspace is True


def test_a_saved_config_carries_the_key_so_it_is_never_pinned(tmp_path: Path) -> None:
    """A fresh install writes the key explicitly, so reloading keeps the new default."""
    config = tmp_path / "config.json"
    save_config(Config(), config)

    payload = json.loads(config.read_text(encoding="utf-8"))
    assert payload["tools"]["restrictToWorkspace"] is True
    assert load_config(config).tools.restrict_to_workspace is True


def test_a_saved_opt_out_survives_a_reload(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    settings = Config()
    settings.tools.restrict_to_workspace = False
    save_config(settings, config)

    assert load_config(config).tools.restrict_to_workspace is False
