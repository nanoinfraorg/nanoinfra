# tests/cli/conftest.py
"""No CLI test starts a real helper process, and no CLI test replaces a config loader function.

`_run_gateway` starts three children now: the executor (#40), the fetcher (#19), and the MCP host
(#22). Each one is a real interpreter that binds a socket, and a test that reaches the real start
leaves that child alive after the test ends. Ten such orphans came out of one session, and two of
them held the operator's own `~/.nanoinfra/run` sockets, which would collide with the next real
gateway start.

One test file already stubbed the three starts, and one test that never used that helper leaked
anyway. So the stub belongs here, where it covers the whole package and needs no test to remember
it. `tests/gates` covers each start on its own, and those tests keep their children under a
`tmp_path`.

`mock_paths` lives here for a different reason (#80). It redirects the config path for the onboard
tests, and it must do that through the `_current_config_path` global rather than through a mock of
`get_config_path`. `tests/cli/test_config_path_redirection.py` holds the property, and it needs the
fixture, so the fixture needs a shared home.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

_HELPER_STARTS = (
    "_start_executor_for_gateway",
    "_start_fetcher_for_gateway",
    "_start_mcp_host_for_gateway",
)

# A CLI test may start the gateway as a real subprocess, and a monkeypatch in this process cannot
# reach that child. These are the variables a deployment sets when another supervisor already runs
# the helpers, so the child starts none of the three and needs no patch at all.
_EXTERNAL_ENV = (
    "NANOINFRA_EXECUTOR_EXTERNAL",
    "NANOINFRA_FETCHER_EXTERNAL",
    "NANOINFRA_MCP_HOST_EXTERNAL",
)


@pytest.fixture(autouse=True)
def no_real_helper_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace each helper start with one that answers None.

    None is what a failed start already returns, and every caller handles it, so the gateway path
    under test behaves as it does on a host where a helper cannot start.
    """
    for helper in _HELPER_STARTS:
        monkeypatch.setattr(
            f"nanoinfra.cli.gateway_runtime.{helper}",
            lambda _config: None,
            raising=True,
        )
    for name in _EXTERNAL_ENV:
        monkeypatch.setenv(name, "1")


def _helper_names() -> tuple[str, ...]:
    return _HELPER_STARTS


@pytest.fixture
def helper_start_names() -> tuple[str, ...]:
    """The three names, for a test that asserts the fixture covers all of them."""
    return _helper_names()


@pytest.fixture
def helper_external_env() -> tuple[str, ...]:
    """The three variables a spawned gateway reads, for the same reason."""
    return _EXTERNAL_ENV


@pytest.fixture
def mock_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[Path, Path, MagicMock]]:
    """Point the real config path at a temporary file (nanoinfraorg/nanoinfra#80).

    This fixture used to replace ``get_config_path``, ``load_config`` and ``save_config`` on
    ``nanoinfra.config.loader`` with mocks. Those mocks escaped the fixture.

    ``onboard`` imports channel modules and the wizard on first use, so a module can reach its own
    first import while this fixture holds the loader. Several of those modules bind a loader
    function by name at module level::

        from nanoinfra.config.loader import get_config_path

    A module that runs that line keeps the mock. ``mock.patch`` restores the loader module and
    reaches no copy of the name, so the mock stayed for the life of the worker, and it answered
    with a directory this fixture had already deleted. Four modules each held one:
    ``nanoinfra.channels.weixin.state``, ``nanoinfra.channels.whatsapp.state``,
    ``nanoinfra.channels.validation`` and ``nanoinfra.cli.onboard``. The weixin one failed
    ``tests/channels/test_channel_plugins.py``, which reads a legacy default state directory
    beside the config file.

    ``_current_config_path`` is the seam the product itself uses. Every reader calls the live
    ``get_config_path``, and that function reads the global, so one assignment reaches a module
    that is already imported and a module that is not. No stand-in exists for a later import to
    keep. The root ``conftest.py`` restores the same global around every test.

    ``load_config`` and ``save_config`` run for real now, inside ``tmp_path``.
    """
    from nanoinfra.config import loader

    base_dir = tmp_path / "onboard"
    base_dir.mkdir()
    config_file = base_dir / "config.json"
    workspace_dir = base_dir / "workspace"

    # A private global, and the reason to reach it is that ``set_config_path`` writes it and no
    # public reset exists. This is the pattern the root ``conftest.py`` uses for the same global.
    monkeypatch.setattr(loader, "_current_config_path", config_file)

    # The workspace path is patched on the module that reads it, and no other module holds a copy
    # of that name, so this mock cannot escape. A test asserts what ``onboard`` passed to it.
    mock_workspace_path = MagicMock(return_value=workspace_dir)
    monkeypatch.setattr("nanoinfra.cli.commands.get_workspace_path", mock_workspace_path)

    yield config_file, workspace_dir, mock_workspace_path


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "real_helper_process: this test starts a real helper child and cleans it up itself",
    )
