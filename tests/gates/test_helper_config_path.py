"""Each confined helper reads the config its parent loaded, not the one in $HOME.

Found while bringing up a second instance for a data connector: the executor was started for
that instance and answered `connector 'google-calendar' is not active in this deployment`,
because the child's loader falls back to `~/.nanoinfra/config.json` when nobody names one. The
argv carried a socket and a workspace and no config, so a gateway started with `--config` ran
its helpers against another instance's settings.

The blast radius was not only connectors. The executor reads `gates` for the whole policy and
`audit` for retention; the fetcher reads `tools.web`; the MCP host reads `mcpServers`, which is
the list of programs it may start. So a second instance inherited the first one's policy, its
audit retention, and its server list.

Nothing widens. `confinement._live_config_path()` already granted the child read on the
parent's config file rather than on the fallback, so the file the child now reads is the one
Landlock already allowed.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from nanoinfra.config.loader import get_config_path, set_config_path
from nanoinfra.gates.executor import __main__ as executor_entry
from nanoinfra.gates.fetcher import __main__ as fetcher_entry
from nanoinfra.gates.mcp_host import __main__ as mcp_host_entry

_HELPERS = (
    ("executor", executor_entry, "nanoinfra.gates.executor.server"),
    ("fetcher", fetcher_entry, "nanoinfra.gates.fetcher.server"),
    ("mcp-host", mcp_host_entry, "nanoinfra.gates.mcp_host.server"),
)


@pytest.fixture(autouse=True)
def _restore_config_path():
    """The config path is process global, so a test that sets it must put it back."""
    original = get_config_path()
    yield
    set_config_path(original)


def _stub_server(monkeypatch: pytest.MonkeyPatch, module_name: str) -> list[tuple[Any, Any]]:
    calls: list[tuple[Any, Any]] = []
    module = type(sys)(module_name)

    def serve_forever(socket_path: Path, *, workspace: Path) -> None:
        calls.append((socket_path, workspace))

    module.serve_forever = serve_forever  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, module_name, module)
    return calls


@pytest.mark.parametrize(("label", "entry", "server_module"), _HELPERS)
def test_a_named_config_becomes_the_one_the_child_reads(
    label: str,
    entry: Any,
    server_module: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls = _stub_server(monkeypatch, server_module)
    named = tmp_path / "instance" / "config.json"
    named.parent.mkdir()
    named.write_text("{}", encoding="utf-8")

    code = entry.main(
        [
            "--socket",
            str(tmp_path / "h.sock"),
            "--workspace",
            str(tmp_path / "ws"),
            "--config",
            str(named),
        ]
    )

    assert code == 0, label
    assert calls == [(tmp_path / "h.sock", tmp_path / "ws")]
    assert get_config_path() == named, label


@pytest.mark.parametrize(("label", "entry", "server_module"), _HELPERS)
def test_no_config_argument_leaves_the_location_alone(
    label: str,
    entry: Any,
    server_module: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The flag is optional, so a deployment that never passed one is unchanged."""
    _stub_server(monkeypatch, server_module)
    before = get_config_path()

    assert entry.main(["--socket", str(tmp_path / "h.sock"), "--workspace", str(tmp_path)]) == 0
    assert get_config_path() == before, label


@pytest.mark.parametrize(("label", "entry", "server_module"), _HELPERS)
def test_the_config_path_is_set_before_the_server_is_imported(
    label: str, entry: Any, server_module: str
) -> None:
    """Order matters: modules resolve paths from config at import time.

    The audit store proves why. It pins the device and inode of its root when the process
    starts, and that root derives from the config path, so a path set after the import pins
    another instance's log for the life of the process.

    Asserted on the source because that is where the order lives -- a runtime check would have
    to observe an import that has already happened.
    """
    source = inspect.getsource(entry.main)
    set_call = source.index("set_config_path(args.config)")
    server_import = source.index(f"from {server_module} import serve_forever")
    assert set_call < server_import, label
