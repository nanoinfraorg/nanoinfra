# tests/cli/test_config_path_redirection.py
"""A test redirects the config path through the global, and never through a stand-in -- #80.

`mock_paths` replaced three functions on `nanoinfra.config.loader` with mocks. `mock.patch` restores
the loader module, and it reaches no other module, so any module that ran

    from nanoinfra.config.loader import get_config_path

for the **first time** inside that window kept the mock for the life of the worker. `onboard`
imports channel modules and the wizard lazily, so four modules kept one:

    nanoinfra.channels.weixin.state.get_config_path   = <MagicMock name='get_config_path'>
    nanoinfra.channels.whatsapp.state.get_config_path = <MagicMock name='get_config_path'>
    nanoinfra.channels.validation.load_config         = <MagicMock name='load_config'>
    nanoinfra.cli.onboard.get_config_path             = <MagicMock name='get_config_path'>
    nanoinfra.cli.onboard.load_config                 = <MagicMock name='load_config'>

The mock answered with a directory the fixture had already deleted, so
`tests/channels/test_channel_plugins.py::test_optional_features_payload_detects_legacy_default_weixin_state`
read `configured` as False. That test asks whether a `weixin/account.json` sits beside the config
file, and the stale mock pointed the answer at a dead path.

The suite order is how we noticed. `pytest tests/cli/ tests/channels/` failed and
`pytest tests/channels/` alone passed, because a bare run imports those modules long before it
reaches `tests/cli`. The property is about the fixture, so the tests below measure the fixture and
never a collection order.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from unittest.mock import NonCallableMock

from nanoinfra.config import loader
from nanoinfra.config.schema import Config

# The names a module can bind at import time, and therefore the names a fixture must not replace on
# the loader module. `grep -rn "from nanoinfra.config.loader import" nanoinfra/` finds a module
# level binding for each one.
_LOADER_FUNCTIONS = (
    "get_config_path",
    "load_config",
    "save_config",
    "set_config_path",
    "resolve_config_env_vars",
)


def _load_a_fresh_copy(module_path: str) -> ModuleType:
    """Execute *module_path* again, under a throwaway name.

    This is what a lazy import does the first time it runs, and that is the moment the leak
    happened. A throwaway name leaves `sys.modules` unchanged, so the copy reaches no other test.
    """
    spec = importlib.util.find_spec(module_path)
    assert spec is not None and spec.origin is not None, module_path
    copy_spec = importlib.util.spec_from_file_location(
        f"{module_path}__copy_for_a_test", spec.origin
    )
    assert copy_spec is not None and copy_spec.loader is not None
    copy = importlib.util.module_from_spec(copy_spec)
    copy_spec.loader.exec_module(copy)
    return copy


def test_the_fixture_redirects_the_config_path(mock_paths) -> None:
    """The redirection still works, which is the reason the fixture exists."""
    config_file, _, _ = mock_paths

    assert loader.get_config_path() == config_file


def test_the_fixture_leaves_every_loader_function_alone(mock_paths) -> None:
    """No stand-in sits on the loader module while the fixture is active (#80).

    A module that imports one of these names now must get the real function. The check reads the
    module each function was defined in, so a plain function is no more acceptable than a mock. A
    module that captured either one would hold a dead path just the same.
    """
    _ = mock_paths

    for name in _LOADER_FUNCTIONS:
        value = getattr(loader, name)
        assert not isinstance(value, NonCallableMock), (
            f"nanoinfra.config.loader.{name} is a mock while mock_paths is active. A module that "
            "imports that name now keeps the mock after the fixture ends (#80). Redirect through "
            "loader._current_config_path instead."
        )
        assert getattr(value, "__module__", None) == "nanoinfra.config.loader", (
            f"nanoinfra.config.loader.{name} is a stand-in while mock_paths is active (#80)."
        )


def _plant_a_legacy_weixin_state(config_file: Path) -> None:
    """Write the `weixin/account.json` that sits beside a config file."""
    state_dir = config_file.parent / "weixin"
    state_dir.mkdir(parents=True)
    (state_dir / "account.json").write_text(
        json.dumps({"token": "legacy-weixin-token"}),
        encoding="utf-8",
    )


def test_a_module_imported_now_still_follows_the_config_path(mock_paths, tmp_path) -> None:
    """The defect of #80, as a property rather than as a collection order.

    `nanoinfra.channels.weixin.state` binds `get_config_path` at module level, and it derives a
    legacy default state directory from it. A fresh copy of that module stands for the first lazy
    import that `onboard` triggers.

    The copy must hold the live function. A live function reads `_current_config_path` on every
    call, so the copy follows the global after the import. A captured stand-in answers with the one
    path it was built with, whatever the global says afterwards, and that is what left a channels
    test reading a deleted directory.
    """
    config_file, _, _ = mock_paths
    _plant_a_legacy_weixin_state(config_file)

    weixin_state = _load_a_fresh_copy("nanoinfra.channels.weixin.state")

    assert weixin_state.get_config_path.__module__ == "nanoinfra.config.loader"
    # `None` is the section a channel with no saved config has, so this reads the legacy default.
    assert weixin_state.local_state_present(None) is True

    # Move the config path, to a directory that holds no saved state. A live binding follows and
    # answers False. A stand-in keeps pointing at the directory above and answers True.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    loader.set_config_path(elsewhere / "config.json")

    assert weixin_state.local_state_present(None) is False


def test_the_onboard_command_writes_a_config_the_loader_can_read(mock_paths) -> None:
    """The real save and the real load run under this fixture now, so a round trip must hold.

    The mocked `save_config` wrote `model_dump(by_alias=True)` without `mode="json"`, and the mocked
    `load_config` answered a default `Config()` whatever the file held. So neither half of the round
    trip was measured here before.
    """
    from typer.testing import CliRunner

    from nanoinfra.cli.commands import app

    config_file, _, _ = mock_paths

    result = CliRunner().invoke(app, ["onboard"])

    assert result.exit_code == 0, result.stdout
    assert loader.load_config(config_file).agents.defaults.model == Config().agents.defaults.model
